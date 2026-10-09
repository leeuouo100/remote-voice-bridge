"""连接等待窗口闸 —— 「等得够久 + 看得见在等 + 能被打断」。

为什么单独一条闸
----------------
2026-10-09 真机（v1.0.27 刚装上，用户报「连接就没那么流畅，我是关了软件再
重新连接，才连接得上」）。日志把三次连接原样记下来了：

    12:05:50 → 12:06:11  失败      20.85s   ← 撞上当时的 20 秒超时
    12:06:14 → 12:06:28  用户关掉软件  14.07s
    12:06:32 → 12:06:50  成功      18.18s   ← **余量只剩 1.8 秒**

也就是说：这台机器上「遥控器醒来 → 链路建起来」要 **18 秒上下**，而窗口只有
20 秒。用户看到的「不流畅、要关软件重开」是两件事叠起来的：

  ① 真慢（18 秒是 Windows/遥控器的物理时间，压不掉）；
  ② **我们在第 20 秒主动放弃了已经等到一半的进度**，然后从头再来。

这条闸钉四件事
--------------
1. **窗口要显著大于最坏观测值**（18.2s），不能贴着它 —— 见
   `_CONNECT_WAIT_SECONDS`。
2. **等待期间必须播报进度**（日志 + `state.last_event`）。用户看不见
   「它在等」就会以为卡死 —— 他正是第 14 秒去关软件的。
3. **等待必须能被打断**：托盘的「重新连接」只置 `_reconnect_request`，
   以前这里只看 `stop` ⇒ 点菜单**毫无反应**，用户以为菜单坏了。
   而且要**只清一次**：两边都清 → 调用方分不清"被打断"还是"真超时"；
   两边都不清 → 下一轮立刻又被打断 ⇒ **无限重连**。
4. **失败话术不许再劝人关软件**（用户已经把"关了重开"变成固定操作了）。

用法
----
    python tools/check_connect_wait.py

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 窗口的下限。实测最坏 18.18 秒 ⇒ 60 秒才是「3 倍余量」的及格线；
# 贴着 20 秒正是这次事故的成因。
MIN_WAIT_SECONDS = 60.0


def _code_only(src: str, keep_strings: bool = False) -> str:
    """剥掉注释与（默认还有）字符串字面量。

    ⚠ 本闸的注释里逐字引用了老话术与老数值（说明它们为什么错），
      不剥的话检查会把自己的注释当成违规。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "#":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith('"""', i) or src.startswith("'''", i):
            q = src[i:i + 3]
            j = src.find(q, i + 3)
            i = n if j < 0 else j + 3
        elif c in "\"'":
            q = c
            j = i + 1
            while j < n and src[j] != q:
                if src[j] == "\\":
                    j += 1
                j += 1
            if keep_strings:
                out.append(src[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _fn_body(src: str, name: str) -> str:
    """取模块级函数 `name` 的函数体（到下一个**顶格** def/class/@ 为止）。"""
    m = re.search(rf"^(?:async )?def {re.escape(name)}\(", src, re.M)
    if not m:
        return ""
    rest = src[m.end():]
    nxt = re.search(r"^(?:async )?def |^class |^@", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _wait_loop(body: str) -> str:
    """只取「等链路建起来」那个 while 循环（到函数尾），供静态判据用。

    ⚠ 必须切在 `while waited < timeout` 上 —— 函数前半段（建 GattSession、
      触发 GATT 访问）里也有 `stop` / `_reconnect_request` 之类的词，
      拿整个函数当判据会把"别处提过一嘴"当成"这里真的检查了"。
    """
    if "while waited < timeout" not in body:
        return ""
    return body.split("while waited < timeout", 1)[1]


# ── 1. 窗口必须显著大于最坏观测值 ───────────────────────────────────────────
def check_window(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")

    m = re.search(r"^_CONNECT_WAIT_SECONDS\s*=\s*([0-9.]+)", src, re.M)
    if not m:
        print("   ❌ main.py 里没有 _CONNECT_WAIT_SECONDS —— 窗口必须是一个"
              "有名字、有实测依据的常量，不能是个散落的字面量")
        return False
    secs = float(m.group(1))
    if secs < MIN_WAIT_SECONDS:
        print(f"   ❌ _CONNECT_WAIT_SECONDS = {secs:g}s，小于 {MIN_WAIT_SECONDS:g}s。"
              f"真机实测最坏 18.18s，20s 的窗口余量只剩 1.8s —— 这正是"
              f"2026-10-09「连接不流畅、要关软件重开」的成因")
        ok = False
    elif verbose:
        print(f"   OK 等待窗口 {secs:g}s（实测最坏 18.18s，余量 {secs / 18.18:.1f} 倍）")

    # 默认参数必须引用这个常量，不许再写死一个数字
    sig = re.search(r"async def _hold_ble_connection\(([^)]*)\)", src)
    if sig is None:
        print("   ❌ 找不到 _hold_ble_connection 的签名")
        return False
    if "_CONNECT_WAIT_SECONDS" not in sig.group(1):
        print(f"   ❌ _hold_ble_connection 的默认 timeout 没有引用 "
              f"_CONNECT_WAIT_SECONDS（当前签名：{sig.group(1).strip()}）——"
              f"常量改了默认值不跟着走，等于常量是摆设")
        ok = False
    elif verbose:
        print("   OK 默认 timeout 引用同一个常量（不会各自漂）")

    return ok


# ── 2. 等待期间：播报进度 + 能被「重新连接」打断 ────────────────────────────
def check_wait_loop(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    body = _fn_body(src, "_hold_ble_connection")
    if not body:
        print("   ❌ main.py 里找不到 _hold_ble_connection")
        return False

    loop = _wait_loop(body)
    if not loop:
        print("   ❌ 找不到 `while waited < timeout` 等待循环（改名了？闸门要跟着改）")
        return False
    code = _code_only(loop, keep_strings=True)

    # ① 播报进度：必须有节流常量 + 真的往 state 里写
    if "_CONNECT_WAIT_REPORT_EVERY" not in code:
        print("   ❌ 等待循环里没有按 _CONNECT_WAIT_REPORT_EVERY 节流地播报进度 ——"
              "用户看不见「它在等」就会以为卡死，然后去关软件重开")
        ok = False
    elif "state.update(" not in code:
        print("   ❌ 等待循环里没有 state.update(...) —— 只写日志不够，"
              "托盘 tooltip 与控制台读的是 state")
        ok = False
    elif verbose:
        print("   OK 等待期间会按节流把进度写进 state（托盘/控制台看得见）")

    # ② 可被「重新连接」打断
    if "_reconnect_request.is_set()" not in code:
        print("   ❌ 等待循环里没有检查 _reconnect_request —— 托盘的「重新连接」"
              "只置这个事件，不检查它的话，90 秒等待期间点菜单**毫无反应**")
        ok = False
    elif verbose:
        print("   OK 等待循环里检查了 _reconnect_request（菜单点得动）")

    # ③ 只在调用方清一次
    if "_reconnect_request.clear()" in code:
        print("   ❌ 等待循环里自己 clear 了 _reconnect_request —— 调用方就分不清"
              "「被重新连接打断」和「真超时」，会把用户点了菜单误报成超时")
        ok = False
    else:
        caller = _code_only(_fn_body(src, "_run_bridge_inner"), keep_strings=True)
        if "_reconnect_request.clear()" not in caller:
            print("   ❌ 没有任何地方 clear _reconnect_request —— 事件一直置位，"
                  "下一轮连接一进来又立刻被打断 ⇒ **无限重连**")
            ok = False
        elif verbose:
            print("   OK _reconnect_request 只在调用方清一次（不会无限重连）")

    return ok


# ── 3. 失败话术：别再劝人关软件 ─────────────────────────────────────────────
def check_message(verbose: bool = True) -> bool:
    ok = True
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    code = _code_only(_fn_body(src, "_run_bridge_inner"), keep_strings=True)

    if "不用" not in code or "关软件" not in code:
        print("   ❌ 连接失败的提示里没有明说「不用关软件」—— 2026-10-09 真机："
              "用户已经把「关了重开」变成固定操作了，而那样恰好把建链进度清零")
        ok = False
    elif verbose:
        print("   OK 失败提示里明说了「不用关软件」")

    # 老话术（"然后点托盘菜单里的「重新连接」"）不许还在
    if "点托盘菜单里的" in code:
        print("   ❌ 还在用老话术「点托盘菜单里的『重新连接』」—— 那是让用户"
              "再走一遍完整重建；新话术是「按遥控器任意键，本程序自己接上」")
        ok = False
    elif verbose:
        print("   OK 没有残留「点托盘菜单里的…」老话术")

    return ok


# ── 3b. 超时后**复用** device / GattSession（v1.0.29）──────────────────────
def check_reuse(verbose: bool = True) -> bool:
    """超时后不许把整轮连接丢掉重来。

    ⚠ 为什么单独一条：`_open_ble_device` / `GattSession` 这两步对
      「遥控器何时醒来」**毫无贡献** —— 建链的时间几乎全花在
      "等遥控器醒来 + Windows 去连它"上。超时后整轮重建 =
      **把已经等到一半的进度清零，从头再等**。
      2026-10-09 真机三次连接：失败 20.85s → 用户关软件 14.07s → 成功 18.18s。
    """
    ok = True
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")

    m = re.search(r"^_CONNECT_INNER_TRIES\s*=\s*(\d+)", src, re.M)
    if not m:
        print("   ❌ main.py 里没有 `_CONNECT_INNER_TRIES` —— 复用几轮必须是"
              "**有名字的常量**，否则下一个人不知道该动哪个数")
        return False
    tries = int(m.group(1))
    if tries < 2:
        print(f"   ❌ _CONNECT_INNER_TRIES = {tries} —— 等于没做复用"
              f"（超时后又回到「整轮重建、进度清零」）")
        ok = False
    elif verbose:
        print(f"   OK 一轮内最多再等 {tries} 轮（复用同一个 device / GattSession）")

    body = _code_only(_fn_body(src, "_run_bridge_inner"), keep_strings=True)
    if "_CONNECT_INNER_TRIES" not in body:
        print("   ❌ _run_bridge_inner 里没有用 _CONNECT_INNER_TRIES 的重试循环")
        return False

    # ⚠ 切在 `for _attempt ...` 上：函数里别处也提过 ble / device，
    #   拿整个函数当判据会把"别处提过一嘴"当成"这里真的重建了"。
    if "for _attempt in range(_CONNECT_INNER_TRIES)" not in body:
        print("   ❌ 找不到重试循环（`for _attempt in range(_CONNECT_INNER_TRIES)`）"
              "—— 改名了？闸门要跟着改")
        return False
    loop = body.split("for _attempt in range(_CONNECT_INNER_TRIES)", 1)[1]
    loop = loop.split("if not connected:", 1)[0]

    if "_open_ble_device" in loop:
        print("   ❌ 重试循环里又调了 `_open_ble_device` —— 那就还是「每轮重建」，"
              "等于把已经等到一半的进度清零（v1.0.29 修的就是这个）")
        ok = False
    elif "_hold_ble_connection" not in loop:
        print("   ❌ 重试循环里没有 `_hold_ble_connection`")
        ok = False
    elif verbose:
        print("   OK 重试循环里只调 _hold_ble_connection，**不重建** device / GattSession")

    return ok


# ── 4. 行为级：拿假 ble 真跑一遍 ────────────────────────────────────────────
class _FakeBle:
    """够 `_hold_ble_connection` 用的最小替身。

    `bluetooth_device_id=None` → 跳过 GattSession 那段（拿不到就降级，是设计好的）。
    """

    def __init__(self, status):
        self.bluetooth_device_id = None
        self.connection_status = status
        self.gatt_calls = 0

    async def get_gatt_services_async(self, *a):
        self.gatt_calls += 1
        return None


class _GattRes:
    def __init__(self, status, services):
        self.status = status
        self.services = services


class _FakeBleGattOk(_FakeBle):
    """`connection_status = CONNECTED` **并且** GATT 真读得通。

    ⚠ 这条是给 B4 用的。如果只把 `connection_status` 设成 CONNECTED、
      GATT 探针却读不通，那按 v1.0.27 的设计**就该继续等**（正是「假连接」），
      B4 会等到最后一搏才返回 —— 那是**对的行为**，拿它断言"立刻返回"
      会把正确的代码判红（本闸第一版就是这么错的）。
    """

    def __init__(self, status, ok_value):
        super().__init__(status)
        self._ok = ok_value

    async def get_gatt_services_async(self, *a):
        self.gatt_calls += 1
        return _GattRes(self._ok, ["svc"])


def check_behavior(verbose: bool = True) -> bool:
    ok = True
    try:
        import main
        import state
        from winrt.windows.devices.bluetooth import BluetoothConnectionStatus
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus,
        )
    except Exception as e:                                # noqa: BLE001
        print(f"   ❌ import 失败：{type(e).__name__}: {e}")
        return False

    DISC = BluetoothConnectionStatus.DISCONNECTED
    CONN = BluetoothConnectionStatus.CONNECTED
    # ⚠ 用**真实**枚举值，别硬编码（SUCCESS 在 UWP 里是 0 不是 1）
    OKV = int(GattCommunicationStatus.SUCCESS)

    def _reset_events():
        main._stop_request.clear()
        main._reconnect_request.clear()

    # B1：没连上就老老实实等满 timeout（别为了"能打断"把正常路径弄成秒退）
    _reset_events()
    t0 = time.time()
    got = asyncio.run(main._hold_ble_connection(_FakeBle(DISC), timeout=2.0, stop=None))
    dt = time.time() - t0
    if got[0] is not False:
        print(f"   ❌ B1 没连上时应返回 False，实际 {got[0]!r}")
        ok = False
    elif not (1.9 <= dt <= 3.5):
        print(f"   ❌ B1 timeout=2.0 时等了 {dt:.2f}s（应 ≈2.0s）——"
              f"要么没等、要么等过头")
        ok = False
    elif verbose:
        print(f"   OK B1 没连上会等满窗口（{dt:.2f}s / timeout=2.0s）")

    # B2：stop 已置位 → 立刻返回
    _reset_events()
    ev = threading.Event()
    ev.set()
    t0 = time.time()
    got = asyncio.run(main._hold_ble_connection(_FakeBle(DISC), timeout=20.0, stop=ev))
    dt = time.time() - t0
    if not (dt < 2.0 and got[0] is False):
        print(f"   ❌ B2 stop 已置位时没有立刻返回（{dt:.2f}s, {got[0]!r}）——"
              f"用户点「退出」不该还干等满窗口")
        ok = False
    elif verbose:
        print(f"   OK B2 stop 已置位 → 立刻返回（{dt:.2f}s）")

    # B3：**「重新连接」要能立刻打断**（这是本次新加的能力）
    _reset_events()
    main._reconnect_request.set()
    t0 = time.time()
    got = asyncio.run(main._hold_ble_connection(_FakeBle(DISC), timeout=20.0, stop=None))
    dt = time.time() - t0
    if dt >= 2.0:
        print(f"   ❌ B3 _reconnect_request 置位时等了 {dt:.2f}s —— 托盘的"
              f"「重新连接」在等待期间点不动，用户会以为菜单坏了")
        ok = False
    elif got[0] is not False:
        print(f"   ❌ B3 被打断时应返回 False，实际 {got[0]!r}")
        ok = False
    elif not main._reconnect_request.is_set():
        print("   ❌ B3 等待循环把 _reconnect_request 自己清了 —— 调用方就分不清"
              "「被重新连接打断」和「真超时」")
        ok = False
    elif verbose:
        print(f"   OK B3 「重新连接」能立刻打断（{dt:.2f}s），且事件留给调用方清")

    # B4：已连接 **且 GATT 真读得通** → 立刻返回 True（正常路径没被改坏）
    _reset_events()
    t0 = time.time()
    got = asyncio.run(main._hold_ble_connection(
        _FakeBleGattOk(CONN, OKV), timeout=20.0, stop=None))
    dt = time.time() - t0
    if got[0] is not True or dt >= 2.0:
        print(f"   ❌ B4 已连接且 GATT 读得通时应立刻返回 True"
              f"（实际 {got[0]!r}，{dt:.2f}s）")
        ok = False
    elif verbose:
        print(f"   OK B4 已连接 + GATT 通 → 立刻返回 True（{dt:.2f}s）")

    # B4b 对照：CONNECTED 但 GATT **读不通** ⇒ 必须继续等（那正是「假连接」）。
    #      没有这条，B4 可能只是"看到 CONNECTED 就 True"的假绿。
    _reset_events()
    t0 = time.time()
    got = asyncio.run(main._hold_ble_connection(
        _FakeBle(CONN), timeout=1.5, stop=None))
    dt = time.time() - t0
    if dt < 1.4:
        print(f"   ❌ B4b CONNECTED 但 GATT 读不通时只等了 {dt:.2f}s —— "
              f"那就等于把「假连接」当真连接了（v1.0.27 修的就是这个）")
        ok = False
    elif verbose:
        print(f"   OK B4b 对照：CONNECTED 但 GATT 读不通 → 继续等（{dt:.2f}s）")

    # B5：等待期间**真的**往 state 里写了进度（证明播报那条路走得通）
    _reset_events()
    state.update(last_event="")
    old_every = main._CONNECT_WAIT_REPORT_EVERY
    main._CONNECT_WAIT_REPORT_EVERY = 0.5          # 只为让这条测试跑得快
    try:
        asyncio.run(main._hold_ble_connection(_FakeBle(DISC), timeout=2.0, stop=None))
    finally:
        main._CONNECT_WAIT_REPORT_EVERY = old_every
    ev_txt = state.get().last_event or ""
    if "等遥控器" not in ev_txt:
        print(f"   ❌ B5 等待期间没有把进度写进 state.last_event（当前 {ev_txt!r}）——"
              f"托盘 tooltip / 控制台就还是显示不出「正在等」")
        ok = False
    elif verbose:
        print(f"   OK B5 等待期间 state.last_event = {ev_txt!r}")

    _reset_events()
    return ok


# ── 5. 反例自证 ─────────────────────────────────────────────────────────────
def run_counter_examples() -> bool:
    ok = True
    print("\n── 反例自证（判据真的会红吗）──")
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")

    # 反例 1：窗口退回 20 秒 → check_window 的数值判据必须红
    broken = re.sub(r"^_CONNECT_WAIT_SECONDS\s*=\s*[0-9.]+",
                    "_CONNECT_WAIT_SECONDS = 20.0", src, count=1, flags=re.M)
    m = re.search(r"^_CONNECT_WAIT_SECONDS\s*=\s*([0-9.]+)", broken, re.M)
    if m is None or float(m.group(1)) >= MIN_WAIT_SECONDS:
        print("   ❌ [反例] 把窗口改回 20s 后仍能通过 → 数值判据无效")
        ok = False
    else:
        print("   OK [反例] 窗口改回 20s → 数值判据变红")

    # 反例 2：删掉「重新连接」那个检查 → 可打断判据必须红
    # ⚠ 锚点必须落在**代码**上：注释里也写着 `_reconnect_request.is_set()`。
    anchor = ("        if _reconnect_request.is_set():\n"
              "            logger.info(\"♻️ 连接等待期间收到「重新连接」请求 → 提前结束，立刻重建\")\n")
    broken2 = src.replace(anchor, "")
    if broken2 == src:
        print("   ❌ [反例] 找不到「重新连接」检查的锚点（闸门锚点过期了）")
        ok = False
    else:
        loop2 = _code_only(
            _wait_loop(_fn_body(broken2, "_hold_ble_connection")), keep_strings=True)
        if "_reconnect_request.is_set()" in loop2:
            print("   ❌ [反例] 删掉「重新连接」检查后仍判成有 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 删掉「重新连接」检查 → 可打断判据变红")

    # 反例 3：删掉进度播报 → 播报判据必须红
    broken3 = src.replace(
        "        if waited >= next_report:\n"
        "            next_report += _CONNECT_WAIT_REPORT_EVERY\n", "")
    if broken3 == src:
        print("   ❌ [反例] 找不到进度播报的锚点（闸门锚点过期了）")
        ok = False
    else:
        loop3 = _code_only(
            _wait_loop(_fn_body(broken3, "_hold_ble_connection")), keep_strings=True)
        if "_CONNECT_WAIT_REPORT_EVERY" in loop3:
            print("   ❌ [反例] 删掉进度播报后仍判成有 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 删掉进度播报 → 播报判据变红")

    # 反例 4：注释里引用老话术 / 老数值不许被误判
    probe = ('# 老话术：然后点托盘菜单里的「重新连接」\n'
             '# 老数值：_CONNECT_WAIT_SECONDS = 20.0\n')
    if "点托盘菜单里的" in _code_only(probe):
        print("   ❌ [反例] 注释里的老话术被误判 → 闸门会对着自己的注释报红")
        ok = False
    else:
        print("   OK [反例] 注释里引用老话术不会被误判（_code_only 生效）")

    # 反例 5：正向断言必须 keep_strings=True（「不用关软件」在字符串里）
    probe2 = '    "   **不用**关软件重开。"\n'
    if "不用" in _code_only(probe2):
        print("   ❌ [反例] 默认模式保留了字符串字面量 → 两种模式没区别")
        ok = False
    elif "不用" not in _code_only(probe2, keep_strings=True):
        print("   ❌ [反例] keep_strings=True 也没保住字符串 → 正向断言不可用")
        ok = False
    else:
        print("   OK [反例] 两种模式确有区别（正向断言必须 keep_strings=True）")

    # 反例 6：把 _open_ble_device 塞回重试循环 → 复用判据必须红
    anchor6 = "        connected, ble_session = await _hold_ble_connection(ble, stop=stop)\n"
    if anchor6 not in src:
        print("   ❌ [反例] 找不到重试循环里那行 _hold_ble_connection（闸门锚点过期了）")
        ok = False
    else:
        broken6 = src.replace(
            anchor6,
            "        ble = await _open_ble_device(dev_info)\n" + anchor6, 1)
        body6 = _code_only(_fn_body(broken6, "_run_bridge_inner"), keep_strings=True)
        loop6 = body6.split("for _attempt in range(_CONNECT_INNER_TRIES)", 1)[1]
        loop6 = loop6.split("if not connected:", 1)[0]
        if "_open_ble_device" not in loop6:
            print("   ❌ [反例] 循环里塞回重建却判不出来 → 判据无效")
            ok = False
        else:
            print("   OK [反例] 循环里塞回 `_open_ble_device` → 复用判据变红")

    return ok


def main() -> int:
    print("=" * 60)
    print("连接等待闸 —— 等得够久 + 看得见在等 + 能被打断")
    print("=" * 60)

    print("\n── 1. 窗口显著大于最坏观测值（18.18s）──")
    ok = check_window()

    print("\n── 2. 等待循环：播报进度 + 可被「重新连接」打断 ──")
    ok = check_wait_loop() and ok

    print("\n── 3. 失败话术：别再劝人关软件 ──")
    ok = check_message() and ok

    print("\n── 3b. 超时后复用 device / GattSession（不重建）──")
    ok = check_reuse() and ok

    print("\n── 4. 行为级：假 ble 跑六种情形 ──")
    ok = check_behavior() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
