"""
模拟真实 WinRT BLE 回调线程（无事件循环的 Dummy-XXXX 线程）跑完整语音链路。

验证 v1.0.4 的修复：
  - MIC_OPEN / MIC_CLOSE 在「没有 asyncio 事件循环」的线程里也能真正投递出去
  - session.py 的 threading.Timer 定时器在回调线程里不炸
  - 投递失败时 ensure_mic_open() 返回 False 且**不置位** mic_open_sent
    （main.py 据此打 ERROR，而不是假报「已重新开麦」）

流程严格照抄 main.py 的 on_ctl 分支：
  audio_start → on_audio_start(codec, stream_id) → ensure_mic_open()   [第 1 次 MIC_OPEN]
  mic_open_result(0) → on_mic_open_result(0) → 挂 OPEN_TIMEOUT 定时器
  audio_stop  → on_audio_stop() → 相位归 CLOSED（清 mic_open_sent）
              → ensure_mic_open()                                      [第 2 次 MIC_OPEN]

2026-09-29 补（审查报告 P2-3）：这个类有**两个**线程在碰它 ——
BLE 回调线程 + `threading.Timer` 的定时器线程。所以这里再钉两件事：

  - **定时器必须登记**：`_retry_open` 的定时器原先返回值直接丢了，
    `close()` 取消不到它 ⇒ 会话已经收尾，1 秒后它到点照样开麦。
    这里用「对照 + 判据」两条路把它测出来（对照证明定时器真会跑，
    判据证明 `close()` 拦得住）。
  - **shutdown() 之后对象作废**：重连/退出时旧协调器会被丢掉，
    但它排出去的定时器还挂在 threading 里。`shutdown()` 之后所有入口
    必须一律 no-op。

用法：python tools/check_ble_callback_thread.py
（tools/check_all.py 会带上它 —— 这是防「MIC_OPEN 又悄悄发不出去」的回归闸）
"""
import ast
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atvv
from session import SessionCoordinator, Phase

SESSION_PY = Path(__file__).resolve().parent.parent / "session.py"

# 所有会碰状态、因而**必须**持锁的入口（P2-3）。
# ⚠ 加新入口时也要加到这里 —— 漏了就等于漏一把锁。
LOCKED_METHODS = [
    "set_mode", "voice_key_down", "voice_key_up", "close",
    "ensure_mic_open", "mark_mic_open_failed",
    "on_audio_start", "on_audio_stop", "on_mic_open_result", "shutdown",
    "_open_timeout", "_close_timeout", "_retry_open",
]

sent: list[str] = []


def make_bridge_loop(broken: bool = False):
    """起一个主事件循环，模拟 run_bridge 里的 _bridge_loop。

    broken=True 时返回一个**已经 close 掉**的循环：run_coroutine_threadsafe 会抛
    RuntimeError('Event loop is closed') —— 模拟 v1.0.3 那种"投递阶段就失败"。
    注意：不能只是"没跑起来"（那样 call_soon_threadsafe 照样成功入队，
    只是没人消费，属于"已投递"，返回 True 才是对的）。
    """
    loop = asyncio.new_event_loop()

    if broken:
        loop.close()
        return loop

    def _runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_runner, name="MainLoop", daemon=True)
    t.start()
    return loop


def _send_tx(loop, cmd: bytes, tag: str) -> bool:
    """与 main.py::_send_tx 同构。"""
    if not cmd:
        return False

    async def _write():
        await asyncio.sleep(0)          # 让出一次，模拟真实 GATT 写入
        sent.append(tag)
        print(f"    [loop={threading.current_thread().name}] 真正写入 {tag}  {cmd.hex(' ')}")
        return True

    coro = _write()
    try:
        asyncio.run_coroutine_threadsafe(coro, loop)
        return True
    except Exception as e:
        coro.close()        # 与 main.py 一致：别留 "never awaited" 噪声
        print(f"    ❌ {tag} schedule failed: {e}")
        return False


def build(atv, loop):
    def on_mic_open(sid):
        cmd = atv.mic_open_cmd()
        return cmd if _send_tx(loop, cmd, "open") else None

    def on_mic_close(sid):
        cmd = atv.mic_close_cmd()
        return cmd if _send_tx(loop, cmd, "close") else None

    return SessionCoordinator(on_mic_open=on_mic_open, on_mic_close=on_mic_close)


# ── 静态判据（纯函数，反例复用）──────────────────────────────────────────────

def method_bodies(src: str) -> dict[str, str]:
    """{方法名: 方法源码}，只看 `SessionCoordinator` 里的 def。"""
    out: dict[str, str] = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == "SessionCoordinator":
            for f in node.body:
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f.name] = ast.get_source_segment(src, f) or ""
    return out


def unlocked_methods(src: str) -> list[str]:
    """返回**没有** `with self._lock:` 的入口方法名。

    这些方法会被两个线程同时调用（BLE 回调线程 + 定时器线程），
    不持锁就可能留下 `phase=CLOSED` 而 `mic_open_sent=True` 这种自相矛盾的状态。
    """
    bodies = method_bodies(src)
    return [n for n in LOCKED_METHODS
            if n in bodies and "with self._lock:" not in bodies[n]]


def untracked_schedules(src: str) -> list[int]:
    """返回 `self._schedule(...)` 被当**语句**丢掉返回值的那几行行号。

    ⚠ 用 ast 而不是正则：这些调用常跨行，正则判断不了"这一行是不是被赋值了"。
      丢返回值的后果：`close()` / `shutdown()` 取消不到它 —— 会话收尾了、
      链路重连了，它到点还会把相位推回 OPENING 并再发一次 MIC_OPEN。
    """
    bad: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            f = node.value.func
            if (isinstance(f, ast.Attribute) and f.attr == "_schedule"
                    and isinstance(f.value, ast.Name) and f.value.id == "self"):
                bad.append(node.lineno)
    return bad


def case_static() -> bool:
    src = SESSION_PY.read_text(encoding="utf-8")
    ok = True

    def ck(cond: bool, msg: str) -> None:
        nonlocal ok
        print(f"  {'✅' if cond else '❌'} {msg}")
        if not cond:
            ok = False

    print(f"\n{'='*72}\n【静态】session.py 的线程纪律（P2-3）\n{'='*72}")
    ck("self._lock = threading.RLock()" in src,
       "有可重入锁 `self._lock = threading.RLock()`"
       "（用 Lock 会在「公开方法互相调用」时自己锁死自己）")
    bad = unlocked_methods(src)
    ck(not bad, f"每个会碰状态的入口都持锁（缺：{bad}）")
    bad_sched = untracked_schedules(src)
    ck(not bad_sched, f"每个 `self._schedule(...)` 都收下了返回的定时器（丢在行 {bad_sched}）")
    ck("self._timers: set[threading.Timer] = set()" in src,
       "定时器登记表 `self._timers` 存在")
    ck("def shutdown(self) -> None:" in src and "self._closed = True" in src,
       "有 `shutdown()`，并且用 `_closed = True` 作硬标记")
    ck("if self._closed:" in method_bodies(src).get("_retry_open", ""),
       "`_retry_open` 先看 `_closed`（重连后到点的幽灵定时器必须被挡住）")
    sched_body = method_bodies(src).get("_schedule", "")
    ck("if self._closed:" in sched_body,
       "`_schedule` 的**回调里**也看 `_closed`"
       "（`Timer.cancel()` 对已经进入回调的定时器无效，只能靠标记挡）")

    # 反例：把锁拆掉 / 把定时器返回值丢掉 —— 必须被判出来
    no_lock = src.replace("        with self._lock:\n", "", 1)
    ck(no_lock != src and unlocked_methods(no_lock),
       "反例：删掉一处 `with self._lock:` → 判据报出未持锁的方法")
    dropped = src.replace(
        "            self._open_timer = self._schedule(self.OPEN_TIMEOUT, self._open_timeout)",
        "            self._schedule(self.OPEN_TIMEOUT, self._open_timeout)", 1)
    ck(dropped != src and untracked_schedules(dropped),
       "反例：把 `_schedule` 的返回值丢掉 → 判据报出行号")
    ck(untracked_schedules(src) == [],
       "对照：改好之后同一个判定函数返回空（不是永远返回非空）")
    return ok


def run_retry_timer_case(do_close: bool) -> bool:
    """`_retry_open` 的定时器到点会不会诈尸（P2-3）。

    `do_close=False` 是**对照**：证明这个定时器真的会跑（不然判据测的是空气）。
    `do_close=True`  是**判据**：`close()` 之后它必须闭嘴。
    """
    global sent
    sent = []
    title = ("【判据】close() 之后，重试定时器不许再开麦"
             if do_close else
             "【对照】不 close 时，重试定时器**确实**会到点开麦（证明测的不是空气）")
    print(f"\n{'='*72}\n{title}\n{'='*72}")

    loop = make_bridge_loop(broken=False)
    atv = atvv.ATVVProtocol()
    atv.parse_control(bytes([11, 1, 0, 0x02, 0x00, 0x00, 100]))
    sess = build(atv, loop)

    # 相位停在 CLOSED：`_retry_open` 只在 CLOSED 时才真的重开麦，
    # 所以这条用例必须不经 on_audio_start（它会把相位推到 OPEN）。
    sess.on_mic_open_result(1)          # code != 0 → 挂 1.0s 的重试定时器
    n_timers = len(getattr(sess, "_timers", []) or [])
    print(f"  挂上重试定时器：此刻在跑 {n_timers} 个（应 ≥1）")

    if do_close:
        sess.close("测试收尾")
        print(f"  close() 之后在跑 {len(getattr(sess, '_timers', []) or [])} 个（应 0）")

    time.sleep(1.6)                     # 超过 1.0s 的重试延时
    n_open = sent.count("open")
    print(f"  1.6 秒内发出的 MIC_OPEN 次数：{n_open}")

    expect = 0 if do_close else 1
    ok = (n_timers >= 1) and (n_open == expect)
    print(f"  {'✅ PASS' if ok else '❌ FAIL'} —— 期望 {expect} 次，实际 {n_open} 次")
    return ok


def run_shutdown_case() -> bool:
    """`shutdown()` 之后对象作废：所有入口一律 no-op（P2-3）。"""
    global sent
    sent = []
    print(f"\n{'='*72}\n【判据】shutdown() 之后所有入口变 no-op\n{'='*72}")

    loop = make_bridge_loop(broken=False)
    atv = atvv.ATVVProtocol()
    atv.parse_control(bytes([11, 1, 0, 0x02, 0x00, 0x00, 100]))
    sess = build(atv, loop)

    sess.shutdown()
    print(f"  shutdown() 后 is_closed={sess.is_closed}（应 True）")

    # 重连/退出之后，这些调用全都该被挡住 —— 一条命令都不许再发出去。
    sess.voice_key_down()
    sess.on_audio_start(codec=3, stream_id=1)
    sess.ensure_mic_open()
    sess.on_mic_open_result(0)
    sess.on_audio_stop(0)
    time.sleep(0.3)

    phase = sess.state.phase
    print(f"  连打 5 个入口之后：phase={phase.name}  发出命令 {sent}")
    ok = (sess.is_closed and sent == [] and phase == Phase.CLOSED
          and len(getattr(sess, "_timers", []) or []) == 0)
    print(f"  {'✅ PASS' if ok else '❌ FAIL'} —— 期望：无命令 / phase=CLOSED / 无残留定时器")
    return ok


def run_case(title: str, broken: bool, expect: list[str], expect_sent_flag: bool) -> bool:
    global sent
    sent = []
    print(f"\n{'='*72}\n{title}\n{'='*72}")

    loop = make_bridge_loop(broken=broken)
    atv = atvv.ATVVProtocol()
    # 真机上电先协商能力，否则 mic_open_cmd() 会抛 "No capabilities negotiated"
    atv.parse_control(bytes([11, 1, 0, 0x02, 0x00, 0x00, 100]))   # CAPS v1.0 / ADPCM 16k
    sess = build(atv, loop)

    def callback_thread():
        print(f"  回调线程 = {threading.current_thread().name}"
              f"（无 asyncio 事件循环，正是 v1.0.3 出事的地方）")
        try:
            asyncio.get_event_loop()
            print("    ⚠ 竟然拿到事件循环了（与真机不符）")
        except Exception as e:
            print(f"    确认无事件循环: {e}")

        # ① 遥控器按下语音键 → 只上报 AUDIO_START（真机从不上报 START_SEARCH）
        print("  ① audio_start → on_audio_start + ensure_mic_open")
        sess.on_audio_start(codec=3, stream_id=1)
        atv.state.stream_active = True
        r1 = sess.ensure_mic_open()
        print(f"     ensure_mic_open() -> {r1}（应 True）")

        # ② 遥控器回 MIC_OPEN 成功 → 挂 OPEN_TIMEOUT 定时器（threading.Timer）
        print("  ② mic_open_result(0) → on_mic_open_result")
        sess.on_mic_open_result(0)

        # ③ 收流（解码在 atvv 层，session 不参与）
        print("  ③ 收流中 atvv.decode_audio x3")
        for _ in range(3):
            atv.decode_audio(b"\x00" * 20)

        # ④ 松手 → audio_stop → 相位归 CLOSED，然后补开麦
        print("  ④ audio_stop → on_audio_stop + ensure_mic_open（补发，关键）")
        sess.on_audio_stop(reason=0)
        atv.state.stream_active = False
        r2 = sess.ensure_mic_open()
        print(f"     ensure_mic_open() -> {r2}（应 {expect_sent_flag}）")

        print(f"  ⑤ 等 0.8s，确认 threading.Timer 在回调线程上下文不炸（OPEN_TIMEOUT 到点）")
        time.sleep(0.8)
        print(f"     此刻 phase={sess.state.phase.name}  mic_open_sent={sess.state.mic_open_sent}")

    t = threading.Thread(target=callback_thread, name="Dummy-7777", daemon=True)
    t.start()
    t.join(timeout=15)

    # 等主循环把队列里的写入跑完。
    # ⚠ 不能用固定 sleep —— CI 机器卡一下就会误判成"没发出去"（假红）。
    #   这里轮询到「条数够了」为止，最多等 5 秒。
    deadline = time.time() + 5.0
    while time.time() < deadline and len(sent) < len(expect):
        time.sleep(0.05)
    if len(sent) < len(expect) and expect:
        time.sleep(0.5)                 # 再给一次机会，然后照实判定

    print(f"\n  实际发出的命令序列: {sent}")
    ok = (sent == expect) and (sess.state.mic_open_sent == expect_sent_flag)
    if ok:
        print(f"  ✅ PASS —— 序列 {expect}，mic_open_sent={expect_sent_flag}")
    else:
        print(f"  ❌ FAIL —— 期望序列 {expect} / mic_open_sent={expect_sent_flag}"
              f"（实际 {sent} / {sess.state.mic_open_sent}）")
    return ok


def main() -> int:
    results = []
    results.append(case_static())
    results.append(run_case(
        "【正常路径】主循环在跑：MIC_OPEN 应该真的发出去两次（按下一次 + 松手补发一次）",
        broken=False, expect=["open", "open"], expect_sent_flag=True,
    ))
    results.append(run_case(
        "【故障路径】主循环已关（模拟 v1.0.3 的投递失败）："
        "序列应为空，且 mic_open_sent 保持 False → main.py 打 ERROR 而不是假成功",
        broken=True, expect=[], expect_sent_flag=False,
    ))
    # 对照在前、判据在后：先证明"这个定时器真会跑"，再说"close() 拦得住它"。
    results.append(run_retry_timer_case(do_close=False))
    results.append(run_retry_timer_case(do_close=True))
    results.append(run_shutdown_case())

    all_ok = all(results)
    print(f"\n{'='*72}")
    print("✅ 全部通过" if all_ok else "❌ 有用例失败")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
