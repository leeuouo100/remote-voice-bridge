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

用法：python tools/check_ble_callback_thread.py
（tools/check_all.py 会带上它 —— 这是防「MIC_OPEN 又悄悄发不出去」的回归闸）
"""
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atvv
from session import SessionCoordinator, Phase

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

    try:
        asyncio.run_coroutine_threadsafe(_write(), loop)
        return True
    except Exception as e:
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
    ok1 = run_case(
        "【正常路径】主循环在跑：MIC_OPEN 应该真的发出去两次（按下一次 + 松手补发一次）",
        broken=False, expect=["open", "open"], expect_sent_flag=True,
    )
    ok2 = run_case(
        "【故障路径】主循环已关（模拟 v1.0.3 的投递失败）："
        "序列应为空，且 mic_open_sent 保持 False → main.py 打 ERROR 而不是假成功",
        broken=True, expect=[], expect_sent_flag=False,
    )
    print(f"\n{'='*72}")
    print("✅ 全部通过" if (ok1 and ok2) else "❌ 有用例失败")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
