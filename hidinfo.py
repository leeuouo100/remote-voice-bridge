"""
遥控器的 HID 硬件身份探测 —— 不按任何键，先把"这台机器到底认不认得它"查清楚。

## 为什么需要这个模块

2026-09-15 武哥报：「遥控器上除了语音键，其他所有按键都没有作用」。

这个现象有一条**极其干净的分界线**，可惜此前没人利用：

    · 语音键 走 ATVV（BLE GATT 自定服务）→ 能用  ⇒ BLE 链路、GATT、ATVV 代码全好
    · 其余键 走 HID（HOGP 蓝牙键盘协议）   → 全废  ⇒ 问题**只可能在 HID 这一层**

`tools/diag_remote.py` 只能在"按键之后"看现象，答不了"按键之前设备是什么状态"。
本模块补的就是这一段：把 Windows 自己的账本翻出来看。

## 真机验证过的事实（2026-09-15，Chromecast Voice Remote，VID 0x18D1 / PID 0x9450）

遥控器只暴露 **5 个 HID 集合**，这是理解一切的关键：

| 集合 | UsagePage/Usage      | 含义       | Windows 会做什么        | 输入报告 |
|------|----------------------|------------|-------------------------|----------|
| Col01| 0x01 / 0x06 键盘      | 键盘       | ✅ 变成键盘事件          | 9 字节   |
| Col02| 0x0C / 0x01 消费类    | 媒体/浏览器| ✅ 变成媒体键/浏览器键   | 4 字节   |
| Col03| 0x01 / 0x02 鼠标      | 鼠标       | ✅ 变成鼠标移动/点击     | 5 字节   |
| Col04| 0xFF01 / 0x01 厂商    | 厂商自定义 | ❌ **什么都不做**        | 21 字节  |
| Col05| 0xFF80 / 0x00 厂商    | 厂商自定义 | ❌ **什么都不做**        | 21 字节  |

两个厂商页的输入报告都是 **21 字节**（比键盘的 9 字节大得多），按键状态很可能就
躺在这里。而 Windows 的 HID 栈**不会**把厂商页的报告翻译成任何按键事件 ——
这正是"除语音键外全部按键都没反应"的最强候选解释。

所以本报告的结论里特意区分两件事：
  · **设备在不在 / 驱动绑没绑上**（注册表 + 在线枚举就能答，本模块负责）
  · **按键发在哪一页**（要按下才知道，交给 tools/watch_reports.py）

## 三路证据

1. `SYSTEM\\CurrentControlSet\\Enum\\HID\\<硬件ID>\\<实例>` ——
   BLE HID 设备的每个 top-level collection。**层级踩过坑**：`ColNN` 是**硬件 ID 键名**
   的一部分（形如 `{00001812-…}_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c&Col01`），
   不是它下面的子键；子键只有 `Device Parameters` / `Properties`。
   关键值在**实例那一层**：`Service` / `ConfigFlags` / `DeviceDesc` / `HardwareID`。
   硬件 ID 里的 `00001812-…` 就是 HID-over-GATT 服务 UUID ⇒ 这路是走蓝牙低功耗键盘协议来的。

2. SetupAPI + `HidP_GetCaps` —— 当前**在线**集合的 UsagePage/Usage + 输入报告长度。
   这一路才能把"厂商自定义页"点出来。

3. `SYSTEM\\CurrentControlSet\\Enum\\BTHLE` / `BTHENUM` —— 蓝牙友好名与地址。

三路合起来才能把话说死。任何一路缺失都只降级为"证据不足"，不猜。

## ctypes 踩过的坑（别再犯）

· `SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize` 在 64 位是 8、32 位是 6，
  写错不算失败、直接 `ERROR_INVALID_NAME(123)` —— 路径得出来是残缺的。
  **本模块不再依赖偏移量**：直接在缓冲区里找 `\\\\?\\` 前缀，绕开这个坑。
· 用 0（无访问权限）打开的句柄 `HidD_GetAttributes` / `HidP_GetCaps` 能用，
  但 `DeviceIoControl` 取报告描述符会 `ERROR_INVALID_FUNCTION(1)`。
  取描述符必须用 `GENERIC_READ` 开。
· 取报告描述符的 IOCTL 是 **0x000B0193**（不是网上常见的 0x000B0190）。
  实测 0x000B0190 返回 err=1，0x000B0193 成功。且它返回的是 hidparse 的
  "HidP KDR" 结构，**不是原始描述符**，别拿去当描述符解析。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

try:
    import winreg
except ImportError:                                    # 非 Windows
    winreg = None                                       # type: ignore[assignment]

# Chromecast 系遥控器在 Windows 上的 VID（Google）。
GOOGLE_VID = 0x18D1

# 取报告描述符的 IOCTL。见文件头"踩过的坑"——是 0x93 结尾，不是 0x90。
_IOCTL_HID_GET_REPORT_DESCRIPTOR = 0x000B0193

# 厂商自定义用法页的下界：0xFF00 及以上都是，Windows 一律不处理。
_VENDOR_PAGE_MIN = 0xFF00

# (UsagePage, Usage) → (中文名, Windows 会不会处理, 说明)
_COLLECTION_ROLES: dict[tuple[int, int], tuple[str, bool, str]] = {
    (0x01, 0x06): ("键盘", True, "Windows 会把它当键盘事件处理"),
    (0x01, 0x02): ("鼠标", True, "Windows 会把它当鼠标移动 / 点击处理"),
    (0x01, 0x07): ("小键盘", True, "Windows 会把它当小键盘处理"),
    (0x01, 0x80): ("系统控制", True, "Windows 会把它当系统键（休眠/电源）处理"),
    (0x0C, 0x01): ("消费类控制", True, "Windows 会把它当媒体键 / 浏览器键处理"),
}


def collection_role(page: int, usage: int) -> tuple[str, bool, str]:
    """(页, 用法) → (中文名, Windows 是否处理, 说明)。厂商页单独点名。"""
    hit = _COLLECTION_ROLES.get((page, usage))
    if hit:
        return hit
    if page >= _VENDOR_PAGE_MIN:
        return ("厂商自定义", False,
                "⚠ Windows 对厂商自定义页**不做任何事**：这一路的按键不会产生"
                "键盘/鼠标/媒体事件，任何映射表都够不到它")
    return (f"未登记页 0x{page:02X}", False,
            f"不在已知表里（0x{page:02X}/0x{usage:02X}），行为未知")


# ── 第 1、3 路：注册表 ────────────────────────────────────────────────────────

def _reg_children(path: str) -> list[str]:
    """列子键名。任何失败都返回空列表 —— 探测工具不许抛异常。"""
    if winreg is None:
        return []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            n = winreg.QueryInfoKey(k)[0]
            return [winreg.EnumKey(k, i) for i in range(n)]
    except OSError:
        return []


def _reg_values(path: str) -> dict:
    """读一个键的所有值。"""
    if winreg is None:
        return {}
    out: dict = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            n = winreg.QueryInfoKey(k)[1]
            for i in range(n):
                name, val, _t = winreg.EnumValue(k, i)
                out[name] = val
    except OSError:
        pass
    return out


def _bt_addr_from_hwid(hwid: str) -> str:
    """从硬件 ID 里挖出蓝牙地址。

    ID 形如 `…_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c&Col01`：
    最后一段（`_` 切分）去掉 `&ColNN` 就是地址。
    ⚠ 不能用"取所有 12 位十六进制串"那种做法 —— 会把标准蓝牙基址 UUID 里的
      `00805f9b34fb` 之类的片段也当成地址，得出一个假的 MAC。
    """
    tail = str(hwid).rsplit("_", 1)[-1]
    addr = tail.split("&")[0].strip().lower()
    if len(addr) == 12 and all(c in "0123456789abcdef" for c in addr):
        return ":".join(addr[i:i + 2] for i in range(0, 12, 2)).upper()
    return ""


def _col_index(hwid: str) -> str:
    """从硬件 ID 里取 `ColNN` 标记（没有就返回空）。"""
    for part in str(hwid).split("&"):
        if part.lower().startswith("col"):
            return part.upper()
    return ""


def registry_hid_collections() -> list[dict]:
    """注册表里的遥测 HID 集合（含 BLE/HOGP 判定）。

    ⚠ 层级（真机验证）：`Enum\\HID\\<硬件ID>\\<实例>`，值挂在**实例**层，
      不是 `\\Device Parameters` 下。ColNN 在**硬件 ID** 里。
    """
    if winreg is None:
        return []
    base = r"SYSTEM\CurrentControlSet\Enum\HID"
    out: list[dict] = []
    for hwid in _reg_children(base):
        for inst in _reg_children(f"{base}\\{hwid}"):
            vals = _reg_values(f"{base}\\{hwid}\\{inst}")
            if not vals:
                continue
            service = str(vals.get("Service", "") or "")
            out.append({
                "hwid": hwid,
                "instance": inst,
                "col": _col_index(hwid),
                "hogp": "00001812" in hwid.lower(),
                "service": service,
                "desc": str(vals.get("DeviceDesc", "") or ""),
                "config_flags": vals.get("ConfigFlags", 0),
                "guid": str(vals.get("ClassGUID", "") or ""),
                "bt_addr": _bt_addr_from_hwid(hwid),
                # kbdhid = 这一路被 Windows 绑成了键盘；这是"键能不能到 Windows"的总闸
                "is_keyboard": service.lower() == "kbdhid",
                "is_mouse": service.lower() == "mouhid",
                "enabled": vals.get("ConfigFlags", 0) == 0,
            })
    return out


def _ble_devices() -> list[dict]:
    """BTHLE（低功耗）+ BTHENUM（经典）里的蓝牙设备，含友好名与地址。"""
    out: list[dict] = []
    for hive_path, kind in (
        (r"SYSTEM\CurrentControlSet\Enum\BTHLE", "低功耗 BLE"),
        (r"SYSTEM\CurrentControlSet\Enum\BTHENUM", "经典蓝牙"),
    ):
        for dev in _reg_children(hive_path):
            for inst in _reg_children(f"{hive_path}\\{dev}"):
                vals = _reg_values(f"{hive_path}\\{dev}\\{inst}")
                if not vals:
                    continue
                out.append({
                    "kind": kind,
                    "key": dev,
                    "friendly": str(vals.get("FriendlyName", "") or ""),
                    "desc": str(vals.get("DeviceDesc", "") or ""),
                    "enabled": vals.get("ConfigFlags", 0) == 0,
                })
    return out


# ── 第 2 路：SetupAPI + hid.dll（在线集合）────────────────────────────────────

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", _GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wintypes.DWORD), ("VendorID", wintypes.WORD),
                ("ProductID", wintypes.WORD), ("VersionNumber", wintypes.WORD)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


# HID 设备接口类 GUID：{4D1E55B2-F16F-11CF-88CB-001111000030}
_GUID_DEVINTERFACE_HID = _GUID(
    0x4D1E55B2, 0xF16F, 0x11CF,
    (ctypes.c_byte * 8)(0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30),
)

_GENERIC_READ = 0x80000000
_SHARE_RW = 0x00000003
_OPEN_EXISTING = 3
_DIGCF_PRESENT = 0x02
_DIGCF_DEVICEINTERFACE = 0x10
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_HIDP_STATUS_SUCCESS = 0x00110000


def _extract_path(buf) -> str:
    """从 detail 缓冲区里取设备路径。

    ⚠ 不按 `cbSize` 偏移取（32/64 位不一样，错了就得到残缺路径 + err 123），
      直接找 `\\\\?\\` 前缀，两边都能用。
    """
    txt = buf.raw.decode("utf-16-le", errors="ignore")
    for mark in ("\\\\?\\", "\\??\\"):
        i = txt.find(mark)
        if i >= 0:
            return txt[i:].split("\x00")[0]
    return ""


def _open_hid(path: str):
    """打开 HID 集合句柄。先试读权限（取描述符要用），退回无权限（读 caps 够用）。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    for access in (_GENERIC_READ, 0):
        h = kernel32.CreateFileW(path, access, _SHARE_RW, None, _OPEN_EXISTING, 0, None)
        if h and h != _INVALID_HANDLE:
            return kernel32, h
    return kernel32, None


def live_hid_collections() -> list[dict]:
    """当前在线的 HID 集合，含 UsagePage/Usage 与输入报告长度。

    ⚠ 非 Windows / 任何调用失败都返回空列表。空 = "没查到"，**不等于"没有设备"**。
    """
    if not hasattr(ctypes, "WinDLL"):
        return []
    try:
        setupapi = ctypes.WinDLL("setupapi")
        hid = ctypes.WinDLL("hid")
    except OSError:
        return []

    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
        wintypes.DWORD, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDP_CAPS)]
    hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(_HIDD_ATTRIBUTES)]

    devs = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(_GUID_DEVINTERFACE_HID), None, None,
        _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if not devs or devs == _INVALID_HANDLE:
        return []

    out: list[dict] = []
    try:
        i = 0
        while True:
            ifd = _SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    devs, None, ctypes.byref(_GUID_DEVINTERFACE_HID), i, ctypes.byref(ifd)):
                break
            i += 1
            need = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                devs, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            if not need.value:
                continue
            buf = ctypes.create_string_buffer(need.value)
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    devs, ctypes.byref(ifd), buf, need.value, ctypes.byref(need), None):
                continue
            path = _extract_path(buf)
            if not path:
                continue

            kernel32, h = _open_hid(path)
            if h is None:
                continue
            try:
                attrs = _HIDD_ATTRIBUTES()
                attrs.Size = ctypes.sizeof(_HIDD_ATTRIBUTES)
                vid = pid = 0
                if hid.HidD_GetAttributes(ctypes.c_void_p(h), ctypes.byref(attrs)):
                    vid, pid = attrs.VendorID, attrs.ProductID
                page = usage = -1
                in_len = 0
                pp = ctypes.c_void_p()
                if hid.HidD_GetPreparsedData(ctypes.c_void_p(h), ctypes.byref(pp)) and pp:
                    try:
                        caps = _HIDP_CAPS()
                        if hid.HidP_GetCaps(pp, ctypes.byref(caps)) == _HIDP_STATUS_SUCCESS:
                            page, usage = caps.UsagePage, caps.Usage
                            in_len = caps.InputReportByteLength
                    finally:
                        hid.HidD_FreePreparsedData(pp)
                out.append({
                    "path": path, "vid": vid, "pid": pid,
                    "usage_page": page, "usage": usage,
                    "in_len": in_len,
                    "is_google": vid == GOOGLE_VID,
                })
            finally:
                kernel32.CloseHandle(ctypes.c_void_p(h))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return out


def report_descriptor_length(path: str) -> int:
    """取报告描述符长度（IOCTL 0x000B0193）。

    ⚠ 返回的是 hidparse 的 "HidP KDR" 结构，**不是原始描述符**，
      所以这里只报长度做证据，不尝试解析。取不到返回 0。
    """
    kernel32, h = _open_hid(path)
    if h is None:
        return 0
    try:
        kernel32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p]
        buf = ctypes.create_string_buffer(8192)
        ret = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            ctypes.c_void_p(h), _IOCTL_HID_GET_REPORT_DESCRIPTOR, None, 0,
            buf, 8192, ctypes.byref(ret), None)
        return int(ret.value) if ok else 0
    except Exception:                                   # noqa: BLE001
        return 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(h))


# ── 程序配置摘要（"什么都不发生"的另一半原因）──────────────────────────────

def config_summary() -> list[str]:
    """把生效中的 config.json 关键项列出来。

    为什么要放进硬件报告里：**"按键没反应"还有一个同样常见的原因 —— 映射被关了
    或全被清空**。这个原因不看配置永远查不出来，而配置就在本机、读一下零成本。
    """
    try:
        from config import Config, CONFIG_DIR
    except Exception:                                   # noqa: BLE001
        return ["    （读不到 config 模块）"]
    try:
        c = Config.load()
    except Exception as e:                              # noqa: BLE001
        return [f"    ⚠ 读配置失败：{e}"]

    km = getattr(c, "keymap", {}) or {}
    live = {k: v for k, v in km.items() if str(v or "").strip()}
    blank = [k for k, v in km.items() if not str(v or "").strip()]
    enabled = bool(getattr(c, "mapping_enabled", True))

    L = [
        f"    配置文件：{CONFIG_DIR / 'config.json'}",
        f"    映射总开关 mapping_enabled = {enabled}"
        + ("" if enabled else "   ← ⚠ 关着！所有映射都不会执行"),
        f"    拦截原生按键 suppress_keys = {bool(getattr(c, 'suppress_keys', False))}",
        f"    输入法触发方式 input_method = {getattr(c, 'input_method', '?')!r}",
        f"    按住说话按键 voice_ptt_keys = {getattr(c, 'voice_ptt_keys', None)}",
        f"    有映射的按键 {len(live)} 个，空映射（＝按了没反应）{len(blank)} 个"
        + (f"：{'、'.join(sorted(blank))}" if blank else ""),
    ]
    if not enabled or not live:
        L.append("    → ❌ 光看配置就能解释「按键没反应」："
                 + ("映射总开关是关的。" if not enabled else "所有按键映射都是空的。"))
    else:
        L.append("    → 配置层面正常：有 " + str(len(live)) + " 个按键配了动作，"
                 "若仍无反应，问题在按键事件有没有到达 Windows（见上面结论 0）。")
    return L


# ── 汇总 ─────────────────────────────────────────────────────────────────────

def probe(with_descriptor_len: bool = False) -> dict:
    """把三路证据收成一个字典。任何一路失败都只留空，绝不抛异常。"""
    live = live_hid_collections()
    if with_descriptor_len:
        for d in live:
            d["desc_len"] = report_descriptor_length(d["path"])
    return {
        "cols": registry_hid_collections(),
        "live": live,
        "ble": _ble_devices(),
    }


def _remote_ble(ble: list[dict]) -> list[dict]:
    out = []
    for d in ble:
        blob = (d.get("friendly", "") + " " + d.get("desc", "")).lower()
        if "chromecast" in blob or "remote" in blob:
            out.append(d)
    return out


def format_report(snap: dict | None = None, indent: str = "  ") -> str:
    """生成中文报告，**先给结论再给证据** —— 读者是拿它来判断下一步的。"""
    s = snap if snap is not None else probe()
    cols = list(s.get("cols") or [])
    live = list(s.get("live") or [])

    hogp = [c for c in cols if c.get("hogp")]
    kbd = [c for c in cols if c.get("is_keyboard")]
    google_live = [d for d in live if d.get("is_google")]
    vendor_live = [d for d in google_live if d.get("usage_page", 0) >= _VENDOR_PAGE_MIN]

    L: list[str] = []
    A = L.append

    A("【结论 0】遥控器的 HID 键盘在这台机器上是什么状态？（不用按任何键）")
    A("")

    if not cols:
        A("  → ⚠ 注册表里没有任何 HID 设备记录（也可能是读取被拒）。")
        A("     证据不足，无法判定。请把本报告发出来，不要据此改设置。")
    elif not hogp:
        A("  → ❌ 没有任何「蓝牙低功耗键盘（HOGP）」集合。")
        A("     含义：Windows 从来没把遥控器认成键盘。除语音键外的按键")
        A("           一个都不会变成 Windows 事件 —— **映射表怎么改都没用**。")
        A("     做法：删掉蓝牙里的遥控器重新配对（配对时别急着关设置页，")
        A("           等系统把「HID Keyboard Device」装出来再关）。")
    elif not kbd:
        A("  → ❌ 有 HOGP 集合，但**没有任何一路被绑成键盘驱动（Service≠kbdhid）**。")
        A("     含义：设备在了，Windows 却没把键盘那一路接上 → 按键不会有反应。")
        A("     做法：设备管理器 → 键盘 → 卸载「HID Keyboard Device」后")
        A("           断开/重连蓝牙，让 Windows 重新装一次驱动。")
    else:
        dis = [c for c in kbd if not c.get("enabled")]
        A(f"  → ✅ 有 {len(kbd)} 路已被绑成键盘驱动（Service=kbdhid）。")
        if dis:
            A(f"     ⚠ 其中 {len(dis)} 路 ConfigFlags≠0（被禁用或驱动没就绪）："
              + "、".join(c["col"] for c in dis))
            A("       含义：这些集合的按键不会被 Windows 处理，需在设备管理器里启用。")
        else:
            A("     含义：键盘那一路是好的，**部分按键应该能到达 Windows**。")
            A("           若「所有按键都没反应」，请看下面的证据 2：")
            A("           遥控器还有两个**厂商自定义页**，Windows 对它们完全不理。")
            A("           按键到底发在哪一页，要按一次键才知道 ——")
            A("           安装版再跑一次「遥控器诊断」（报告末尾的【结论 4】就是这一段），")
            A("           源码版可跑 python tools/watch_reports.py 单独长听。")

    if vendor_live:
        A("")
        A(f"  → ⚠ 另注意：遥控器暴露了 {len(vendor_live)} 个**厂商自定义**集合"
          f"（用法页 {'、'.join('0x%02X' % d['usage_page'] for d in vendor_live)}），")
        A("     Windows 对它们**不做任何事**。若按键发在这里，任何映射都够不到。")
        A("     验证办法：再跑一次「遥控器诊断」看报告末尾的【结论 4】"
          "（源码版：python tools/watch_reports.py），")
        A("     按下按键，看哪一路集合收到了原始报告。")

    A("")
    A("  ── 证据 1：注册表里的 HID 集合（Windows 的账本）──")
    if not cols:
        A("    （无）")
    # ⚠ 只展开 HOGP（＝遥控器）那几行。
    #   本机还有键盘、鼠标、蓝牙耳机……全列出来会把遥控器的 5 行淹没掉，
    #   而这份报告的读者是要"一眼看到遥控器是什么状态"。
    #   另外同一路常常有**两条一模一样的记录**（同一设备重新配对后，
    #   旧 devnode 会留着），按内容去重，否则 5 行变 10 行更难对。
    seen: set[tuple] = set()
    others = 0
    for c in sorted(cols, key=lambda x: (not x["hogp"], x["col"])):
        if not c["hogp"]:
            others += 1
            continue
        sig = (c["col"], c["service"], c["desc"], c["config_flags"], c["bt_addr"])
        if sig in seen:
            continue
        seen.add(sig)
        bound = c["service"] or "(未绑定)"
        A(f"    {c['col'] or '—':<7}Service={bound:<8} "
          f"ConfigFlags={c['config_flags']}  {c['desc']}")
    if others:
        A(f"    （另有 {others} 条其它来源的 HID 记录：本机的键盘/鼠标等，与本问题无关）")
    addrs = sorted({c["bt_addr"] for c in cols if c["hogp"] and c.get("bt_addr")})
    if addrs:
        A(f"    遥控器蓝牙地址：{'、'.join(addrs)}")

    A("")
    A("  ── 证据 2：当前在线的集合 + 它们是什么（HidP_GetCaps）──")
    if not live:
        A("    （没枚举到 —— 可能是权限/会话限制，**不代表设备不存在**）")
    others_live = 0
    for d in live:
        # 同证据 1：只展开 Google（遥控器）那几路，其余只报个数。
        if not d.get("is_google"):
            others_live += 1
            continue
        if d["usage_page"] < 0:
            A(f"    VID=0x{d['vid']:04X} PID=0x{d['pid']:04X}  （读不到 caps）")
            continue
        name, handled, why = collection_role(d["usage_page"], d["usage"])
        mark = "✅" if handled else "❌"
        extra = f"  descLen={d['desc_len']}" if d.get("desc_len") else ""
        A(f"    {mark} 用法页=0x{d['usage_page']:04X}/{d['usage']:04X}  "
          f"输入报告 {d['in_len']} 字节  {name}{extra}")
        if not handled:
            A(f"       └ {why}")
    if others_live:
        A(f"    （另有 {others_live} 个其它 HID 集合：本机键盘/鼠标等，与本问题无关）")
    if not google_live:
        A("    ⚠ 在线集合里没看到 Google 设备（VID 0x18D1）—— 遥控器此刻没连上？")
    else:
        pages = {d["usage_page"] for d in google_live}
        if 0x01 not in pages:
            A("    ⚠ Google 设备**没有暴露键盘页（0x01）** —— "
              "键在 Windows 这一侧根本不会被当键盘事件。")

    A("")
    A("  ── 证据 3：程序配置（'什么都没发生'的另一半原因）──")
    L.extend(config_summary())

    A("")
    A("  ── 证据 4：蓝牙设备清单 ──")
    hits = _remote_ble(s.get("ble") or [])
    if hits:
        for d in hits:
            A(f"    {d.get('friendly') or d.get('desc') or d['key']}"
              f"（{d['kind']}，{'已启用' if d['enabled'] else '已禁用/未启用'}）")
    else:
        A("    （没找到名字里带 Remote / Chromecast 的蓝牙设备）")

    return "\n".join(indent + ln if ln else "" for ln in L)


def main() -> int:
    """python hidinfo.py —— 打印报告；没找到 HID 集合时返回 1。"""
    try:
        from _utf8 import setup as _setup_utf8
    except ImportError:
        _setup_utf8 = None
    if _setup_utf8:
        _setup_utf8()
    snap = probe(with_descriptor_len=True)
    print(format_report(snap, indent=""))
    return 0 if snap.get("cols") else 1


if __name__ == "__main__":
    sys.exit(main())
