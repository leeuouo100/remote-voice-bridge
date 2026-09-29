"""托盘退出必须让桥线程「优雅收尾」的回归闸 —— 2026-09-29 审查报告 P1-4。

背景
====
托盘点「退出」时原先只 `_stop.set()`（托盘模块自己的事件），而
`run_bridge` 的主循环**根本不看它**。桥线程是 daemon，主进程一结束就被
直接掐掉 —— `finally` 里的清理（关 GattSession / 停 Frida 注入 / 关音频流）
**不保证执行**。

现象是"退出再开就连不上"，而日志里看不出任何异常 —— 因为异常根本没机会写。
另一处同类问题：`_hold_ble_connection` 里有 20 秒的连接等待，退出时不该还要
在那儿干等（托盘 join 有超时，等超了清理照样被掐）。

这道闸钉四件事（每条都配反例）：

  A. **可打断的等待**（动态，真跑）：`_hold_ble_connection(stop=…)` 在 stop
     已置位 / 中途置位时都要立刻返回，而不是干等满 20 秒
     A0 反例：不传 stop 时**确实**会等满 timeout（证明 A 不是"本来就快"）
  B. 桥循环收 `stop_event`，且**主循环每一轮最前面**就检查它
     （实体是 `_run_bridge_inner`；`run_bridge` 只是壳，B0/B7 盯壳有没有转交）
  C. 托盘把 `_stop` 真的传下去，并且 `_quit` 会 **join** 桥线程
     （join 不到要有痕迹，不许静默）
  D. `request_stop()` 把两个事件都置起来（连接阶段也能被叫醒）

用法： python tools/check_graceful_stop.py
输出： GRACEFUL STOP OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

# 沙箱：import main 会建日志文件，别写进用户真实的 AppData
_SANDBOX = tempfile.mkdtemp(prefix="rvb-stop-")
os.environ["APPDATA"] = _SANDBOX
os.environ["RVB_NO_AUTO_CONSOLE"] = "1"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILS: list[str] = []
PASSES = 0


def check(cond, msg) -> bool:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
    return bool(cond)


# ── A. 可打断的等待（动态）──────────────────────────────────────────────────
class FakeBle:
    """够 `_hold_ble_connection` 用的最小替身。

    `bluetooth_device_id=None` → 跳过 GattSession 那段（拿不到就降级，是设计好的）；
    `connection_status` 一直是 DISCONNECTED → 走到那个 20 秒等待循环。
    """

    def __init__(self):
        self.bluetooth_device_id = None
        self.connection_status = None      # 在 case_a 里填真的枚举值
        self.gatt_calls = 0

    async def get_gatt_services_async(self):
        self.gatt_calls += 1
        return None


def case_a() -> None:
    import main
    from winrt.windows.devices.bluetooth import BluetoothConnectionStatus

    def make() -> FakeBle:
        b = FakeBle()
        b.connection_status = BluetoothConnectionStatus.DISCONNECTED
        return b

    # A1：stop 已置位 → 立刻返回，绝不干等满 timeout
    ev = threading.Event()
    ev.set()
    t0 = time.time()
    ok, sess = asyncio.run(main._hold_ble_connection(make(), timeout=20.0, stop=ev))
    dt = time.time() - t0
    check(dt < 2.0, f"A1 stop 已置位时立刻返回（实际 {dt:.2f}s，timeout=20s）")
    check(ok is False, "A1b 没连上就该返回 False")
    check(sess is None, "A1c 拿不到 GattSession 时返回 None（降级路径）")

    # A0 反例：不传 stop、给个短 timeout → 真的会等满。
    #     没有这条，A1 可能是"这个函数本来就立刻返回"的假绿。
    t0 = time.time()
    asyncio.run(main._hold_ble_connection(make(), timeout=1.5, stop=None))
    dt0 = time.time() - t0
    check(dt0 >= 1.4, f"A0 反例：不传 stop 时会等满 timeout（实际 {dt0:.2f}s ≥ 1.5s）")
    check(dt0 > dt + 1.0,
          f"A0b 反例对比成立：带 stop({dt:.2f}s) 明显快于不带({dt0:.2f}s)")

    # A2：stop **中途**置位也要能提前跳出
    ev2 = threading.Event()
    threading.Timer(0.8, ev2.set).start()
    t0 = time.time()
    asyncio.run(main._hold_ble_connection(make(), timeout=20.0, stop=ev2))
    dt2 = time.time() - t0
    check(dt2 < 4.0, f"A2 stop 中途置位也能提前跳出（实际 {dt2:.2f}s，timeout=20s）")

    # A3：链路真的连上时必须返回 True（别为了能打断把正常路径弄坏）
    b = make()
    b.connection_status = BluetoothConnectionStatus.CONNECTED
    ok3, _ = asyncio.run(main._hold_ble_connection(b, timeout=20.0, stop=None))
    check(ok3 is True, "A3 已连接时立刻返回 True（正常路径没被改坏）")

    # ── D. request_stop 两个事件都置起来 ───────────────────────────────────
    main._stop_request.clear()
    main._reconnect_request.clear()
    main.request_stop()
    check(main._stop_request.is_set(), "D1 request_stop 置了 _stop_request")
    check(main._reconnect_request.is_set(),
          "D2 request_stop 也置了 _reconnect_request（连接阶段的等待才叫得醒）")
    main._stop_request.clear()
    main._reconnect_request.clear()


# ── 静态部分 ────────────────────────────────────────────────────────────────
def _fn_src(tree: ast.AST, src: str, name: str) -> str | None:
    """取函数的源码。

    ⚠ 必须同时认 `FunctionDef` 和 `AsyncFunctionDef` —— `run_bridge` /
      `_hold_ble_connection` 都是 `async def`，只认前者会"找不到函数"，
      然后一整套断言全红，看着像代码有问题，其实是闸门自己看漏了。
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return None


def _main_loop_head(fn_src: str, n: int = 2) -> str:
    """取**主循环**体的头 n 条语句的源码。

    主循环的指纹：里面有一条 `ran_ok = True`（"这一轮正常结束"）。
    run_bridge 里还有别的 `while True`（音频重连那些），靠行号取会取错。

    ⚠ 为什么盯"头两条"而不是"整个循环里出现过"：检查写在循环末尾时，
    用户点了退出还要再等一整轮（中间还可能有 await 卡住），等于没及时退出。
    """
    fn = ast.parse(fn_src).body[0]
    for node in ast.walk(fn):
        if not isinstance(node, ast.While):
            continue
        is_main = any(
            isinstance(s, ast.Assign)
            and any(getattr(t, "id", "") == "ran_ok" for t in s.targets)
            and getattr(s.value, "value", None) is True
            for s in ast.walk(node)
        )
        if is_main:
            return "\n".join(ast.get_source_segment(fn_src, s) or ""
                             for s in node.body[:n])
    return ""


def case_static() -> None:
    main_src = (REPO / "main.py").read_text(encoding="utf-8")
    tray_src = (REPO / "tray_app.py").read_text(encoding="utf-8")
    mtree = ast.parse(main_src)
    ttree = ast.parse(tray_src)

    # ── B. 桥循环收 stop_event，主循环看得见 ─────────────────────────────
    #
    # ⚠ 2026-09-29（P1-3）之后 `run_bridge` 只是一层壳，真正的主循环在
    #   `_run_bridge_inner` 里。查壳等于什么都没查（B3 会取到空串）——
    #   这正是"重构把闸门悄悄弄瞎"的典型：闸门还亮着绿灯，代码已经搬走了。
    #   所以实体定位一律指 `_run_bridge_inner`，另用 B0/B7 盯壳有没有把
    #   参数**真的传下去**（壳与实体脱节 = stop 传不进去 = P1-4 复发）。
    shell = _fn_src(mtree, main_src, "run_bridge")
    rb = _fn_src(mtree, main_src, "_run_bridge_inner")
    check(shell is not None, "B0 run_bridge() 这层壳还在")
    check(rb is not None,
          "B0b 找得到 _run_bridge_inner()（真正跑主循环的那一层）")
    check(rb is not None and "stop_event" in rb.split('"""')[0] + rb[:400],
          "B1 _run_bridge_inner 的签名里有 stop_event")
    check(rb is not None and "stop.is_set()" in rb,
          "B2 _run_bridge_inner 里真的检查了 stop.is_set()")

    head = _main_loop_head(rb or "")
    check("stop.is_set()" in head,
          f"B3 **主循环头两条语句**里就有退出检查（实际：{head!r}）")
    # B0a 反例：把检查挪到循环末尾的写法，必须被判不合格
    fake = ("async def _run_bridge_inner(stop_event=None):\n"
            "    stop = stop_event\n"
            "    ran_ok = False\n"
            "    try:\n"
            "        while True:\n"
            "            await asyncio.sleep(0.2)\n"
            "            ran_ok = True\n"
            "            if stop.is_set():\n"
            "                break\n"
            "    finally:\n"
            "        pass\n"
            "    return ran_ok\n")
    fake_head = _main_loop_head(
        _fn_src(ast.parse(fake), fake, "_run_bridge_inner") or "")
    check(fake_head and "stop.is_set()" not in fake_head,
          f"B0a 反例：检查写在循环末尾 → B3 判不合格（不是「只要出现过就算」）"
          f"（实际取到 {fake_head!r}）")

    # ── B4. _hold_ble_connection 接受 stop 且等待循环里看它 ────────────────
    hb = _fn_src(mtree, main_src, "_hold_ble_connection")
    check(hb is not None and "stop" in (hb or "").split("\n")[0],
          "B4 _hold_ble_connection 的签名里有 stop")
    check(hb is not None and "stop.is_set()" in hb,
          "B5 那个 20 秒等待循环里真的看 stop（否则退出时要干等）")
    check(rb is not None and "_hold_ble_connection(ble, stop=stop)" in rb,
          "B6 _run_bridge_inner 把 stop 传给了 _hold_ble_connection")

    # ── B7. 壳必须把 stop_event 真的转交下去 ──────────────────────────────
    # 壳里漏传 = 外面点退出、里面看不见，闸门 B1/B2/B3 却全绿（查的是实体）。
    check(shell is not None
          and "_run_bridge_inner(" in shell
          and "stop_event" in shell.split("_run_bridge_inner(", 1)[1].split(")", 1)[0],
          "B7 壳 run_bridge 把 stop_event 转交给了 _run_bridge_inner")
    # B7a 反例：壳漏传 stop_event（改传 None），B7 必须判不合格
    fake_shell = ("async def run_bridge(device_type=None, name_hint=None, stop_event=None):\n"
                  "    res = _BridgeResources()\n"
                  "    try:\n"
                  "        res.ran_ok = await _run_bridge_inner(device_type, name_hint, None, res)\n"
                  "        return res.ran_ok\n"
                  "    finally:\n"
                  "        await res.teardown()\n")
    fs = _fn_src(ast.parse(fake_shell), fake_shell, "run_bridge") or ""
    check(not ("stop_event" in fs.split("_run_bridge_inner(", 1)[1].split(")", 1)[0]),
          "B7a 反例：壳漏传 stop_event（传 None）→ B7 判不合格（不是「壳里出现过这个词就算」）")

    # ── C. 托盘真的传下去 + 退出时 join ───────────────────────────────────
    check("run_bridge(stop_event=_stop)" in tray_src,
          "C1 _bridge_worker 把托盘的 _stop 作为 stop_event 传给了 run_bridge")
    quit_src = _fn_src(ttree, tray_src, "_quit")
    check(quit_src is not None, "C2 找得到 _quit()")
    check(quit_src is not None and "join(" in quit_src,
          "C3 _quit 会 join 桥线程（等它走完 finally 再放进程走）")
    check(quit_src is not None and "request_stop()" in quit_src,
          "C4 _quit 也调 main.request_stop()（正在跑的那一轮也能看见）")
    check(quit_src is not None and "is_alive()" in quit_src
          and ("⚠" in quit_src or "没退出" in quit_src),
          "C5 join 超时**要有痕迹**（不许静默放它走）")
    # C0 反例：老写法（只 set + icon.stop，不 join）必须被判不合格
    old_quit = ("def _quit(icon, item):\n"
                "    _stop.set()\n"
                "    state.reset()\n"
                "    icon.stop()\n")
    oq = _fn_src(ast.parse(old_quit), old_quit, "_quit")
    check(oq is not None and "join(" not in oq,
          "C0a 反例：老写法没有 join → C3 判不合格")
    check(oq is not None and "request_stop()" not in oq,
          "C0b 反例：老写法没叫醒主循环 → C4 判不合格")

    # ── C6. 桥线程引用被留着（否则没得 join）─────────────────────────────
    check("_bridge_thread" in tray_src and "global _icon, _bridge_thread" in tray_src,
          "C6 托盘留了 _bridge_thread 引用（否则 _quit 无从 join）")


def main() -> int:
    for fn in (case_a, case_static):
        try:
            fn()
        except Exception as e:                   # noqa: BLE001
            import traceback
            FAILS.append(f"{fn.__name__} 抛异常：{type(e).__name__}: {e}"
                         f"\n{traceback.format_exc()[-600:]}")

    shutil.rmtree(_SANDBOX, ignore_errors=True)

    for m in FAILS:
        print(f"  FAIL {m}")
    if FAILS:
        print(f"GRACEFUL STOP FAILED（{len(FAILS)} 项）")
        print("  提示：托盘点「退出」必须让桥线程自己走完 finally。")
        print("        daemon 线程被直接掐掉 = GattSession / 注入 / 音频流没清理，")
        print("        现象是「退出再开就连不上」，而日志里一个字都没有。")
        return 1
    print(f"  OK   {PASSES} 项全过（可打断等待 / 主循环看得见 / 传下去 / join 有痕迹）")
    print("GRACEFUL STOP OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
