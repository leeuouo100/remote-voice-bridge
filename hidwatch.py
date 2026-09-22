"""
HID 报告监听 —— 按键到底发在哪一路？

## 为什么必须做这个

`hidinfo.py` 能告诉你"遥控器暴露了哪些集合"，但答不了最后那一问：
**某个具体按键（比如方向键、确认键）的报告，落在哪一路？**

这一问之所以关键，是因为遥控器的 5 个集合里，Windows 只认其中 3 路：

    Col01 键盘      → Windows 当键盘事件   → 我们的键盘钩子收得到
    Col02 消费类    → Windows 当媒体键     → 我们的键盘钩子收得到
    Col03 鼠标      → Windows 当鼠标       → **键盘钩子收不到**（要看鼠标）
    Col04 厂商 0xFF01 → Windows 什么都不做 → **谁都收不到，除非自己去读**
    Col05 厂商 0xFF80 → 同上

所以"按键没反应"有四种完全不同的成因，只看日志区分不出来：

  ① 报告根本没来（遥控器没发 / 没连上）        → 蓝牙层问题
  ② 报告来了，落在厂商页                       → Windows 不翻译，只能我们自己读
  ③ 报告来了，落在鼠标页                       → Windows 动鼠标，键盘钩子看不见
  ④ 报告来了，也变成键盘事件了 → 钩子也收到了  → 问题在我们这层的映射/注入

本模块同时挂上**三层监听**（键盘钩子 / 鼠标钩子 / 各集合原始报告流），
同一次按键在哪一层出现，就是答案。这是唯一能把 ①②③④ 分开的办法。

## 实现要点（踩过坑，别改）

· HID 集合用**阻塞** ReadFile 读（CreateFileW 不传 FILE_FLAG_OVERLAPPED）。
  每路一个守护线程，一直阻塞等报告 —— 有按键才醒，不占 CPU。
· 停止时**必须 CancelIoEx 再 CloseHandle**：不然线程永远卡在 ReadFile 上，
  进程退不掉，用户会以为程序卡死。CancelIoEx 之后 ReadFile 返回 FALSE 并
  置 ERROR_OPERATION_ABORTED，线程自然收尾。
· 键盘/鼠标集合可能被系统独占（kbdhid/mouhid 占着），打不开属正常，
  **不算失败**，只记一笔"这路打不开（被系统占用）"。厂商页没人占，一定打得开。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

try:
    import hidinfo
except ImportError:                                     # 打包/独立运行
    from . import hidinfo                           # type: ignore[no-redef]

_VENDOR_PAGE_MIN = 0xFF00

# ── 低级键盘钩子：把「注入」标志一起拿下来 ────────────────────────────────
#
# 为什么非要有它（2026-09-17 补，代价是被带偏过一整轮排查）：
#   `keyboard` 库的钩子只给键名，**看不见 LLKHF_INJECTED** —— 而本程序每次按
#   语音键都会**自己注入** lctrl+lwin+lshift（一次 3~4 个键事件）。这些注入键
#   和遥控器的按键在钩子层长得一模一样，于是「键盘事件收到了 33 个」被当成
#   「遥控器的按键能到 Windows」的证据；真相是那 33 个里绝大部分是我们自己
#   发的。`tools/check_hidinfo.py` 的判读测试因此长期**假绿**。
#   装上这个钩子之后，「真实 N 个 / 注入 M 个」分开数，判读才可信。
#
# 不复用 `keyboard._winkeyboard` 的私有定义：那是个下划线模块，换版本就可能
# 没了；诊断工具宁可自带一份，也顺便不依赖 `keyboard` 这个可选包。

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
LLKHF_INJECTED = 0x00000010
PM_REMOVE = 0x0001


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


_HOOKPROC = (ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                wintypes.WPARAM, wintypes.LPARAM)
             if hasattr(ctypes, "WINFUNCTYPE") else None)

# vk → 名字。只收「遥控器可能发的 + 排查时认得出的」；认不出的退回 VK_0xNN。
# 特意把消费类媒体键（音量 / 播放 / 浏览器）列全：那种遥控器的音量键就是它们，
# 排查时最需要一眼看到的是「到底有没有 volume up 这种键出现」。
_VK_NAMES: dict[int, str] = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x10: "shift", 0x11: "ctrl",
    0x12: "alt", 0x13: "pause", 0x14: "caps lock", 0x1B: "esc", 0x20: "space",
    0x21: "page up", 0x22: "page down", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2C: "print screen", 0x2D: "insert", 0x2E: "delete",
    0x5B: "left windows", 0x5C: "right windows", 0x5D: "apps",
    0x6A: "numpad *", 0x6B: "numpad +", 0x6D: "numpad -", 0x6E: "numpad .",
    0x6F: "numpad /", 0x90: "num lock", 0x91: "scroll lock",
    0xA0: "left shift", 0xA1: "right shift", 0xA2: "left ctrl",
    0xA3: "right ctrl", 0xA4: "left alt", 0xA5: "right alt",
    0xA6: "browser back", 0xA7: "browser forward", 0xA8: "browser refresh",
    0xA9: "browser stop", 0xAA: "browser search", 0xAB: "browser favorites",
    0xAC: "browser home", 0xAD: "volume mute", 0xAE: "volume down",
    0xAF: "volume up", 0xB0: "media next", 0xB1: "media prev",
    0xB2: "media stop", 0xB3: "media play", 0xB4: "launch mail",
    0xB5: "media select", 0xB6: "launch app 1", 0xB7: "launch app 2",
    0xBA: ";:", 0xBB: "=+", 0xBC: ",<", 0xBD: "-_", 0xBE: ".>", 0xBF: "/?",
    0xC0: "`~", 0xDB: "[{", 0xDC: "\\|", 0xDD: "]}", 0xDE: "'\"",
}
for _i in range(24):                       # F1..F24
    _VK_NAMES[0x70 + _i] = f"f{_i + 1}"
for _i in range(10):                       # 主键盘数字 / 小键盘
    _VK_NAMES[0x30 + _i] = chr(ord("0") + _i)
    _VK_NAMES[0x60 + _i] = f"numpad {_i}"
for _i in range(26):                       # 字母
    _VK_NAMES[0x41 + _i] = chr(ord("a") + _i)


def vk_name(vk: int) -> str:
    """vk 码 → 可读名字。认不出就退回 `VK_0xNN`，**不静默丢**。"""
    return _VK_NAMES.get(int(vk), f"VK_0x{int(vk):02X}")


class LowLevelKeyHook:
    """WH_KEYBOARD_LL 钩子：记 `(时间, 名字, vk, scan, 是否注入)`。

    低级钩子必须由**装它的那个线程**抽消息，所以这里自带线程 + `PeekMessage`
    轮询（用 `GetMessage` 会阻塞住，收不到停信号）。

    `installed=False` 时看 `error` 里的原因 —— 钩子装不上是**必须报出来**的
    一种"读不到"：它会让下面所有键盘计数变成 0，看起来像"遥控器没反应"。
    """

    def __init__(self, on_event=None):
        self.on_event = on_event
        self.events: list[tuple[float, str, int, int, bool]] = []
        self.installed = False
        self.error = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._proc = None
        self._hook = 0
        self._user32 = None

    def start(self, timeout: float = 2.0) -> bool:
        if _HOOKPROC is None:
            self.error = "这个平台没有 WINFUNCTYPE（非 Windows）"
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="llkbhook")
        self._thread.start()
        t0 = time.time()
        while not self.installed and not self.error and time.time() - t0 < timeout:
            time.sleep(0.02)
        return self.installed

    def _run(self) -> None:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError as e:                                    # noqa: BLE001
            self.error = f"加载 user32 失败：{e}"
            return
        self._user32 = user32
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, _HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
            wintypes.UINT, wintypes.UINT, wintypes.UINT]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        self._proc = _HOOKPROC(self._on)            # 必须留引用，回收即失效
        h = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc,
                                     kernel32.GetModuleHandleW(None), 0)
        if not h:
            self.error = f"SetWindowsHookExW 失败 err={ctypes.get_last_error()}"
            return
        self._hook = h
        self.installed = True

        msg = wintypes.MSG()
        while not self._stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)
        try:
            user32.UnhookWindowsHookEx(ctypes.c_void_p(self._hook))
        except Exception:                                       # noqa: BLE001
            pass
        self.installed = False

    def _on(self, n_code, w_param, l_param):
        # ⚠ 回调里绝不能抛异常、也不能慢：抛出去会撕断整条钩子链，
        #   整个系统的键盘都会跟着卡。
        try:
            if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(
                    l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                # 元组形状与退路钩子保持一致：(ts, kind, name, scan, injected)
                item = (time.time(), "keyboard", vk_name(vk),
                        int(kb.scanCode), bool(int(kb.flags) & LLKHF_INJECTED))
                self.events.append(item)
                if self.on_event:
                    self.on_event(item)
        except Exception:                                       # noqa: BLE001
            pass
        try:
            return self._user32.CallNextHookEx(None, n_code, w_param, l_param)
        except Exception:                                       # noqa: BLE001
            return 0

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None


class HidCollection:
    """一路 HID 集合：句柄 + 身份信息 + 收到的报告。"""

    def __init__(self, path: str, vid: int, pid: int, page: int, usage: int, in_len: int):
        self.path = path
        self.vid = vid
        self.pid = pid
        self.page = page
        self.usage = usage
        self.in_len = max(int(in_len or 0), 8)
        self.handle = None
        self.reports: list[tuple[float, bytes]] = []
        self.open_error = ""
        # ⚠ 单独记"开成功过没有"：`close()` 会把 handle 清成 None，
        #   之后再看 handle 就分不清"从没打开"和"已经关了"，
        #   报告里那两行会退化成一样的空括号。
        self.opened = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def is_keyboard(self) -> bool:
        return (self.page, self.usage) == (0x01, 0x06)

    @property
    def is_mouse(self) -> bool:
        return (self.page, self.usage) == (0x01, 0x02)

    @property
    def status(self) -> str:
        """报告里显示的状态后缀。

        `ERROR_ACCESS_DENIED(5)` 对**键盘/鼠标**集合是**预期结果**：
        kbdhid / mouhid 独占持有它们，第三方读不到 —— 而且我们也不需要读
        （Windows 自己会把它们翻译成键盘事件，走钩子就能看到）。
        不解释这一点的话，报告里一行"打开失败"会让人以为程序有毛病。
        """
        if self.opened:
            return ""
        if "err=5" in self.open_error and (self.is_keyboard or self.is_mouse):
            return "（系统独占，属正常：这一路由 Windows 自己处理）"
        return f"（{self.open_error}）"

    # ── 显示 ──────────────────────────────────────────────────────────────
    @property
    def key(self) -> str:
        """给报告里用的一行短标签。"""
        name, _handled, _why = hidinfo.collection_role(self.page, self.usage)
        col = ""
        for part in self.path.split("#"):
            if part.lower().startswith("col"):
                col = part.split("&")[0].upper()
                break
        return f"{col or '—':<6} 0x{self.page:04X}/{self.usage:04X} {name}"

    @property
    def is_vendor(self) -> bool:
        return self.page >= _VENDOR_PAGE_MIN

    # ── 读取 ──────────────────────────────────────────────────────────────
    def open(self) -> bool:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        h = k32.CreateFileW(self.path, 0x80000000, 3, None, 3, 0, None)
        if not h or h == ctypes.c_void_p(-1).value:
            self.open_error = f"打开失败 err={ctypes.get_last_error()}"
            return False
        self.handle = h
        self.opened = True
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name=f"hidread-{self.key.strip()}")
        self._thread.start()
        return True

    def _pump(self) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        buf = ctypes.create_string_buffer(self.in_len)
        n = wintypes.DWORD(0)
        while not self._stop.is_set():
            ok = k32.ReadFile(ctypes.c_void_p(self.handle), buf, self.in_len,
                              ctypes.byref(n), None)
            if not ok:
                break                       # 被 CancelIoEx 打断，或设备断开
            if n.value:
                self.reports.append((time.time(), bytes(buf.raw[:n.value])))

    def close(self) -> None:
        self._stop.set()
        if self.handle:
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                k32.CancelIoEx(ctypes.c_void_p(self.handle), None)
            except Exception:                       # noqa: BLE001
                pass
            try:
                ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self.handle))
            except Exception:                       # noqa: BLE001
                pass
            self.handle = None
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None


class ReportWatcher:
    """开一路线程读一路集合，外加键盘/鼠标钩子。用法：

        w = ReportWatcher()
        w.start()          # 打开所有 Google 集合 + 挂钩子
        ...                # 期间用户按键
        print(w.summary())
        w.stop()
    """

    # (来源, 时间, 文本)
    def __init__(self, only_google: bool = True, on_event=None):
        self.only_google = only_google
        self.on_event = on_event
        self.collections: list[HidCollection] = []
        # (ts, kind, name, scan, injected)
        # ⚠ `injected` 三态：True=本程序自己注入的，False=真实按键，
        #   None=不知道（退路钩子给不出这个信息）。判读时**只有 False 才作数** ——
        #   把 None 当成"真实"会让注入键冒充遥控器按键，正是被带偏的那次。
        self.key_events: list[tuple[float, str, str, int, bool | None]] = []
        self._hooks: list = []
        self._kb_hook: LowLevelKeyHook | None = None
        self.hook_error = ""
        self.started_at = 0.0

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self, with_hooks: bool = True) -> int:
        """打开集合（可同时挂钩子）。返回成功打开的集合数。

        `with_hooks=False` 用于**并发**场景：`tools/diag_remote.py` 自己已经有
        一个键盘钩子在按阶段记账，再挂一个只会把同一个按键记两遍。
        那种场合它只要"集合原始报告"这半边。
        """
        self.started_at = time.time()
        for d in hidinfo.live_hid_collections():
            if self.only_google and not d.get("is_google"):
                continue
            c = HidCollection(d["path"], d["vid"], d["pid"],
                              d["usage_page"], d["usage"], d["in_len"])
            if c.open():
                self.collections.append(c)
            else:
                self.collections.append(c)      # 也留下，报告里要写"打不开"
        if with_hooks:
            self._install_hooks()
        return len([c for c in self.collections if c.handle])

    def _install_hooks(self) -> None:
        """挂**键盘**钩子：优先用自带低级钩子（能分"注入"），退而用 keyboard 库。

        ⚠ 为什么不用再挂鼠标钩子：遥控器的鼠标集合（Col03）已经被本模块的
          "原始报告"线程直接读着了 —— 那比挂鼠标钩子**更靠前也更准**
          （钩子拿到的是 Windows 翻译后的结果，报告是设备原样发出的字节）。
          遥控器若把方向键当鼠标发，Col03 的计数一定会涨，看得到。
          另外 `mouse` 也不在 requirements.txt 里，挂它是白挂。
        """
        hook = LowLevelKeyHook(on_event=self._on_native_key)
        if hook.start():
            self._kb_hook = hook
            self._hooks.append("llkb")
            return
        self.hook_error = hook.error or "低级钩子装不上（原因未记录）"

        # 退路：`keyboard` 库。键名有了，但**分不出是不是我们自己注入的**，
        # 所以注入标志记成 None（判读时会明说"这一栏不知道"，而不是冒充"真实"）。
        try:
            import keyboard as kb
        except ImportError:
            self.hook_error += "；`keyboard` 包也没装"
            return

        def on_kb(e):
            if e.event_type != "down":
                return
            item = (time.time(), "keyboard", str(e.name),
                    int(e.scan_code or 0), None)
            self.key_events.append(item)
            self._emit(item)

        try:
            self._hooks.append(kb.hook(on_kb, suppress=False))
        except Exception as e:                  # noqa: BLE001
            self.hook_error += f"；kb.hook 失败：{e.__class__.__name__}: {e}"

    def _on_native_key(self, item) -> None:
        self.key_events.append(item)
        self._emit(item)

    def _emit(self, item) -> None:
        if self.on_event:
            try:
                self.on_event(item)
            except Exception:                   # noqa: BLE001
                pass

    def stop(self) -> None:
        for c in self.collections:
            c.close()
        if self._kb_hook is not None:
            self._kb_hook.stop()
            self._kb_hook = None
        try:
            import keyboard as kb
            kb.unhook_all()
        except Exception:                       # noqa: BLE001
            pass
        self._hooks.clear()

    # ── 报告 ──────────────────────────────────────────────────────────────
    def summary_lines(self) -> list[str]:
        """"哪个键发在哪一路"的结论表。

        ⚠ 判读的**唯一铁律**：只有 `injected is False`（真实、非注入）的键盘事件
          才能用来支撑"按键能到 Windows"。`True` 是本程序自己注入的语音热键，
          `None` 是"不知道"。以前这里写的是 `if kb_n:` —— 只要钩子活着就一定成立，
          于是把 33 个（其实大部分是自己注入的）当成"遥控器按键到了 Windows"，
          连着两轮排查都被带偏。别再改回去。
        """
        L: list[str] = []
        A = L.append
        real = [e for e in self.key_events if e[4] is False]
        inj = [e for e in self.key_events if e[4] is True]
        unk = [e for e in self.key_events if e[4] is None]

        def tally(events) -> str:
            d: dict[str, int] = {}
            for e in events:
                d[e[2]] = d.get(e[2], 0) + 1
            return "、".join(f"{n}×{c}" if c > 1 else n
                            for n, c in sorted(d.items(),
                                               key=lambda kv: (-kv[1], kv[0])))

        A("【按键落点】各监听通道收到的次数")
        A("")
        A(f"  {'通道':<46}{'次数':>6}")
        A("  " + "-" * 64)
        A(f"  {'⌨ 键盘事件 · 真实（非注入）':<46}{len(real):>6}   ← 只有这一行能作证")
        A(f"  {'⌨ 键盘事件 · 注入（本程序自己发的）':<46}{len(inj):>6}")
        if unk:
            A(f"  {'⌨ 键盘事件 · 分不出是不是注入的':<46}{len(unk):>6}")
        for c in self.collections:
            A(f"  {c.key:<46}{len(c.reports):>6}{c.status}")
        A("")

        vendor_hit = [c for c in self.collections if c.is_vendor and c.reports]
        mouse_hit = [c for c in self.collections
                     if c.page == 0x01 and c.usage == 0x02 and c.reports]
        A("【判读】")
        if real:
            A(f"  → 收到 {len(real)} 个**真实**键盘事件（非注入）：")
            A(f"     {tally(real)}")
            A("     这些键确实进了 Windows。若你当时手没碰键盘、只按遥控器，")
            A("     那它们就是遥控器发来的 —— 问题在程序这层（映射值 / 拦截开关），")
            A("     对照 bridge.log 里 🔘 HID 按键 那几行的键名与动作即可。")
            A("     ⚠ 但若上面这些键名其实是你**敲出来**的字母/回车，那就来自物理")
            A("       键盘，**不能**证明遥控器有反应 —— 请重跑一次，全程别碰键盘。")
            if vendor_hit:
                A("     （厂商页也同时收到了报告：说明遥控器是「键盘+厂商」双发，")
                A("       那一份不影响键盘那一路。）")
        elif inj:
            A(f"  → ⚠ 键盘事件只有 {len(inj)} 个，而且**全是本程序自己注入的**：")
            A(f"     {tally(inj)}")
            A("     这是语音热键（按一次语音键就注入 3~4 个事件），**不能**当作")
            A("     「遥控器的按键到了 Windows」的证据 —— 本判读以前正是这么误判的，")
            A("     所以这里必须明说：这次测量里遥控器的按键**一个都没到**。")
            A("     下一步看上面 HID 集合 / 私有服务的计数，并确认桥程序已退出。")
        elif unk:
            A(f"  → ⚠ 收到 {len(unk)} 个键盘事件，但**分不出是不是注入的**")
            A("     （低级钩子没挂上，用的是退路钩子）。")
            A(f"     原因：{self.hook_error or '未记录'}")
            A("     这种情况下无法下结论 —— 这些事件可能全是本程序自己发的。")
            A("     请重跑，并确认报告里【键盘钩子】显示的是低级钩子。")
        elif vendor_hit:
            A("  → 🎯 按键报告**只出现在厂商自定义页**，Windows 键盘事件一个都没有。")
            A("     这就是「除语音键外所有按键都没反应」的根因：")
            A("     报告被发在 Windows 不认识的那一页，任何映射表都够不到它。")
            A("     要做的是：自己去读这些厂商集合、按字节解出按键 —— 不是改映射。")
            for c in vendor_hit:
                A(f"     · {c.key} 收到 {len(c.reports)} 条，"
                  f"样本 {c.reports[0][1].hex(' ')}")
        elif mouse_hit:
            A("  → 只收到鼠标事件：按键是当**鼠标**发出来的（方向键/确认键常见）。")
            A("     映射表里配的键盘动作对它无效，需要按鼠标事件单独处理。")
        elif any(c.reports for c in self.collections):
            A("  → 收到了其它页的报告，但没有键盘事件。同上：要自己解报告。")
        else:
            A("  → ⚠ 一条都没收到。可能原因：遥控器没连上、按键没按下、")
            A("     或按键报告走在**厂商页而该路被系统占用**。先确认遥控器已连接、")
            A("     且刚才确实按了键；仍为空就跑一次 diag_remote.py 对照。")
        if self.hook_error:
            A(f"  （键盘钩子备注：{self.hook_error}）")
        return L

    def summary(self) -> str:
        return "\n".join(self.summary_lines())


def available() -> bool:
    """这台机器能不能读 HID 集合（非 Windows 直接不可用）。

    ⚠ 不检查 `keyboard` / `mouse`：读集合本身只要 ctypes。
      钩子库缺失只影响"键盘钩子"那一行的计数，不影响核心结论。
    """
    return hasattr(ctypes, "WinDLL")
