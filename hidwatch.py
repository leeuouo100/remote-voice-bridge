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
        self.key_events: list[tuple[float, str, str, int]] = []   # ts, kind, name, scan
        self._hooks: list = []
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
        """只挂**键盘**钩子。

        ⚠ 为什么不用再挂鼠标钩子：遥控器的鼠标集合（Col03）已经被本模块的
          "原始报告"线程直接读着了 —— 那比挂鼠标钩子**更靠前也更准**
          （钩子拿到的是 Windows 翻译后的结果，报告是设备原样发出的字节）。
          遥控器若把方向键当鼠标发，Col03 的计数一定会涨，看得到。
          另外 `mouse` 也不在 requirements.txt 里，挂它是白挂。
        """
        try:
            import keyboard as kb
        except ImportError:
            return

        def on_kb(e):
            if e.event_type != "down":
                return
            item = (time.time(), "keyboard", str(e.name), int(e.scan_code or 0))
            self.key_events.append(item)
            self._emit(item)

        try:
            self._hooks.append(kb.hook(on_kb, suppress=False))
        except Exception:                       # noqa: BLE001
            pass

    def _emit(self, item) -> None:
        if self.on_event:
            try:
                self.on_event(item)
            except Exception:                   # noqa: BLE001
                pass

    def stop(self) -> None:
        for c in self.collections:
            c.close()
        try:
            import keyboard as kb
            kb.unhook_all()
        except Exception:                       # noqa: BLE001
            pass
        self._hooks.clear()

    # ── 报告 ──────────────────────────────────────────────────────────────
    def summary_lines(self) -> list[str]:
        """"哪个键发在哪一路"的结论表。"""
        L: list[str] = []
        A = L.append
        A("【按键落点】各监听通道收到的次数")
        A("")
        A(f"  {'通道':<42}{'次数':>6}")
        A("  " + "-" * 60)
        kb_n = len([1 for e in self.key_events if e[1] == "keyboard"])
        A(f"  {'键盘钩子（Windows 键盘事件）':<42}{kb_n:>6}")
        for c in self.collections:
            A(f"  {c.key:<42}{len(c.reports):>6}{c.status}")
        A("")

        vendor_hit = [c for c in self.collections if c.is_vendor and c.reports]
        mouse_hit = [c for c in self.collections
                     if c.page == 0x01 and c.usage == 0x02 and c.reports]
        A("【判读】")
        if kb_n:
            A("  → 键盘事件收到了：按键**能**到达 Windows。")
            A("     若仍没反应，问题在程序这一层（映射值 / 拦截开关）。")
            A("     请看 bridge.log 里 🔘 HID 按键 那几行的实际键名与动作。")
            if vendor_hit:
                A("     （厂商页也同时收到了报告：说明遥控器是「键盘+厂商」双发，")
                A("       那一份不影响键盘那一路。）")
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
        return L

    def summary(self) -> str:
        return "\n".join(self.summary_lines())


def available() -> bool:
    """这台机器能不能读 HID 集合（非 Windows 直接不可用）。

    ⚠ 不检查 `keyboard` / `mouse`：读集合本身只要 ctypes。
      钩子库缺失只影响"键盘钩子"那一行的计数，不影响核心结论。
    """
    return hasattr(ctypes, "WinDLL")
