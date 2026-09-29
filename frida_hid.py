"""遥控器按键旁路 —— 注入 WUDFHost.exe，从 HID 驱动内部把报告抄出来。

── 为什么需要这一路（先读这段，别急着改）────────────────────────────
在此之前，本项目读遥控器按键走的是 `remote_hid.py`：自己 CreateFile 打开
厂商页（0xFF01 / 0xFF80）读原始报告。**真机实测那一路一直是 0 条**，
于是有了「除语音键外所有按键都没反应」这个悬案。

2026-09-29 定案：**报告不是没发，是在 WUDFHost.exe 内部就被消费掉了。**
BLE HID（HOGP）在 Windows 上是 UMDF 驱动，跑在 WUDFHost.exe 里；它通过
`ntdll!NtDeviceIoControlFile` + IOCTL `0x80018483` 把 GATT 特征读上来，
报告就在那次调用的**输出缓冲区**里 —— 而这一层在用户态 HID 接口之前，
所以 `ReadFile` / Raw Input / 键盘钩子**全都看不到**。

唯一出路：用 Frida 注入 WUDFHost，在那次 IOCTL 的输出缓冲区上抄一份。
机制移植自 VibeMote（https://github.com/Tilkmilk/vibe-mote，MIT），
见 THIRD_PARTY_NOTICES.md。

── 与 remote_hid.py 的关系 ──────────────────────────────────────────
两条路**并行、互补**，解出来的 button_id 都喂给 main.py 同一个
`_on_hid_button` → 现有的 `resolve_button` 映射表 / 控制台 / config.json
**全都不用改**：
  · frida_hid（本模块）：注入驱动宿主读报告 —— 真机上唯一能拿到按键的那一路
  · remote_hid：自开厂商页读报告 —— 对某些驱动/适配器组合仍然有效，保留
任一路拿到按键都能用；两路都没拿到就是"报告真的没来"。

── 降级 ────────────────────────────────────────────────────────────
没装 frida、注入被安全软件拦、宿主找不到 —— 一律只记日志、不抛异常，
语音功能完全不受影响。这一路是"读不到"，不是"桥起不来"。

── 报告格式 ────────────────────────────────────────────────────────
同一个 WUDFHost 还服务别的蓝牙键鼠，它们吐 9 字节键盘报告，混在一起。
遥控器只发两种（见 decode_tap_report）：
  · 3 字节消费类页：[0x02][usage_lo][usage_hi]（16 位 usage）
  · N 字节厂商页：  [0x01][usage]…（8 位 usage）
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import logsetup

logger = logging.getLogger("rvb.frida")


def _tap_js_path() -> Path:
    """定位 frida_tap.js —— 源码环境在模块旁，打包后由 PyInstaller 放到 _MEIPASS。"""
    import sys
    cands: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(Path(base) / "frida_tap.js")
    cands.append(Path(__file__).resolve().parent / "frida_tap.js")
    for c in cands:
        if c.is_file():
            return c
    return cands[-1]

# ── 消费类页 usage → 按钮 id ─────────────────────────────────────────
#
# 遥控器在**消费类页**（0x000C）上发的是 16 位 usage；这张表来自 VibeMote
# 对同款遥控器（Google TV / Chromecast，VID 18D1 PID 9450）的受控采集
# （其自述 14/14 全命中）。与本项目 config.CHROMECAST_BUTTONS 的 button_id
# 一一对齐，所以两条路解出来的东西能喂进同一张映射表。
#
# ⚠ 语音键**不在**这张表里：它走 ATVV 的 BLE 数据通道（ATVV CTL op 0x04），
#   不经过 HID。混进来就是"按语音键触发两次"。
CC_USAGE_TO_BUTTON: dict[int, str] = {
    0x019E: "power",
    0x0189: "input",
    0x0042: "up",
    0x0043: "down",
    0x0044: "left",
    0x0045: "right",
    0x0041: "ok",
    0x0224: "back",
    0x0223: "home",
    0x00E9: "vol_up",
    0x00EA: "vol_down",
    0x00E2: "mute",
    0x0077: "youtube",
    0x0078: "netflix",
}
BUTTON_TO_CC_USAGE: dict[str, int] = {v: k for k, v in CC_USAGE_TO_BUTTON.items()}


def decode_tap_report(raw: bytes) -> tuple[str | None, bool]:
    """把一条从 WUDFHost 抄出来的原始报告解成 (按钮 id, 是否按下)。

    返回 (None, False) = 认不出（调用方忽略）。
    返回 ("<up>", False) = 松手帧（具体松开哪个键由调用方用「上一个按下的键」补）。
    抽成纯函数是为了能脱离硬件单测 —— 报告格式是本模块最容易写错、
    又最难靠"看起来能用"发现的地方（同 remote_hid.decode_report 的理由）。
    """
    if not raw or len(raw) < 2:
        return None, False

    if raw[0] == 0x02 and len(raw) >= 3:
        # 消费类页：16 位 usage
        usage = raw[1] | (raw[2] << 8)
        if usage == 0:
            return "<up>", False
        btn = CC_USAGE_TO_BUTTON.get(usage)
        return (btn, True) if btn else (None, False)

    if raw[0] == 0x01:
        # 厂商页：8 位 usage（与 remote_hid 同一套解码口径）
        usage = raw[1]
        if usage == 0:
            return "<up>", False
        from remote_hid import USAGE_TO_BUTTON
        btn = USAGE_TO_BUTTON.get(usage)
        return (btn, True) if btn else (None, False)

    return None, False


def block_usages_for(keymap: dict | None) -> tuple[list[int], list[int]]:
    """按当前 keymap 算出「要抹掉原生动作」的 usage 列表。

    只抹**已被映射**的键（空值 / "native" = 禁用，不抹 —— 那种情况用户
    就是想让这个键保持系统原生行为）。返回 (消费类页 usages, 厂商页 usages)。
    """
    km = keymap or {}
    cc: list[int] = []
    vp: list[int] = []
    try:
        from config import CHROMECAST_BUTTONS
    except Exception:                                  # noqa: BLE001
        CHROMECAST_BUTTONS = {}
    for btn, target in km.items():
        t = str(target or "").strip()
        if not t or t == "native":
            continue
        u = BUTTON_TO_CC_USAGE.get(btn)
        if u is not None:
            cc.append(u)
        v = (CHROMECAST_BUTTONS.get(btn) or {}).get("usage")
        if isinstance(v, int):
            vp.append(v)
    return sorted(cc), sorted(vp)


# ── 定位遥控器的 HID 驱动宿主（WUDFHost.exe）─────────────────────────
#
# ⚠ 本机实测的节点名长这样（2026-09-29，只读查注册表得来）：
#   {00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c
#     ├─ 服务 UUID（0x1812 = HID over GATT）
#     ├─ VID 段带两位前缀（`02` + `18d1`）⇒ 要比对**后 4 位**
#     └─ 末尾是**远端 MAC**（f196a263671c）—— 比 VID/PID 更硬的"就是这台"
#   而且**只有 0x1812 那个服务节点**带 `Device Parameters\WUDFDiagnosticInfo\HostPid`，
#   别的服务（1800/1801/180a/…）没有 ⇒ 靠它就能唯一确定驱动宿主。
_BTHLE_ENUM = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
_HID_SVC_PREFIX = "{00001812-0000-1000-8000-00805f9b34fb}"
_WUDF_DIAG = r"Device Parameters\WUDFDiagnosticInfo"
READ_IOCTL = 0x80018483

_VIDPID_RE = re.compile(r"vid&([0-9a-f]+)_pid&([0-9a-f]+)", re.I)


@dataclass(frozen=True)
class HidHost:
    """一台 BLE HID（0x1812）设备所在的驱动宿主 —— 一条**可核对**的身份。"""

    host_pid: int
    svc: str          # 服务节点名（含 UUID / VID / PID / MAC）
    instance: str     # 该服务节点下的设备实例键
    vid: str          # 解析出来的 VID（小写 4 位）
    dev_pid: str      # 解析出来的 PID（小写 4 位）

    @property
    def key(self) -> str:
        """注册表里的完整实例路径（日志/核对用）。"""
        return f"{self.svc}\\{self.instance}"

    @property
    def mac(self) -> str | None:
        """节点名末尾的远端 MAC（12 位小写 hex），解析不出返回 None。"""
        m = re.search(r"_([0-9a-f]{12})$", self.svc, re.I)
        return m.group(1).lower() if m else None

    def matches(self, vid: str, pid: str) -> bool:
        return (self.vid == str(vid).lower()[-4:]
                and self.dev_pid == str(pid).lower()[-4:])


def _parse_vidpid(name: str) -> tuple[str, str] | None:
    """从 BTHLEDevice 服务节点名里抠出 (VID, PID)，各取后 4 位小写。

    ⚠ 不能再用老写法的 `vid.lower() in name.lower()`（子串判断）：
    节点名里还有 REV / 序列号 / MAC 等段，短 VID 会在别处偶然命中 ——
    所谓"精确匹配"其实是在撞运气（2026-09-29 审查报告 P1-8）。
    另外 Windows 的 VID 段带两位前缀（`VID&0218d1`），必须取后 4 位。
    """
    m = _VIDPID_RE.search(name or "")
    if not m:
        return None
    return (m.group(1).lower()[-4:], m.group(2).lower()[-4:])


def hid_hosts(vid: str = "18D1", pid: str = "9450",
              any_hid: bool = False) -> list[HidHost]:
    """所有 BLE HID（0x1812）设备所在的驱动宿主。

    `any_hid=True`：不挑 VID/PID，任何 BLE HID 设备都要 —— 这是**兼容款兜底**，
    默认关。为什么默认关（P1-8）：同一个 WUDFHost 里可能还服务别的蓝牙键鼠，
    "猜第一项"等于可能去抄/改写**别人**的报告。
    """
    import winreg
    out: list[HidHost] = []
    want = (str(vid).lower()[-4:], str(pid).lower()[-4:])
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BTHLE_ENUM) as root:
            i = 0
            while True:
                try:
                    svc = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not svc.casefold().startswith(_HID_SVC_PREFIX.casefold()):
                    continue
                parsed = _parse_vidpid(svc)
                if not any_hid and parsed != want:
                    continue
                with winreg.OpenKey(root, svc) as sk:
                    j = 0
                    while True:
                        try:
                            inst = winreg.EnumKey(sk, j)
                        except OSError:
                            break
                        j += 1
                        try:
                            with winreg.OpenKey(
                                    root, f"{svc}\\{inst}\\{_WUDF_DIAG}") as dk:
                                host_pid = int(winreg.QueryValueEx(dk, "HostPid")[0])
                        except (OSError, TypeError, ValueError):
                            continue
                        v, p = parsed if parsed else ("", "")
                        out.append(HidHost(host_pid=host_pid, svc=svc,
                                           instance=inst, vid=v, dev_pid=p))
    except OSError:
        pass
    return out


def hid_devices(vid: str = "18D1", pid: str = "9450", any_hid: bool = False):
    """[(HostPid, 服务节点名)] —— 兼容老调用方的薄封装。"""
    return [(h.host_pid, h.svc) for h in hid_hosts(vid, pid, any_hid)]


def find_wudfhost_pid(vid: str = "18D1", pid: str = "9450",
                      any_hid: bool = False) -> int | None:
    """遥控器 HID 服务（0x1812）所在 WUDFHost 进程的 PID（找不到返回 None）。"""
    hosts = hid_hosts(vid, pid, any_hid)
    return hosts[0].host_pid if hosts else None


def process_image_path(pid: int) -> str | None:
    """进程映像的完整路径（读不到返回 None）。

    ⚠ 注入之前一定要看这一眼：**PID 会被回收**。我们从注册表读到的 HostPid，
    可能在"读到"和"attach"之间那个进程已经退出、PID 被系统分配给了别人 ——
    只看 PID 就 attach，等于把脚本注进一个**不相干**的进程。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, int(pid))    # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return None
            return buf.value or None
        finally:
            k32.CloseHandle(h)
    except Exception:                                  # noqa: BLE001
        return None


def is_wudfhost(pid: int) -> bool:
    """这个 PID 到底是不是系统的 WUDFHost.exe（P1-8 的"身份校验"）。

    ⚠ 为什么要查这一眼：**PID 会被回收**。我们从注册表读到的 HostPid，可能在
      "读到"和"attach"之间那个进程已经退出、PID 被系统分配给了别人 ——
      只看 PID 就 attach，等于把脚本注进一个**不相干**的进程。

    分三层，能查多严就查多严（两层都查不动时**放行**并留痕）：
      ① 直接读进程映像路径（要提权；非管理员时 `OpenProcess` 会给
         `ERROR_ACCESS_DENIED`）。读到了就必须是 `\\System32\\WUDFHost.exe`。
      ② 退一步问 `tasklist` 要 WUDFHost 的 PID 清单，我们那个必须在里面。
      ③ 都拿不到 → 放行。判 False 会让按键旁路**永远挂不上**，
         代价远大于"极低概率的 PID 复用"。
    """
    p = process_image_path(pid)
    if p:
        return p.replace("/", "\\").casefold().endswith("\\system32\\wudfhost.exe")
    known = wudfhost_pids()
    if known:
        return int(pid) in known
    return True


def identity_note(pid: int) -> str:
    """给日志用：这次身份是怎么核实的（或为什么没核实成）。"""
    p = process_image_path(pid)
    if p:
        return f"映像 {p}"
    known = wudfhost_pids()
    if known:
        return f"tasklist 里 {len(known)} 个 WUDFHost 之一"
    return "⚠ 无法核实身份（读不到映像路径、tasklist 也没输出）"


def wudfhost_pids() -> list[int]:
    """列出机器上所有 WUDFHost.exe 的 PID（诊断用）。

    一台机器上常有**好几个** WUDFHost（蓝牙、指纹、摄像头各一个），
    而"遥控器的按键读调用"只出现在服务它的那个里面。
    """
    import subprocess
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WUDFHost.exe", "/NH", "/FO", "CSV"],
            capture_output=True, timeout=15,
            creationflags=0x08000000).stdout or b""
    except Exception:                                  # noqa: BLE001
        return pids
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            text = out.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = out.decode("utf-8", "replace")
    for line in text.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower().startswith("wudfhost") and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


# ── 当前在跑的那一个旁路（供控制台「保存后等确认」用）───────────────────
#
# 为什么需要它：屏蔽表的下发是**异步**的，而 JS 侧是「按这张表把报告里的
# usage 原地写 0」。控制台保存完就回「已生效」的话，用户可能立刻按一个键，
# 而钩子还在用旧表 —— 看到的是"改了没用"。所以控制台要能拿到在跑的那个
# 实例、等它确认（2026-09-29 审查报告 P1-6）。
_ACTIVE: "RemoteHidTap | None" = None
_ACTIVE_LOCK = threading.Lock()


def active_tap() -> "RemoteHidTap | None":
    """当前在跑的按键旁路（没有则 None）。"""
    with _ACTIVE_LOCK:
        return _ACTIVE


def push_mapping(mapping: dict | None, wait: bool = False,
                 timeout: float = 1.5) -> bool:
    """把新的屏蔽表下发给**当前在跑**的旁路；返回「钩子确认收到了」。

    没有旁路在跑时返回 True —— 没什么要确认的，**别**让控制台把
    「没装 frida / 旁路没起来」报成「保存失败」：那会把用户支去查配置。
    """
    tap = active_tap()
    if tap is None:
        return True
    return tap.set_mapping(mapping, wait=wait, timeout=timeout)


class RemoteHidTap(threading.Thread):
    """后台线程：注入 WUDFHost → 收报告 → 解 usage → 回调 on_button(btn, down)。

    与 remote_hid.RemoteHidButtons 的接口保持一致（on_button(button_id, is_down)），
    所以 main.py 里两条路喂的是同一个回调、同一张映射表。
    """

    def __init__(self, on_button, vidpid: tuple[str, str] = ("18D1", "9450"),
                 *, allow_any_hid: bool = False,
                 expect_mac: str | None = None) -> None:
        super().__init__(daemon=True, name="frida-hid-tap")
        self._on = on_button
        self.vidpid = vidpid
        # ⚠ 默认**不许**回退到"任意 BLE HID 的第一项"（审查报告 P1-8）：
        #   同一个 WUDFHost 里可能还服务别的蓝牙键鼠，猜第一项 = 可能去
        #   抄/改写**别人**的报告。兼容款要显式打开（config: hid_frida_any_hid）。
        self.allow_any_hid = bool(allow_any_hid)
        # 远端 MAC（12 位小写 hex）。有它就要求节点名里**必须**出现 ——
        # 这比 VID/PID 硬：同型号的第二只遥控器 VID/PID 完全一样。
        self.expect_mac = (expect_mac or "").lower() or None
        self._stopped = threading.Event()
        self._script = None
        self._session = None
        self._attached_pid: int | None = None
        self._attached_at = 0.0
        self._mapping: dict = {}
        self._held: set[int] = set()          # 当前按住的 usage（过滤长按连发）
        self._last_down: str | None = None    # 松手时要补上的键
        self._recent: dict[tuple[str, bool], float] = {}
        self._hb: dict = {}
        self.ready = False
        self.note = "未启动"
        self.reports = 0
        self._compat = False
        self._av_hinted = False
        self._target_handle = ""      # 钩子确认锁定的 FileHandle（P1-8）
        self._lock = threading.Lock()
        # ── 屏蔽表的「序号 + 确认」（P1-6）─────────────────────────────
        # 用一把**独立的**锁：确认回调和 set_mapping 都在等/放它，
        # 和 _lock（只保护 _mapping 快照）分开，免得互相等出死锁。
        self._ack_lock = threading.Lock()
        self._ack_event = threading.Event()
        self._ack_seq = 0            # 已下发的最大序号
        self._acked_seq = -1         # 钩子已确认的最大序号

    # ── 对外 ─────────────────────────────────────────────────────────
    def set_mapping(self, mapping: dict | None, wait: bool = False,
                    timeout: float = 1.5) -> bool:
        """更新 keymap，并把「要抹掉原生动作的 usage」下发给钩子。

        `wait=True`：等到钩子回 ack 再返回；返回"是否真的等到了"。
        没挂上脚本时返回 False（＝没有钩子可以确认，调用方据此如实说话）。
        """
        with self._lock:
            self._mapping = dict(mapping or {})
        cc, vp = block_usages_for(self._mapping)

        script = self._script
        if script is None:
            # 还没挂上：表已经记住了，run() 挂上时会自己下发一次。
            return False

        with self._ack_lock:
            self._ack_seq += 1
            seq = self._ack_seq
            self._ack_event.clear()
        try:
            script.post({"type": "block", "cc": cc, "vendor": vp, "seq": seq})
        except Exception:                              # noqa: BLE001
            return False
        if not wait:
            return True
        self._ack_event.wait(timeout)
        with self._ack_lock:
            return self._acked_seq >= seq

    def stop(self) -> None:
        """停掉旁路。**必须等线程真的走完**，否则会留下"没人收的注入会话"。

        为什么顺序是「置位 → join → teardown」而不是「置位 → teardown」：
        线程可能正卡在 `_try_attach()` 里（`frida.attach()` 在本机能阻塞很久）。
        先 `_teardown()` 再 join 的话，线程会在两者之间**又挂上一个新会话** ——
        那个会话从此没人 detach，注入一直留着，直到进程退出。
        （审查报告 P2：`stop()` 不 join，注入竞态下可能残留会话。）

        ⚠ join 有超时：`frida.attach()` 卡住时不能把调用方（托盘退出）一起吊死。
        超时后仍然 teardown，并且 `_try_attach()` 在 attach 返回后会**再查一次**
        `self._stopped`，所以迟到的会话也会被自己拆掉。
        """
        global _ACTIVE
        self._stopped.set()
        if self.is_alive():
            try:
                self.join(timeout=8.0)
            except Exception:                          # noqa: BLE001
                pass
        self._teardown()
        with _ACTIVE_LOCK:
            if _ACTIVE is self:
                _ACTIVE = None

    def start(self) -> None:
        """登记为「当前在跑的旁路」，再起线程。"""
        global _ACTIVE
        with _ACTIVE_LOCK:
            _ACTIVE = self
        super().start()

    def _teardown(self) -> None:
        try:
            if self._script:
                self._script.unload()
        except Exception:                              # noqa: BLE001
            pass
        try:
            if self._session:
                self._session.detach()
        except Exception:                              # noqa: BLE001
            pass
        self._script = None
        self._session = None
        self._attached_pid = None
        self.ready = False
        self._target_handle = ""       # 新脚本要重新"认人"，旧句柄作废
        # 脚本没了 ⇒ 它的屏蔽表也跟着没了，确认状态必须一起作废：
        # 留着一个偏大的 _acked_seq，下一次 set_mapping(wait=True) 会**假确认**。
        with self._ack_lock:
            self._acked_seq = -1
            self._ack_event.clear()

    # ── 主循环 ───────────────────────────────────────────────────────
    def run(self) -> None:
        try:
            import frida                                # noqa: F401
        except ImportError:
            self.note = "缺 frida（按键旁路不可用，语音与 remote_hid 不受影响）"
            logger.info("🔓 按键旁路：未安装 frida，跳过（语音键仍可用；"
                        "pip install frida 可启用）")
            return

        try:
            source = _tap_js_path().read_text(encoding="utf-8")
        except OSError as e:
            self.note = f"读不到 frida_tap.js：{e}"
            logger.warning("🔓 按键旁路：%s", self.note)
            return

        # 重试节奏：Frida 第一次注入要**一次提权**（frida-helper）。失败就猛重试
        # 会把授权框反复弹出来（用户看到的是"怎么一直弹"）。所以指数退避，
        # 权限类失败连撞 3 次就停 5 分钟，只提示一次。
        delay = 2.0
        perm_fails = 0
        hinted = False
        while not self._stopped.is_set():
            try:
                pid, dev = self._next_host_pid()
                if not pid:
                    self.note = self._no_target_note()
                    if self._stopped.wait(min(delay, 10.0)):
                        break
                    delay = min(delay * 2, 30.0)
                    continue

                if self._session is None:
                    # ⚠ 注入前先核对身份：PID 会被回收，注册表里读到的 HostPid
                    #   可能在"读到"和"attach"之间被系统分配给了别的进程。
                    #   只看 PID 就 attach = 可能把脚本注进不相干的东西（P1-8）。
                    if not is_wudfhost(pid):
                        img = process_image_path(pid) or "(读不到)"
                        self.note = f"PID {pid} 不是 WUDFHost.exe（实际：{img}）→ 不注入"
                        logger.warning("🔓 按键旁路：%s", self.note)
                        if self._stopped.wait(min(delay, 10.0)):
                            break
                        delay = min(delay * 2, 30.0)
                        continue
                    logger.info("🔓 按键旁路：挂上蓝牙驱动宿主 WUDFHost (PID %d)%s"
                                "（身份核实：%s）",
                                pid, f" ← {dev}" if dev else "", identity_note(pid))
                    self._session = frida.attach(pid)
                    # ⚠ attach 返回后**再查一次**是否已被要求停止：`frida.attach()`
                    #   能阻塞很久，stop() 的 join 超时后可能走到 _teardown()，
                    #   而这一行才刚返回 —— 不查就会留下一个没人收的会话（P2）。
                    if self._stopped.is_set():
                        logger.info("🔓 按键旁路：attach 返回时已收到停止请求 → 立刻拆掉")
                        self._teardown()
                        return
                    self._script = self._session.create_script(source)
                    self._script.on("message", self._on_message)
                    # ⚠ PID 必须在 load() **之前**记上：脚本一 load 就同步发出
                    #   `ready` 消息，_on_message 里那行日志要用它；晚一步就会
                    #   打出「就绪（注入 WUDFHost PID None）」（踩过一次）。
                    self._attached_pid = pid
                    self._attached_at = time.time()
                    self._script.load()
                    self.set_mapping(self._mapping)
                    self._hb = {}
                    self._av_hinted = False
                    delay = 2.0
                    perm_fails = 0
                else:
                    # 宿主换了/没了就得重新挂：蓝牙重置、驱动重载都会让 WUDFHost
                    # 换 PID，而我们可能还挂在已经死掉的进程上。
                    # ⚠ 这里也用 _next_host_pid()（走同一套精确匹配 + allow_any_hid
                    #   开关），别再单独写一遍 `any_hid=True` 的兜底 —— 那等于
                    #   把 P1-8 的"默认不许猜第一项"在重挂路径上又放开了一次。
                    now, _dev = self._next_host_pid()
                    now = now or self._attached_pid
                    if self._attached_pid and (now != self._attached_pid
                                               or not self._pid_alive(self._attached_pid)):
                        logger.info("🔓 按键旁路：宿主换了或没了（PID %s → %s），重新挂",
                                    self._attached_pid, now)
                        self._teardown()
                        continue
                    time.sleep(1.0)
            except Exception as e:                      # noqa: BLE001
                self._teardown()
                name, text = type(e).__name__, str(e)
                perm = ("PermissionDenied" in name or "AccessDenied" in name
                        or "unable to access" in text)
                if perm:
                    perm_fails += 1
                    if perm_fails >= 3:
                        delay = 300.0
                        self.note = ("按键旁路需要一次管理员授权（已暂停自动重试，"
                                     "5 分钟后再试一次）")
                        if not hinted:
                            hinted = True
                            logger.warning(
                                "🔓 按键旁路：注入需要提权，但授权没给 —— 已停下不再反复"
                                "弹框。想用按键映射请让程序以管理员身份跑一次。")
                    else:
                        self.note = f"挂载失败（{perm_fails}/3）：需要一次提权授权"
                        delay = min(delay * 2, 30.0)
                else:
                    self.note = f"挂载失败：{name}: {text}"
                    if ("TransportError" in name or "connection is closed" in text) \
                            and not self._av_hinted:
                        self._av_hinted = True
                        logger.warning(
                            "🔓 按键旁路：注入被中断了（TransportError）—— 装了杀软/EDR 的"
                            "机器（公司电脑尤其常见）几乎都是安全软件拦的。正规解法是请 IT"
                            "把程序目录加进白名单（我们不改系统设置、不关 Defender）。"
                            "放行之前语音功能不受影响，只是方向键/OK/音量用不了。")
                    delay = min(delay * 2, 60.0)
                logger.info("🔓 按键旁路：%s（%.0f 秒后重试）", self.note, delay)
                if self._stopped.wait(delay):
                    break

    def _next_host_pid(self):
        """挑出「就是这台遥控器」的那个 WUDFHost；挑不到返回 (None, "")。

        顺序（P1-8 的"精确验证"就体现在这里）：
          1. 服务节点名必须解析出 **VID/PID 完全相等**（不是子串命中）；
          2. 有远端 MAC 时，节点名里**必须**带这个 MAC（同型号的第二只遥控器
             VID/PID 一模一样，只有 MAC 分得开）；
          3. 都挑不出来时，只有显式打开 `allow_any_hid` 才退回"任意 BLE HID"，
             并且**标记 _compat** —— 日志/自检里要能看出"这次是猜的"。
        """
        hosts = hid_hosts(*self.vidpid)
        if self.expect_mac:
            exact = [h for h in hosts if h.mac == self.expect_mac]
            if exact:
                self._compat = False
                return exact[0].host_pid, exact[0].key
            # 配对上了 MAC 却对不上：可能换了遥控器 / 地址轮换 / 记录没刷新。
            # 退回 VID/PID 匹配（仍然精确到型号），并在日志里说一声。
            if hosts:
                logger.info("🔓 按键旁路：节点名里没找到远端 MAC %s，"
                            "退回按 VID/PID 匹配（%d 个候选）",
                            self.expect_mac, len(hosts))
        if hosts:
            self._compat = False
            return hosts[0].host_pid, hosts[0].key
        if not self.allow_any_hid:
            return None, ""
        anyh = hid_hosts(*self.vidpid, any_hid=True)
        if not anyh:
            return None, ""
        self._compat = True
        logger.warning("🔓 按键旁路：没找到 VID/PID = %s/%s 的 BLE HID 设备，"
                       "按 hid_frida_any_hid=true 退到第一台 BLE HID 设备（%s）"
                       "—— 这台**可能不是你的遥控器**，请核对日志里的节点名。",
                       self.vidpid[0], self.vidpid[1], anyh[0].key)
        return anyh[0].host_pid, anyh[0].key

    def _no_target_note(self) -> str:
        """找不到目标时，写给用户看的那句话（要能指向下一步）。"""
        n_all = len(hid_hosts(*self.vidpid, any_hid=True))
        v, p = self.vidpid
        if n_all:
            return (f"没找到 VID/PID = {v}/{p} 的 BLE HID 驱动宿主"
                    f"（本机另有 {n_all} 台别的 BLE HID 设备）。"
                    "若这就是你的遥控器，请在 config.json 里把 "
                    "hid_frida_any_hid 设为 true（兼容款兜底，注意可能认错设备）。")
        return ("没找到遥控器的 HID 驱动宿主（遥控器配对了吗？"
                "先在 Windows 设置里确认它已连接）")

    def _pid_alive(self, pid: int) -> bool:
        """进程还在不在。⚠ 两个坑：OpenProcess 失败不一定是"进程不存在"
        （权限不足也会失败），那种必须当"活着"，否则会疯狂重挂。"""
        if not pid:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if h:
                k32.CloseHandle(h)
                return True
            return ctypes.get_last_error() != 87           # 87 = 进程不存在
        except Exception:                                  # noqa: BLE001
            return True

    # ── 消息处理 ─────────────────────────────────────────────────────
    def _on_message(self, message, data) -> None:
        if message.get("type") == "error":
            logger.warning("🔓 按键旁路脚本错误：%s",
                           str(message.get("description"))[:200])
            return
        payload = message.get("payload") or {}
        kind = payload.get("kind")
        if kind == "ready":
            self.ready = True
            self.note = "运行中"
            logger.info("✅ 按键旁路：就绪（注入 WUDFHost PID %s）", self._attached_pid)
        elif kind == "report":
            try:
                raw = bytes.fromhex(str(payload.get("raw") or "").replace(" ", ""))
            except ValueError:
                return
            self._handle_report(raw)
        elif kind == "target":
            # 钩子锁定了目标设备的 FileHandle（P1-8）。从这一刻起它**只**改写
            # 这一个句柄的报告；宿主里别的蓝牙键鼠一个字节都不动。
            self._target_handle = str(payload.get("handle") or "")
            n_h = int(payload.get("handles") or 0)
            logger.info("🎯 按键旁路：已锁定目标设备句柄 %s（本宿主共见 %d 个句柄）"
                        "—— 从此只改写这一个设备的报告",
                        self._target_handle or "?", n_h)
            if n_h > 1:
                logger.warning(
                    "⚠ 这个 WUDFHost 里还有别的 HID 设备（共 %d 个句柄）。"
                    "已只对本机的目标句柄改写，其它设备的报告一个字节都不动；"
                    "若发现按键串台，请把这段日志发出来。", n_h)
        elif kind == "block_ack":
            # 钩子确认收到了这一版屏蔽表 → 放行在 set_mapping(wait=True) 里等的人。
            # ⚠ 只认**序号够新**的那条：下发是异步的，乱序/迟到的 ack 如果也算数，
            #   控制台就会在旧表还在用时报告「已生效」。
            with self._ack_lock:
                try:
                    seq_i = int(payload.get("seq"))
                except (TypeError, ValueError):
                    # 老脚本不带 seq（兼容）→ 当作"就是当前这一版"
                    seq_i = self._ack_seq
                if seq_i >= self._ack_seq:
                    self._acked_seq = seq_i
                    self._ack_event.set()
            logger.info("🔓 按键旁路：已屏蔽 %s 个键的原生动作",
                        payload.get("count", 0))
        elif kind == "hb":
            self._hb = payload
        elif kind == "error":
            logger.warning("🔓 按键旁路：%s", payload.get("message"))

    def _handle_report(self, raw: bytes) -> None:
        # ⚠ 方法名不能叫 `_handle` —— Python 3.13 的 threading.Thread 自带一个
        #   同名属性（OS 线程句柄），子类一覆盖它，`start()` 就会报
        #   "'_thread._ThreadHandle' object is not callable"。踩过一次。
        self.reports += 1
        btn, is_down = decode_tap_report(raw)
        if btn == "<up>":
            btn = self._last_down
            is_down = False
        if not btn:
            return
        now = time.time()
        key = (btn, is_down)
        if now - self._recent.get(key, 0.0) < 0.06:
            return                                     # 去抖 / 两路重复
        self._recent[key] = now
        if is_down:
            self._last_down = btn
        else:
            self._last_down = None if self._last_down == btn else self._last_down
        # ⚠ raw 字节默认**不记**（P2-6）：只对开发有用，且能反推用户按了什么。
        #   要看的时候把 config.json 的 log_raw_hid 改成 true 再重启。
        logger.info("🔘 按键旁路 → 按钮「%s」%s%s",
                    btn, "按下" if is_down else "松开", logsetup.raw_suffix(raw))
        try:
            self._on(btn, is_down)
        except Exception as e:                          # noqa: BLE001
            logger.exception("按键旁路回调异常：%s", e)

    # ── 诊断 ─────────────────────────────────────────────────────────
    def selfcheck(self) -> str:
        """按需输出「钩子到底看到了什么」（供控制台/命令行排查）。

        真机上出现过"钩子挂上了、显示就绪、按键却完全没反应"，只看成功/失败
        分不清是"挂错了宿主"还是"这款遥控器报文格式不同"。这几个原始计数能分辨：
          · 读调用 0 次 + 收到 0 条   → 宿主不对
          · 读调用 N 次、长度里没有 3 → 报文格式不同（另一款遥控器）
          · 长度里有 3 / 收到过报文   → 这条路是通的
        """
        hb = self._hb or {}
        lens = hb.get("lens") or {}
        dist = "、".join(f"{k}字节×{v}" for k, v in sorted(lens.items())) or "无"
        seen = hb.get("seen") or {}
        names = []
        for k, n in sorted(seen.items(), key=lambda kv: -int(kv[1]))[:14]:
            try:
                kind, code = str(k).split(":", 1)
                code = int(code)
            except (ValueError, TypeError):
                continue
            if kind == "cc":
                nm = CC_USAGE_TO_BUTTON.get(code, "未知")
                names.append(f"{nm}(cc:0x{code:04X})×{n}")
            else:
                names.append(f"(vp:0x{code:02X})×{n}")
        n_h = int(hb.get("handles") or 0)
        locked = bool(hb.get("locked"))
        if locked:
            tgt = "目标句柄 已锁定"
        else:
            tgt = ("目标句柄 **未锁定**（还没见到遥控器格式的报文，"
                   "这段时间一个字节都没改）")
        extra = ""
        if n_h > 1:
            extra = (f"，因不是目标而放过 {int(hb.get('skipped') or 0)} 次改写")
        compat = ""
        if self._compat:
            compat = " ⚠ 走的是 hid_frida_any_hid 兼容兜底（VID/PID 没对上，可能认错设备）。"
        return (f"按键旁路自检：宿主 PID {self._attached_pid}"
                f"（本机共 {len(wudfhost_pids())} 个 WUDFHost），"
                f"读调用 {int(hb.get('ioctl') or 0)} 次，输出长度：{dist}；"
                f"已收到 {self.reports} 条按键报告，已屏蔽 {int(hb.get('blocked') or 0)} 次。"
                f" {tgt}（本宿主共见 {n_h} 个句柄{extra}）。"
                + compat
                + (" 已见按键：" + "、".join(names) if names else ""))
