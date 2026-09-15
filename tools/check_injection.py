"""
注入自检 —— 验证我们发的组合键，Windows 是不是真的认了。

两套测量并行，因为对不同的键，"认了"的定义不一样：

  · **键盘钩子看到了事件**（WH_KEYBOARD_LL）
      这是输入法/热键程序真正消费的东西。所有用例都必须过这一项。
  · **系统记录了按下状态**（GetAsyncKeyState）
      给那些"长按要一直认为它按着"的场景用。没有 Win 的组合必须过这一项。

为什么要分开：**Win+Ctrl 是 Windows 自己的系统和弦，注入这种组合时系统会动手脚。**
用 `--raw` 能看到真相 —— Ctrl 已经按下之后再注入 Win，钩子收到的是

    vk=0xFC/scan=0x00/f=0x10        ← 不是 0x5B！

也就是系统把这个 Win 从钩子里藏掉了。而输入法认的就是钩子事件，认不到 0x5B
就等于没按。同时 GetAsyncKeyState 也只留得住 Ctrl 和 Win 里的一个。
所以带 Win 的组合：**只认「钩子收到货真价实的 0x5B」**，状态位不作判据。

踩过的两个真实坑，都在这里留了用例：
  ① `win` 早前走 `wVk=0x5B + KEYEVENTF_EXTENDEDKEY`：钩子看得见事件、
     GetAsyncKeyState 查不到 —— Win 根本没按住。改成走扫描码才正常。
  ② 顺序不能反：**Ctrl 先落下，后面的 Win 就会被换脸成 0xFC**（间隔 0~80ms
     都一样，钩子里 0/8 收得到 0x5B）。Win 排到最前面才稳（8/8）。
     所以本文件必须走 `keys.hotkey_down()`（生产路径），不能自己手写
     一遍下发循环 —— 手写的那份不会用 order_press，测出来是绿的、
     线上却是坏的。

用法： python tools/check_injection.py [--raw]
输出： OK / FAIL / SKIPPED（非 Windows / 查不到键盘状态）
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402  （下面是中文输出，先钉住编码）

_setup_utf8()

if not hasattr(ctypes, "WinDLL"):
    print("SKIPPED（非 Windows）")
    raise SystemExit(0)

import keys  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
_GetAsyncKeyState = user32.GetAsyncKeyState
_GetAsyncKeyState.restype = ctypes.c_short
_GetAsyncKeyState.argtypes = [ctypes.c_int]

# 探测项 → (查状态用的 VK, 钩子里算同款的 VK 集合 / None=钩子那一侧不做判据)
#
# ⚠ 键码要分"通用"和"左/右分体"两套，别混用：
#   通用名（ctrl/shift/alt）注入的是 VK_CONTROL(0x11) / VK_SHIFT(0x10) / VK_MENU(0x12)，
#   查状态也必须查**通用码**；拿 VK_LSHIFT(0xA0) 去查一个通用 0x10 的按下，
#   结果是不确定的（时有时无）—— 早前这个检查就是这么误报的。
#   左/右分体的键（ralt/lalt/rwin/lwin）才查 0xA0/0xA2… 那套。
#
# 钩子那一列写 None 的意思：**这个键的 vkCode 在钩子里不稳定**，不拿它当判据。
#   现在只有 Alt 的通用名是这种（同一个扫描码 0x38，钩子里报 0x12 还是别的不好说），
#   所以通用 Alt 只查状态位 —— 反正那一条要证明的只是"Alt 按下能生效"。
#   左右分体的 lalt/ralt 用 --raw 量到是稳定的 0xA4/0xA5，照常查钩子。
#   反过来，Ctrl/Shift/Win 的钩子 vkCode 也是稳定的（实测多次一致），必须查：
#   带 Win 的组合**只有钩子事件靠得住**（原因见文件头）。
PROBES: dict[str, tuple[int, tuple[int, ...] | None]] = {
    "Ctrl":     (0x11, (0xA2, 0xA3, 0x11)),
    "左 Ctrl":  (0xA2, (0xA2,)),
    "右 Ctrl":  (0xA3, (0xA3,)),
    "Win":      (0x5B, (0x5B,)),
    "右 Win":   (0x5C, (0x5C,)),
    "Alt":      (0x12, None),
    "左 Alt":   (0xA4, (0xA4,)),        # --raw 实测：vk=0xA4/scan=0x38/f=0x30
    "右 Alt":   (0xA5, (0xA5,)),        # --raw 实测：vk=0xA5/scan=0x38/f=0x31
    "Shift":    (0x10, (0xA0, 0xA1, 0x10)),
    "左 Shift": (0xA0, (0xA0,)),
    "右 Shift": (0xA1, (0xA1,)),
}

_HOOK_NAME = {}                      # vk → 探测名（只收关心的键）
for _n, (_st, _hks) in PROBES.items():
    for _vk in (_hks or ()):
        _HOOK_NAME.setdefault(_vk, _n)

# (要下发的组合, 按下后必须成立的探测项)
CASES = [
    (["ctrl", "win"],          ["Ctrl", "Win"]),      # ← 坑 ② 的回归用例
    (["win", "ctrl"],          ["Ctrl", "Win"]),      # 顺序写反了也必须能成
    (["ctrl", "win", "shift"], ["Ctrl", "Win", "Shift"]),
    (["ralt"],                 ["右 Alt"]),           # 豆包输入法默认键
    (["lalt"],                 ["左 Alt"]),
    (["rwin"],                 ["右 Win"]),
    (["lwin"],                 ["Win"]),
    (["ctrl", "shift"],        ["Ctrl", "Shift"]),
]

# 组合里只要带 Win，就不要拿状态位去卡 —— 实测 Ctrl 和 Win 同时按住时，
# 系统只会让它俩里的一个出现在 GetAsyncKeyState 里（谁先按谁留下；
# Ctrl+Shift+Win 时还会轮到 Shift 消失）。这是 Windows 把 Win+Ctrl 当系统
# 和弦处理的结果，不是注入坏了。钩子在所有情况下都收到完整事件，
# 所以带 Win 的组合以钩子事件为准。
_WIN_KEYS = {"win", "lwin", "rwin", "cmd", "command", "windows"}

_SETTLE = 0.30

# 每个用例最多跑几轮。
#
# 为什么要重试：低级键盘钩子（WH_KEYBOARD_LL）有超时机制 —— 回调线程一旦
# 响应不及时，系统会直接跳过这次回调，事件就"丢"了。机器忙的时候
# （比如同时跑着控制台服务和浏览器）很容易撞上，表现成随机的假 FAIL。
# 而真正的回归（比如组合键顺序被改回 Ctrl 在前）是**每一轮都挂**的，
# 所以重试不会掩盖真问题，只会滤掉"机器忙"这种噪声。
#
# 为什么是 6 而不是 3：这个脚本会**真的往系统里注入按键**，读的是系统实时状态，
# 是整套自检里唯一受机器负载影响的一关。实测同一份代码连跑两次，
# 一次报 2 项 FAIL、一次全过。多给 3 轮（每轮约 0.5 秒）就能把
# "机器忙"和"代码坏了"分开；反过来，一个随机的假 FAIL 会让人去改
# 本来没错的代码，那个代价大得多。
_ATTEMPTS_HOOK = 6

# 顺序错了就一定挂 —— 光靠 CASES 只能测"结果对不对"，
# 这条直接盯住 order_press 这个函数本身，改坏了立刻红。
ORDER_CASES = [
    (["ctrl", "win"],          ["win", "ctrl"]),
    (["shift", "ctrl", "win"], ["win", "ctrl", "shift"]),
    (["alt", "lwin"],          ["lwin", "alt"]),
    (["ctrl", "shift", "m"],   ["ctrl", "shift", "m"]),   # 主键不参与排序
    (["ralt", "space"],        ["ralt", "space"]),
]


# ── 低级键盘钩子 ──────────────────────────────────────────────────────────────
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


_HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                             wintypes.WPARAM, ctypes.POINTER(_KBDLLHOOKSTRUCT))
_hook_seen: set[str] = set()
_raw: list[str] = []                 # --raw 时记录原始 vk/scan，用来查盲点
_RAW = "--raw" in sys.argv
_hook = None


def _hook_proc(code, wparam, lparam):
    if code >= 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        kb = lparam.contents
        name = _HOOK_NAME.get(kb.vkCode)
        if name:
            _hook_seen.add(name)
        if _RAW:
            _raw.append(f"vk=0x{kb.vkCode:02X}/scan=0x{kb.scanCode:02X}/f=0x{kb.flags:X}")
    return user32.CallNextHookEx(None, code, wparam, lparam)


_hook_cb = _HOOKPROC(_hook_proc)
_hook_ready = threading.Event()


def _pump_thread() -> None:
    """专门一条线程装钩子 + 阻塞取消息。

    为什么不能在主线程里 `PeekMessage + time.sleep(4ms)` 轮询：
    低级键盘钩子的回调是在**装钩子那条线程取消息的时候**被调用的。
    Windows 的 sleep 精度只有 ~15ms（默认时钟粒度），轮询一睡就把回调拖住；
    拖过 LowLevelHooksTimeout 之后系统会**直接跳过这次回调**，事件就丢了 ——
    表现成"钩子随机会漏事件"，机器一忙更明显。
    `GetMessageW` 是阻塞等待，回调在这段等待里被调用，零轮询延迟。
    """
    global _hook
    _hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_cb, None, 0)
    _hook_ready.set()
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _pump(sec: float) -> None:
    """主线程这边只需要等 —— 取消息由 _pump_thread 负责。"""
    time.sleep(sec)


def _pressed() -> set:
    return {n for n, (vk, _h) in PROBES.items() if _GetAsyncKeyState(vk) & 0x8000}


# 单独按一下 Win（不带别的键）会弹出开始菜单 —— 这是系统行为，不是 bug。
# 但这是一条自检脚本，不该在用户干活的时候把开始菜单糊到脸上。
_GetForegroundWindow = user32.GetForegroundWindow
_GetForegroundWindow.restype = ctypes.c_void_p
_GetClassNameW = user32.GetClassNameW


def _dismiss_start() -> None:
    hwnd = _GetForegroundWindow()
    if not hwnd:
        return
    buf = ctypes.create_unicode_buffer(256)
    _GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
    if "corewindow" in buf.value.lower() or "startmenu" in buf.value.lower():
        keys._send_key("esc", up=False)
        keys._send_key("esc", up=True)
        _pump(0.08)


# 校准用的无害键：F13 在普通键盘上根本不存在，也没有任何程序会绑它，
# 但 SendInput 下发之后系统照样会记录它的按下状态。
_CALIB_VK = 0x7C          # VK_F13


def _calibrate() -> bool:
    """先确认"注入 → 查状态"这条链路本身是通的。

    无桌面会话（CI runner、远程服务）里 SendInput 可能不生效，
    不先校准的话会把环境问题误报成"注入有 bug"。
    """
    keys._send_key("f13", up=False)
    _pump(0.12)
    ok = bool(_GetAsyncKeyState(_CALIB_VK) & 0x8000)
    keys._send_key("f13", up=True)
    _pump(0.08)
    return ok


def _check_order() -> list[str]:
    fails = []
    for combo, want in ORDER_CASES:
        got = keys.order_press(list(combo))
        if got != want:
            fails.append(f"order_press({combo}) = {got}，应为 {want}")
    return fails


def main() -> int:
    global _hook
    if keys._user32 is None:
        print("SKIPPED（SendInput 不可用）")
        return 0

    fails = _check_order()
    for f in fails:
        print(f"FAIL {f}")

    threading.Thread(target=_pump_thread, daemon=True).start()
    if not _hook_ready.wait(3):
        print("SKIPPED（键盘钩子线程没起来）")
        return 1 if fails else 0
    if not _hook:
        print(f"SKIPPED（键盘钩子装不上，err={ctypes.get_last_error()}）")
        return 1 if fails else 0

    if not _calibrate():
        if fails:
            print(f"INJECTION FAILED（{len(fails)} 项）")
            return 1
        print("SKIPPED（当前会话收不到注入的按键状态，多半是无桌面环境）")
        return 0
    _pump(_SETTLE)

    pre = _pressed()
    if pre:
        print(f"⚠ 开始前这些键就处于按下状态，结果可能不准：{sorted(pre)}")

    for combo, expect in CASES:
        label = "+".join(combo)
        has_win = any(k.strip().lower() in _WIN_KEYS for k in combo)
        failure = None
        seen: set[str] = set()
        down: set[str] = set()
        attempt = 0            # 记录用了几轮（下面是 1-based）
        for attempt in range(1, _ATTEMPTS_HOOK + 1):
            _pump(_SETTLE)
            _hook_seen.clear()
            _raw.clear()
            # ⚠ 先记下"这一轮开始前就已经按着的键"。
            #   卡键判定要减掉它们 —— 否则上一轮残留（或用户自己按着的 Ctrl）
            #   会被算成"我们没释放"，报出一个根本不存在的卡键假 FAIL。
            pre_case = _pressed()
            keys.hotkey_down(combo)        # ← 走生产路径（含排序 + 校验补发）
            _pump(0.18)
            down = _pressed()
            seen = set(_hook_seen)
            keys.hotkey_up()
            _pump(0.20)
            after = _pressed()
            _dismiss_start()

            miss_hook, miss_state = [], []
            for e in expect:
                hook_ok = PROBES[e][1] is not None     # 这个键在钩子里可判
                if hook_ok and e not in seen:
                    miss_hook.append(e)                # 钩子没收到 —— 输入法会认不到
                elif not hook_ok:
                    if e not in down:                  # 不可判的只能靠状态位
                        miss_state.append(e)
                elif not has_win and e not in down:
                    miss_state.append(e)               # 不带 Win 时必须两样都成立
            # 减掉"本轮开始前就按着的键"，只追究这一次注入留下的残留
            stuck = sorted((after & set(expect)) - pre_case)

            if miss_hook:
                failure = f"{label}：钩子没收到 {miss_hook} 的按下事件（输入法会认不到）"
            elif miss_state:
                failure = f"{label}：{miss_state} 没有被真正按下（注入无效）"
            elif stuck:
                failure = f"{label}：{stuck} 释放后仍处于按下状态（卡键）"
            else:
                failure = None
            # 卡键是确定性的，别浪费轮次；其余失败继续重试 ——
            # 低级键盘钩子有超时机制，机器一忙就会漏事件，
            # 而真正的回归（顺序被改回去 / SendInput 静默失效）是每一轮都挂的。
            if failure is None or stuck:
                break
        if failure:
            fails.append(failure)
        else:
            extra = "  （带 Win：状态位不作判据，见文件头）" if has_win else ""
            retry = f"  [第{attempt}次才过]" if attempt > 1 else ""
            print(f"OK   {label:22s} → 钩子{sorted(seen)} 状态{sorted(down & set(expect))}{extra}{retry}")
        if _RAW:
            print(f"       raw: {list(_raw)}")

    keys.hotkey_up()
    user32.UnhookWindowsHookEx(_hook)
    _hook = None

    if fails:
        for f in fails:
            print(f"FAIL {f}")
        print(f"INJECTION FAILED（{len(fails)} 项）")
        # 「钩子没收到」在机器忙的时候可能连着栽满所有轮次 —— 它和"组合键顺序错了"
        # 长得一模一样，但一个是环境噪声、一个是真回归。给一句提示，
        # 免得又有人（包括我们）顺着假 FAIL 去改本来没错的代码。
        if all("钩子没收到" in f for f in fails):
            print(f"提示：这类失败也可能是**机器忙**造成的（已自动重试 {_ATTEMPTS_HOOK} 轮仍失败）。"
                  "低级键盘钩子有超时机制，控制台服务 / 浏览器 / 视频渲染占着 CPU 时会漏事件。"
                  "先关掉这些负载再重跑一次；仍然失败才当代码问题查。")
        return 1
    print("INJECTION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
