# -*- coding: utf-8 -*-
"""
蓝牙配对自检与修复 —— 专治「换 USB 口 → 本地蓝牙地址变 → 配对记录作废」。

现象（2026-09-15 真机，武哥的两台机器都中过）
--------------------------------------------
遥控器昨天还好好的，今天程序一直显示「未连接」，而 Windows 设置里明明写着
「Chromecast Remote 已配对 电量 88%」。把这个设备删掉也删不动，设置页卡在
「正在删除设备」。重装程序、重装驱动都没用。

根因
----
Windows 的蓝牙配对记录是**按本地无线电的地址**存的：

    HKLM\\SYSTEM\\CurrentControlSet\\Services\\BTHPORT\\Parameters\\Devices
        \\<远端MAC>\\ServicesFor<本地适配器MAC>

而这颗 USB 蓝牙棒的本地地址是**按 USB 实例（也就是插在哪个口）**缓存的：

    HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB\\<硬件ID>\\<实例>\\Device Parameters
        \\DeviceAddressCache = "047f0ef2d294"

于是把蓝牙棒换一个 USB 口，Windows 就给它一个新地址，而配对记录还绑在旧地址上。
表现是一串互相矛盾的事实：
  · AQS 还能枚举到这台设备（因为记录还在）
  · Windows 设置显示「已配对」，甚至能报电量（缓存值）
  · `from_id_async()` 直接抛 E_INVALIDARG，`unpair_async()` 返回 Failed
    —— 蓝牙栈照着那张记录去造设备对象，却找不到对应地址的无线电
  · 设置页「删除设备」卡死（删除也是照同一张记录去做的）

本模块做什么
------------
只读诊断（**不需要管理员**）：

    python pairing.py

自动修复（**需要管理员**，脚本会自己弹 UAC 提权）：

    python pairing.py --fix              # 按 migrate → rebuild 的阶梯自动试
    python pairing.py --fix --method migrate
    python pairing.py --fix --method rebuild
    python pairing.py --fix --method restore        # 只在"地址改得动"的机器上用
    python pairing.py --fix --method purge --yes    # 兜底：清掉记录，重新配对

四种修法：
  migrate  把配对记录**补到当前地址**下（复制 ServicesFor 子键、同步关联节点的
           Bluetooth_UniqueID）。不碰地址、不碰配对数据。
  rebuild  再进一步：把**关联节点**（Enum\BTHLE\Dev_<远端>）删掉让 Windows 重建。
           它们的 HardwareID 里写死了旧地址，而设备 ID 就是从那儿来的 ——
           只改 Bluetooth_UniqueID 没用（实测：改完枚举出来的 ID 还是旧地址）。
           配对记录**保留**，所以重建成功就不用重新配对。
  restore  把**当前这颗蓝牙棒的地址**改回记录绑的那个。
           ⚠ 2026-09-15 真机实测**改不动**：写进 DeviceAddressCache、重启设备、
           重启蓝牙服务之后读回来还是原值（驱动自己会把值写回去）。
           所以它不在默认阶梯里，只留给"地址真的由缓存决定"的机器。
  purge    前面都没戏时的兜底：备份后清掉记录与关联节点，让用户重新配对
           （顺带治好那个卡死的「正在删除设备」）。

默认阶梯 = migrate → rebuild，两条都**不需要重新配对**。

排查用：
    python pairing.py --acl-probe        # 提权后只探"关联节点删不删得动"，不删东西
    python pairing.py --acl-dump         # 提权后把该键的所有者 + 每条 ACE 打出来
                                         #   （rc=5 只知道"被拒"，不知道"被谁拒"）

删不动的时候（ACL 只放 SYSTEM）：
    python pairing.py --fix --method purge --yes --take-ownership
    `--take-ownership` 会先把这个键的**所有权拿过来**（Administrators），
    再改 DACL，再删 —— 这是"设置里删不动"的唯一正规出路。
    ⚠️ 它会改变系统键的 ACL（所有者 SYSTEM → Administrators），所以**默认不开**。

打包/安装版入口：`RemoteVoiceBridgeDiag.exe --fix-pairing`
（安装目录里的「修复蓝牙配对.bat」就是这个）

本模块刻意**只用标准库**（winreg / ctypes / subprocess），winrt 是可选增强：
BLE 已经坏掉的时候恰恰是它最需要能跑起来的时候，不能反过来依赖它。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ── 注册表路径常量 ───────────────────────────────────────────────────────────
BTHPORT_DEVICES = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
BTHPORT_KEYS = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Keys"
ENUM_USB = r"SYSTEM\CurrentControlSet\Enum\USB"
ENUM_BTHLE = r"SYSTEM\CurrentControlSet\Enum\BTHLE"

ADDR_CACHE_VALUE = "deviceaddresscache"      # 大小写不敏感比较
SERVICES_FOR = "servicesfor"

# winreg 没有导出 DELETE 这个权限位（KEY_WRITE 里也不含它）。
# 删键真正要的就是它 —— 探测可删性必须按它来，用 KEY_SET_VALUE 会漏判
# "能写、不能删"那种 ACL（外表看着能改，DeleteKey 照样 PermissionError）。
DELETE_ACCESS = 0x00010000

# ── 接管 ACL 用的 Win32 常量 ─────────────────────────────────────────────────
# 全都写死成常量而不是"看着差不多就用"，因为这类 API 传错一个位**不报错**，
# 只是行为不对（本项目在 hidinfo 那边已经吃过一次同样味道的亏）。
SE_REGISTRY_KEY = 4                 # SE_OBJECT_TYPE
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
WRITE_OWNER = 0x00080000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
ACL_REVISION = 2
CONTAINER_INHERIT_ACE = 0x2
OBJECT_INHERIT_ACE = 0x1
KEY_ALL_ACCESS = 0xF003F
WIN_BUILTIN_ADMINISTRATORS_SID = 26  # WELL_KNOWN_SID_TYPE

# 配置目录与主程序一致（%APPDATA%\remote-voice-bridge），报告就放这里，
# 用户直接把文件拖进对话即可。
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "remote-voice-bridge"
REPORT_TXT = CONFIG_DIR / "pairing-fix.txt"
REPORT_JSON = CONFIG_DIR / "pairing-fix.json"
LOG_TXT = CONFIG_DIR / "pairing-fix.log"
BACKUP_DIR = CONFIG_DIR / "backup"

_CREATE_NO_WINDOW = 0x08000000


class _Tee:
    """把输出同时写到控制台和文件。

    为什么要它：提权那一次是在**另一个控制台窗口**里跑的，父进程抓不到它的
    stdout —— 而"备份了哪些键、动了什么、验收结果"恰恰是最需要留底的东西。
    不给它落盘的话，用户关掉窗口就什么都不剩了（v1.0.9 的教训就是
    "输出去了没人看得到的地方"，这次不想再犯一遍）。
    """

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:                   # noqa: BLE001
                pass
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:                   # noqa: BLE001
                pass

    def __getattr__(self, item):
        return getattr(self._streams[0], item)


def _setup_utf8() -> None:
    """中文输出在 cp1252 / cp936 控制台上不该抛异常（乱码可以接受，崩了不行）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                       # noqa: BLE001
            pass


# ── 地址格式 ─────────────────────────────────────────────────────────────────
def hex12(addr: str | None) -> str:
    """'04:7F:0E:F2:D2:94' / '047f0ef2d294' → '047f0ef2d294'（小写、无分隔）。"""
    if not addr:
        return ""
    return re.sub(r"[^0-9a-fA-F]", "", str(addr)).lower().ljust(12, "0")[:12]


def pretty(addr: str | None) -> str:
    """'047f0ef2d294' → '04:7F:0E:F2:D2:94'。"""
    h = hex12(addr)
    if len(h) != 12:
        return str(addr or "")
    return ":".join(h[i:i + 2].upper() for i in range(0, 12, 2))


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                           # noqa: BLE001
        return False


def _run(cmd: list[str], timeout: int = 90) -> tuple[int, str]:
    """跑一条系统命令，把 stdout+stderr 一起拿回来（不弹黑框）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace", creationflags=_CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:                      # noqa: BLE001
        return -1, f"{e.__class__.__name__}: {e}"


# ── 注册表读取 ───────────────────────────────────────────────────────────────
def _subkeys(path: str, hive=None):
    import winreg
    hive = hive or winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(hive, path) as k:
            return [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
    except Exception:                           # noqa: BLE001
        return []


def _values(path: str, hive=None) -> dict:
    import winreg
    hive = hive or winreg.HKEY_LOCAL_MACHINE
    out: dict = {}
    try:
        with winreg.OpenKey(hive, path) as k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                n, v, _t = winreg.EnumValue(k, i)
                out[n] = v
    except Exception:                           # noqa: BLE001
        pass
    return out


def _values_typed(path: str) -> list[tuple[str, object, int]]:
    """带类型的值列表 —— migrate 搬家时必须**原样保留类型**。

    为什么不能用 _values()：ServicesFor 底下混了 REG_DWORD 和 REG_QWORD
    （`LEExtendedDeviceInfoFlags` 就是 64 位）。照 DWORD 写回去会把高 32 位
    默默丢掉 —— 注册表不会报错，配对状态却会变得不对。
    """
    import winreg
    out: list[tuple[str, object, int]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                n, v, t = winreg.EnumValue(k, i)
                out.append((n, v, t))
    except Exception:                           # noqa: BLE001
        pass
    return out


def _read_addr_cache(instance_path: str) -> str:
    """读某个 USB 实例的 DeviceAddressCache（12 位十六进制字符串）。"""
    for n, v in _values(instance_path + r"\Device Parameters").items():
        if n.lower() == ADDR_CACHE_VALUE:
            return hex12(v)
    return ""


# ── 设备在场判定（cfgmgr32，不需要管理员）────────────────────────────────────
DN_STARTED = 0x00000008
DN_DRIVER_LOADED = 0x00000002
DN_HAS_PROBLEM = 0x00000400


def _cfgmgr32():
    """配置好原型的 cfgmgr32 —— **必须显式给 argtypes/restype**。

    踩过的坑：不声明原型时 ctypes 把第三个参数（ulFlags）按 32 位 int 传，
    x64 下寄存器高 32 位是脏的 → `CM_Locate_DevNodeW` 对所有设备都返回失败，
    于是**正在用的**那颗蓝牙棒也被标成"历史残留"。这个错法不报任何异常，
    只是结论全反 —— 比崩溃更难发现。
    """
    cfg = ctypes.windll.cfgmgr32
    if getattr(cfg, "_rvb_typed", False):
        return cfg
    cfg.CM_Locate_DevNodeW.restype = ctypes.c_ulong
    cfg.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(ctypes.c_ulong),
                                       ctypes.c_wchar_p, ctypes.c_ulong]
    cfg.CM_Get_DevNode_Status.restype = ctypes.c_ulong
    cfg.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(ctypes.c_ulong),
                                          ctypes.POINTER(ctypes.c_ulong),
                                          ctypes.c_ulong, ctypes.c_ulong]
    cfg._rvb_typed = True
    return cfg


def devnode_status(instance_id: str):
    """返回 ((status, problem) , 0)；定位不到设备返回 (None, rc)。

    为什么不用 ConfigFlags 猜：`Enum` 里的 ConfigFlags=0x20 只是"待重装"标记，
    不代表设备现在不在。真正的在场信息在 PnP 管理器里，只有 cfgmgr32 说了算
    （实测：本机 `0&3` → CR_NO_SUCH_DEVINST(13)，`0&2` → status 0x0180600A 已启动）。
    """
    try:
        cfg = _cfgmgr32()
        devinst = ctypes.c_ulong(0)
        rc = cfg.CM_Locate_DevNodeW(ctypes.byref(devinst),
                                    ctypes.c_wchar_p(instance_id), 0)
        if rc != 0:
            return None, rc
        status = ctypes.c_ulong(0)
        problem = ctypes.c_ulong(0)
        rc2 = cfg.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem),
                                        devinst, 0)
        if rc2 != 0:
            return None, rc2
        return (status.value, problem.value), 0
    except Exception:                           # noqa: BLE001
        return None, -1


def usb_bt_adapters() -> list[dict]:
    """列出所有 USB 蓝牙适配器实例（Service=BTHUSB），标出哪个真的在场。

    同一颗蓝牙棒每插过一个口就留下一个实例 —— 只有一个是"在用"的，
    其余是历史残留（ConfigFlags / 定位信息都还在，容易看错，故一并列出来）。
    """
    out: list[dict] = []
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ENUM_USB) as k:
            hwids = [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
    except Exception:                           # noqa: BLE001
        return out

    for hwid in hwids:
        if "VID_" not in hwid.upper():
            continue
        for inst in _subkeys(f"{ENUM_USB}\\{hwid}"):
            # ⚠ 两个"实例 ID"长得像但不是一回事，别混：
            #   · PnP 实例 ID（喂 cfgmgr32 / pnputil）：`USB\VID_x&PID_y\<实例>`
            #     —— 必须带 **USB\ 这个枚举器前缀**，少了它 CM_Locate_DevNodeW
            #     对所有设备都返回 CR_NO_SUCH_DEVINST(13)，于是"在用的那颗"
            #     被标成历史残留（这个错法不报错、只给反结论）。
            #   · 注册表路径（喂 winreg）：`Enum\USB\<硬件ID>\<实例>`，**不带**前缀。
            reg_path = f"{hwid}\\{inst}"
            iid = f"USB\\{reg_path}"
            vals = _values(f"{ENUM_USB}\\{reg_path}")
            if str(vals.get("Service", "")) != "BTHUSB":
                continue
            st, rc = devnode_status(iid)
            started = bool(st and (st[0] & DN_STARTED))
            loaded = bool(st and (st[0] & DN_DRIVER_LOADED))
            problem = st[1] if (st and (st[0] & DN_HAS_PROBLEM)) else 0
            out.append({
                "instance_id": iid,
                "reg_path": reg_path,
                "hwid": hwid,
                "inst": inst,
                "desc": str(vals.get("DeviceDesc", "")).split(";")[-1],
                "location": str(vals.get("LocationInformation", "")),
                "config_flags": int(vals.get("ConfigFlags", 0) or 0),
                "addr": _read_addr_cache(f"{ENUM_USB}\\{reg_path}"),
                "present": bool(started or (loaded and rc == 0)),
                "started": started,
                "problem_code": problem,
            })
    return out


def read_pairing_records() -> list[dict]:
    """BTHPORT 里的配对记录：每个远端设备 + 它绑在哪些本地地址上。"""
    out: list[dict] = []
    for remote in _subkeys(BTHPORT_DEVICES):
        if not re.fullmatch(r"[0-9a-fA-F]{12}", remote):
            continue
        vals = _values(f"{BTHPORT_DEVICES}\\{remote}")
        name = vals.get("Name") or vals.get("LEName") or b""
        if isinstance(name, bytes):
            name = name.rstrip(b"\x00").decode("utf-8", "replace")
        services = []
        for sub in _subkeys(f"{BTHPORT_DEVICES}\\{remote}"):
            m = re.fullmatch(SERVICES_FOR + r"([0-9a-fA-F]{12})", sub, re.I)
            if m:
                services.append({
                    "addr": hex12(m.group(1)),
                    "value_count": len(_values(f"{BTHPORT_DEVICES}\\{remote}\\{sub}")),
                })
        out.append({"remote": remote.lower(), "name": str(name).strip(),
                    "services_for": services})
    return out


def read_aep_nodes() -> list[dict]:
    """Enum\\BTHLE 下的关联节点（遥控器在设备管理器里的那一堆子设备）。

    `Device Parameters\\Bluetooth_UniqueID` 形如 `Dev_<本地MAC>_<远端MAC>`
    —— 这就是"设备 ID 里那个本地地址"的来源，也是它为什么会过期。
    """
    out: list[dict] = []
    for devkey in _subkeys(ENUM_BTHLE):
        m = re.fullmatch(r"Dev_([0-9a-fA-F]{12})", devkey)
        if not m:
            continue
        remote = hex12(m.group(1))
        for inst in _subkeys(f"{ENUM_BTHLE}\\{devkey}"):
            p = f"{ENUM_BTHLE}\\{devkey}\\{inst}"
            uid = str(_values(p + r"\Device Parameters").get("Bluetooth_UniqueID", ""))
            vals = _values(p)
            # ★ 这个节点在当前适配器下**真的存在**吗？
            # 它是"幽灵"的话，设备 ID 就指向一个不存在的父实例 ——
            # 枚举得到、打不开（E_INVALIDARG），而 HardwareID 里根本看不出旧地址
            # （实测：HardwareID 只有 `BTHLE\Dev_<远端>`，地址不在那儿）。
            st, rc = devnode_status(f"BTHLE\\{devkey}\\{inst}")
            out.append({
                "key": p,
                "devkey": devkey,
                "instance": inst,
                "unique_id": uid,
                "local": hex12(uid[4:16]) if uid.startswith("Dev_") else "",
                "remote": remote,
                "friendly": str(vals.get("FriendlyName", "")),
                "present": bool(st and (st[0] & DN_DRIVER_LOADED)),
                "devnode_status": f"0x{st[0]:08X}" if st else f"rc={rc}",
            })
    return out


def read_key_material(remote: str) -> dict:
    """配对密钥材料在不在（`Parameters\\Keys` 下的内容）。

    ⚠ 这个键默认连读都不给（ACL 只放 SYSTEM），所以非管理员拿到的永远是空 ——
    **空 ≠ 没有**。只有提权之后读到的空才能当结论用。
    这条决定了 restore/migrate 有没有可能成功：密钥没了就只能重新配对。
    """
    import winreg
    info: dict = {"readable": False, "locals": [], "detail": {}}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BTHPORT_KEYS) as k:
            locals_ = [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
        info["readable"] = True
    except Exception as e:                      # noqa: BLE001
        info["detail"]["error"] = f"{e.__class__.__name__}: {getattr(e, 'winerror', e)}"
        return info

    for loc in locals_:
        peers = _subkeys(f"{BTHPORT_KEYS}\\{loc}")
        if remote.lower() in [p.lower() for p in peers]:
            sub = f"{BTHPORT_KEYS}\\{loc}\\{remote.lower()}"
            info["locals"].append({
                "local": hex12(loc),
                "peers": peers,
                "remote_values": sorted(_values(sub).keys()),
                "remote_subkeys": _subkeys(sub),
            })
    info["detail"]["all_locals"] = locals_
    return info


# ── 活适配器地址 ─────────────────────────────────────────────────────────────
def live_addr_winrt() -> str:
    """用 WinRT 问"现在生效的无线电地址"（读不到返回 ''）。"""
    try:
        import asyncio
        from winrt.windows.devices.bluetooth import BluetoothAdapter
    except Exception:                           # noqa: BLE001
        return ""

    async def _get():
        a = await BluetoothAdapter.get_default_async()
        if a is None:
            return ""
        v = getattr(a, "bluetooth_address", None)
        return f"{v:012x}" if isinstance(v, int) else ""

    try:
        return hex12(asyncio.run(_get()))
    except Exception:                           # noqa: BLE001
        return ""


def live_addr_registry(adapters: list[dict] | None = None) -> str:
    """退路：在场蓝牙实例的 DeviceAddressCache（正常和 WinRT 报的一致）。

    ⚠ 有多颗**在场**适配器、且地址各不相同的时候，这里**故意返回空**：
    猜错了会去改错那颗无线电的地址，后果比"读不到"严重得多。
    （本机的英特尔板载蓝牙是幽灵 —— 注册表里有它、还有自己的
    DeviceAddressCache，但 PnP 里根本不存在，正是这种"看着像在场"的假象。）
    """
    ads = adapters if adapters is not None else usb_bt_adapters()
    present = [a["addr"] for a in ads if a["present"] and a["addr"]]
    if len(set(present)) == 1:
        return present[0]
    if not present:
        for a in ads:
            if a["addr"] and not (a["config_flags"] & 0x20):
                return a["addr"]
    return ""


def live_addr(adapters: list[dict] | None = None) -> tuple[str, str]:
    """(地址, 来源)。优先 WinRT —— 它是"现在真的在用的那个"，注册表只是缓存。"""
    a = live_addr_winrt()
    if a:
        return a, "winrt"
    a = live_addr_registry(adapters)
    return a, ("registry" if a else "none")


# ── 诊断 ─────────────────────────────────────────────────────────────────────
def classify(addr: str, records: list[dict], nodes: list[dict]) -> list[dict]:
    """纯函数：活地址 + 配对记录 + 关联节点 → 每条记录的状态。

    为什么单独抽出来：这段判定是**整个工具的脑子**，而它读的是真机注册表。
    不抽出来的话就没法在没插蓝牙棒的 CI 上验它 —— 那种代码第一次运行
    就是在用户机器上（本项目已经栽过好几次）。见 tools/check_pairing.py。
    """
    targets: list[dict] = []
    for r in records:
        bound = [s["addr"] for s in r["services_for"]]
        mine = [n for n in nodes if n["remote"] == r["remote"]]
        # present 缺省当真：假数据（check_pairing.py）里没有这个字段，
        # 而它检的是判定逻辑本身，不是 PnP 状态。
        live_nodes = [n for n in mine
                      if n["local"] == addr and n.get("present", True)]
        # 「幽灵」= **任何**挂在不存在的适配器实例下的节点，不限于当前地址。
        # 为什么必须放宽（2026-09-15 真机）：migrate 把记录补成"两个地址都绑"
        # 之后，关联节点还挂在**旧地址**上、并且是幽灵 —— 此时"活地址上的幽灵"
        # 是空的，只看它会掉进 UNKNOWN（报告里显示"看不出来"），而病根一目了然。
        ghost = [n for n in mine if not n.get("present", True)]
        stale_nodes = [n for n in mine if n["local"] and n["local"] != addr]

        if addr and addr in bound and live_nodes:
            status = "OK"
        elif bound and addr and addr not in bound:
            # ★ v1.0.9 那个事故：记录绑的是**另一个**本地地址。
            # 这一格必须排在 NODE_PHANTOM **前面** —— 真机形态下节点同时也是
            # 过期的幽灵，先判 NODE_PHANTOM 会把"记录作废"这个根因盖掉（闸会红）。
            status = "STALE_ADDR"
        elif bound and mine and not live_nodes:
            # 记录提到了当前地址，但**没有一个是可用节点**：
            # 要么设备 ID 里带的是旧地址，要么挂在已经不存在的适配器实例下。
            # 症状都是"枚举得到、打不开"，处置也一样（删掉让 Windows 重建）。
            status = "NODE_PHANTOM"
        elif addr and addr in bound and not mine:
            status = "RECORD_OK_NO_NODE"      # 记录对，但关联节点被清掉了
        elif not bound:
            status = "RECORD_NO_ADDRESS"
        else:
            status = "UNKNOWN"

        targets.append({
            "remote": r["remote"], "name": r["name"], "bound": bound,
            "record_addr": bound[0] if bound else "",
            "status": status,
            "stale_nodes": [n["key"] for n in stale_nodes],
            "live_nodes": [n["key"] for n in live_nodes],
            "phantom_nodes": [n["key"] for n in ghost],
        })
    return targets


def overall_status(records: list[dict], targets: list[dict]) -> str:
    """把每条记录的状态归成一个总状态（决定后面走哪条修复阶梯）。"""
    if not records:
        return "NO_RECORD"
    if len(targets) == 1:
        return targets[0]["status"]
    if any(t["status"] == "STALE_ADDR" for t in targets):
        return "STALE_ADDR"
    if all(t["status"] == "OK" for t in targets):
        return "OK"
    return "MIXED"


def diagnose(verify: bool = True) -> dict:
    """把"配对记录 / 关联节点 / 活地址"三样东西摆在一起，给出一个明确的状态。

    `verify=True` 时还会**真的把设备打开一次**当终审 —— 理由见下面那段注释。
    """
    adapters = usb_bt_adapters()
    addr, src = live_addr(adapters)
    records = read_pairing_records()
    nodes = read_aep_nodes()
    keys = read_key_material(records[0]["remote"]) if len(records) == 1 else {}

    targets = classify(addr, records, nodes)
    d = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "admin": is_admin(),
        "live_addr": addr,
        "live_addr_source": src,
        "adapters": adapters,
        "records": records,
        "nodes": nodes,
        "keys": keys,
        "targets": targets,
        "status": overall_status(records, targets),
    }

    # ── 终审：真的把设备打开一次 ──
    # 为什么非要这一步：2026-09-15 实测 —— 配对记录补齐了、两个关联节点的
    # Bluetooth_UniqueID 也改成当前地址了，诊断判成 OK，**而设备照样打不开**
    # （枚举出来的设备 ID 里还是旧地址，from_id_async 依旧 E_INVALIDARG）。
    # "注册表看起来一致"和"真的能用"是两件事。这个项目已经在
    # "看着没事其实坏了"上栽过太多次，所以结论一律以能不能打开为准。
    if verify and len(targets) == 1:
        ok, msg = verify_open(targets[0]["remote"])
        d["openable"] = ok
        d["open_message"] = msg
        if not ok and not msg.startswith("SKIPPED"):
            # 只在"状态本来看着没问题"时才兜底成 UNRESOLVABLE ——
            # 它的文案是"记录看着没问题，但打不开"，套在 STALE_ADDR /
            # NODE_PHANTOM 这种**已经看出病根**的情况上反而把结论说糊了。
            #
            # RECORD_OK_NO_NODE 也在此列之外：关联节点一条不剩时，"打不开"
            # 是**必然**的（没有节点就没有设备对象），不是新发现。
            # 2026-09-15 实测：清干净节点之后诊断改口说"看不出问题但要重建"，
            # 反而把"节点已清空、就等它重建"这个准确结论丢了。
            if d["status"] in ("OK", "MIXED", "UNKNOWN"):
                d["status"] = "UNRESOLVABLE"
    return d


STATUS_TEXT = {
    "OK": "配对记录与当前无线电一致，而且设备真的能打开",
    "STALE_ADDR": "★ 配对记录绑在**另一个**本地蓝牙地址上（换过 USB 口 / 换过棒子）",
    "NODE_PHANTOM": "★ 关联节点不可用（幽灵 / 设备 ID 里带的是旧地址）"
                     " —— 记录留着，删掉节点让 Windows 按当前适配器重建即可",
    "RECORD_OK_NO_NODE": "配对记录是对的，但关联节点丢了（重建即可）",
    "RECORD_NO_ADDRESS": "配对记录里没有 ServicesFor 子键（记录已损坏）",
    "NO_RECORD": "根本没有配对记录 —— 需要配对一次",
    "MIXED": "多个设备，状态不一致（逐个看下面）",
    "UNRESOLVABLE": "★ 记录看着没问题，但设备**打不开** —— 光看地址是不够的，要重建关联节点",
    "UNKNOWN": "看不出来（结构不符合已知的任何一种）",
}


def status_needs_fix(status: str) -> bool:
    """这个状态要不要修。OK / 空的才算健康。"""
    return status not in ("OK", "")


def format_report(d: dict) -> str:
    L: list[str] = []
    A = L.append
    A("=" * 72)
    A(f" 蓝牙配对自检  ·  {d['time']}   管理员权限：{'有' if d['admin'] else '无'}")
    A("=" * 72)
    A("")
    A(f"当前生效的无线电地址：{pretty(d['live_addr']) or '（读不到）'}"
      f"   [来源：{d['live_addr_source']}]")
    A("")
    A(f"■ 判定：{STATUS_TEXT.get(d['status'], d['status'])}")
    A("")

    A("── USB 蓝牙适配器实例 ────────────────────────────────────────────")
    if not d["adapters"]:
        A("  （一个都没有）")
    for a in d["adapters"]:
        A(f"  {'✅在场' if a['present'] else '·历史'}  {pretty(a['addr'])}  {a['location']}")
        A(f"          {a['instance_id']}")
        A(f"          {a['desc']}  ConfigFlags={a['config_flags']}"
          + (f"  问题代码={a['problem_code']}" if a["problem_code"] else ""))
    A("")

    A("── 配对记录（BTHPORT\\Parameters\\Devices）─────────────────────────")
    for t in d["targets"]:
        A(f"  设备：{t['name'] or '(无名)'}  [{pretty(t['remote'])}]")
        A(f"    记录绑定的本地地址：{', '.join(pretty(x) for x in t['bound']) or '（无）'}")
        A(f"    状态：{STATUS_TEXT.get(t['status'], t['status'])}")
        if t["stale_nodes"]:
            A(f"    过期的关联节点 {len(t['stale_nodes'])} 个（设备 ID 里带的是旧地址 → "
              "from_id_async 会抛 E_INVALIDARG）")
        if t.get("phantom_nodes"):
            # 一个节点可以同时"地址过期"又"是幽灵"。两行各数一遍会让用户以为
            # 节点数是两倍 —— 诊断工具最不该在数量上含糊，所以把重叠说破。
            dup = len(set(t["phantom_nodes"]) & set(t["stale_nodes"]))
            A(f"    幽灵关联节点 {len(t['phantom_nodes'])} 个"
              + (f"（其中 {dup} 个同时也是过期节点）" if dup else "")
              + "（挂在已经不存在的适配器实例下 → 枚举得到、打不开）")
        if t["live_nodes"]:
            A(f"    可用的关联节点 {len(t['live_nodes'])} 个")
    if not d["targets"]:
        A("  （空）")
    A("")

    A("── 关联节点（Enum\\BTHLE）─────────────────────────────────────────")
    for n in d["nodes"]:
        if not n["local"]:
            # local 读不到 = `Device Parameters\Bluetooth_UniqueID` 没了
            # （半删事故的残留，2026-09-15 真机见过）。
            # 这跟"地址过期"是两码事，报成"地址过期"会让人去查错方向。
            tag = "  ← 设备 ID 丢了（Bluetooth_UniqueID 已被删）"
        elif n["local"] != d["live_addr"]:
            tag = "  ← 地址过期"
        elif not n.get("present", True):
            tag = "  ← 幽灵（父实例已不存在）"
        else:
            tag = "  ✅ 可用"
        A(f"  local={pretty(n['local'])}  remote={pretty(n['remote'])}  "
          f"{n['friendly']}{tag}")
        A(f"      devnode={n.get('devnode_status', '?')}   {n['key']}")
    A("")

    k = d.get("keys") or {}
    A("── 配对密钥材料（Parameters\\Keys）────────────────────────────────")
    if not k.get("readable"):
        A("  ⚠ 读不到（这个键只放 SYSTEM，非管理员一律 Access Denied）——")
        A("    **不代表没有**，要用管理员身份再跑一次才算数。")
    elif k["locals"]:
        for x in k["locals"]:
            A(f"  ✅ 本地 {pretty(x['local'])} 下有本设备的密钥："
              f"{x['remote_values'] or x['remote_subkeys'] or '（空）'}")
    else:
        A(f"  ❌ 提到了所有本地地址下的记录，都没有本设备的密钥条目："
          f"{k.get('detail', {}).get('all_locals')}")
        A("     → 密钥已经丢了，restore / migrate 都没用，只能重新配对。")
    A("")

    if d.get("openable") is not None:
        A("── 终审：设备到底能不能打开 ──────────────────────────────────────")
        A(f"  {'✅ 能' if d['openable'] else '❌ 不能'}：{d.get('open_message', '')}")
        A("  （这一步是真的去 from_id_async，不是看注册表猜的 —— 注册表一致"
          "不等于能用）")
        A("")
    A("=" * 72)
    return "\n".join(L)


# ── 备份 ─────────────────────────────────────────────────────────────────────
def backup(d: dict) -> list[str]:
    """动注册表之前先把要动的东西导出成 .reg。返回生成的文件列表。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    made: list[str] = []

    jobs = [(BTHPORT_DEVICES, "bthport-devices"),
            (BTHPORT_KEYS, "bthport-keys")]
    for n in d["nodes"]:
        jobs.append((n["key"], f"bthle-{hex12(n['remote'])}"))
    for a in d["adapters"]:
        jobs.append((f"{ENUM_USB}\\{a['reg_path']}\\Device Parameters",
                     f"addr-{a['inst']}"))

    for path, tag in jobs:
        f = BACKUP_DIR / f"{stamp}-{tag}.reg"
        rc, out = _run(["reg", "export", f"HKLM\\{path}", str(f), "/y"], timeout=60)
        if rc == 0:
            made.append(str(f))
        else:
            # Keys 之类受保护的键偶尔导不出来 —— 只提示，不中断备份流程
            print(f"   （次要）导出失败：{path} —— {out.strip().splitlines()[:1]}")

    # 地址缓存的原值单独记一份文本，万一 .reg 不好用也能照着手抄回去
    txt = BACKUP_DIR / f"{stamp}-原始值.txt"
    with open(txt, "w", encoding="utf-8") as fh:
        for a in d["adapters"]:
            fh.write(f"{a['instance_id']}\tDeviceAddressCache={a['addr']}\n")
    made.append(str(txt))
    return made


# ── 重启无线电 ───────────────────────────────────────────────────────────────
def restart_radio(instance_id: str = "") -> None:
    """让 Windows 重新初始化这颗蓝牙棒（改了地址缓存之后必须做）。

    顺序很讲究：先把 USB 设备本身重启一遍（驱动会重新读 Device Parameters），
    再重启蓝牙服务（蓝牙栈重建、重新枚举已配对设备）。
    """
    if instance_id:
        rc, out = _run(["pnputil", "/restart-device", instance_id], timeout=120)
        print(f"   pnputil /restart-device → rc={rc}"
              + (f"  {out.strip().splitlines()[0]}" if out.strip() else ""))
    rc, out = _run(["powershell", "-NoProfile", "-Command",
                    "Restart-Service -Name bthserv -Force"], timeout=150)
    if rc != 0:
        _run(["net", "stop", "bthserv", "/y"], timeout=120)
        _run(["net", "start", "bthserv"], timeout=120)
    time.sleep(4)


def read_addr_fresh() -> str:
    """在**新进程**里读地址（同进程内 WinRT 会把地址缓存住，读不到变化）。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--pairing-probe-addr"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--pairing-probe-addr"]
    for _ in range(6):
        rc, out = _run(cmd, timeout=40)
        m = re.search(r"^ADDR=([0-9a-fA-F]{12})\s*$", out, re.M)
        if m:
            return hex12(m.group(1))
        time.sleep(2)
    return ""


# ── 修法 1：把适配器地址改回记录绑定的那个 ───────────────────────────────────
def fix_restore(d: dict, target: str = "", dry: bool = False) -> tuple[bool, str]:
    import winreg
    t = [x for x in d["targets"] if x["status"] == "STALE_ADDR"]
    if not t:
        return False, "没有处于 STALE_ADDR 状态的设备，restore 用不上"
    want = hex12(target) or t[0]["record_addr"]
    if not want:
        return False, "记录里没有可用地址"

    live = [a for a in d["adapters"] if a["present"] and a["addr"] != want]
    if not live:
        return False, f"没有在场、且地址不是 {pretty(want)} 的适配器实例可改"
    if dry:
        return True, f"[试运行] 会把 {len(live)} 个实例的地址改成 {pretty(want)}"

    for a in live:
        path = f"{ENUM_USB}\\{a['reg_path']}\\Device Parameters"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "DeviceAddressCache", 0, winreg.REG_SZ,
                                  hex12(want))
            print(f"   ✍  {a['instance_id']} 地址 {pretty(a['addr'])} → {pretty(want)}")
        except Exception as e:                  # noqa: BLE001
            return False, f"写注册表失败：{e.__class__.__name__} {e}"

    restart_radio(live[0]["instance_id"])
    now = read_addr_fresh()
    if hex12(now) == hex12(want):
        return True, f"地址已改回 {pretty(want)}，配对记录重新有效"
    return False, (f"改完地址是 {pretty(now) or '读不到'}（期望 {pretty(want)}）"
                   " —— 驱动没接受这个值")


# ── 修法 2：把配对记录搬到当前地址下 ─────────────────────────────────────────
def fix_migrate(d: dict, dry: bool = False) -> tuple[bool, str]:
    import winreg
    if not d["live_addr"]:
        return False, "读不到当前无线电地址"
    live = d["live_addr"]

    # 需要补的就是"当前地址那一份不存在"的记录 —— 不按 status 判断。
    # 因为 status 可能是 NODE_PHANTOM / UNRESOLVABLE（记录其实也缺），
    # 按 STALE_ADDR 卡着的话这一步会被跳过、永远修不动。
    todo = [x for x in d["targets"] if live not in x["bound"]]
    if not todo:
        return False, "配对记录里已经有当前地址那一份了"
    if dry:
        return True, f"[试运行] 会把 {len(todo)} 个记录补到 {pretty(live)} 下"

    for x in todo:
        src = f"{BTHPORT_DEVICES}\\{x['remote']}\\ServicesFor{x['record_addr']}"
        dst = f"{BTHPORT_DEVICES}\\{x['remote']}\\ServicesFor{live}"
        vals = _values_typed(src)
        try:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, dst, 0,
                                    winreg.KEY_SET_VALUE) as k:
                for n, v, t in vals:
                    winreg.SetValueEx(k, n, 0, t, v)
            print(f"   ✍  复制 ServicesFor{pretty(x['record_addr'])} → "
                  f"ServicesFor{pretty(live)}（{len(vals)} 个值）")
        except Exception as e:                  # noqa: BLE001
            return False, f"复制配对记录失败：{e.__class__.__name__} {e}"

        # 关联节点里的 Bluetooth_UniqueID 是"设备 ID 那个本地地址"的来源，
        # 不改它的话设备 ID 还是指向旧地址，照样打不开。
        for n in d["nodes"]:
            if n["remote"] != x["remote"] or not n["unique_id"]:
                continue
            new_id = f"Dev_{live}_{x['remote']}"
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    n["key"] + r"\Device Parameters", 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "Bluetooth_UniqueID", 0, winreg.REG_SZ, new_id)
                print(f"   ✍  {n['key']}  Bluetooth_UniqueID → {new_id}")
            except Exception as e:              # noqa: BLE001
                print(f"   ⚠ 改 Bluetooth_UniqueID 失败（不致命）：{e.__class__.__name__}")

        # 链接密钥可能是**按本地地址**存的（Keys\<本地地址>\<远端地址>）。
        # 只搬 ServicesFor 不搬密钥的话，记录看着搬过去了、握手照样失败 ——
        # 这种"半搬"最难查，所以两条一起搬。
        ksrc = f"{BTHPORT_KEYS}\\{x['record_addr']}"
        if _subkeys(ksrc):
            n_copied = _copy_key_tree(ksrc, f"{BTHPORT_KEYS}\\{live}")
            print(f"   ✍  搬运链接密钥 Keys\\{pretty(x['record_addr'])} → "
                  f"Keys\\{pretty(live)}（{n_copied} 个键）")
        else:
            print(f"   （Keys\\{pretty(x['record_addr'])} 不存在或读不到 —— "
                  "密钥若不是按本地地址存的，这一条本来就不需要）")

    ring = [a for a in d["adapters"] if a["present"]]
    restart_radio(ring[0]["instance_id"] if ring else "")
    return True, f"配对记录已搬到 {pretty(live)} 下"


# ── 修法 3：重建关联节点（保留配对记录，不用重新配对）────────────────────────
def fix_rebuild(d: dict, dry: bool = False, wait: float = 60.0,
                own: bool = False) -> tuple[bool, str]:
    """删掉**关联节点**（`Enum\\BTHLE\\Dev_<远端>`），配对记录留着。

    为什么"改 Bluetooth_UniqueID"不够（2026-09-15 实测）：
    两个节点的 `Bluetooth_UniqueID` 都改成当前地址了，诊断也判成 OK，
    但 Windows 枚举出来的设备 ID 里**还是旧地址** ——

        id = BluetoothLE#BluetoothLE04:7f:0e:90:11:01-f1:96:a2:63:67:1c
        is_paired=False   from_id_async → E_INVALIDARG

    因为那个 ID 是从节点的 **HardwareID / 设备接口**来的，不只是那个字符串值。
    而这些节点当初是**挂在已经不存在的那颗适配器实例下**创建的（父实例没了，
    子节点成了幽灵），所以只有删掉、让 Windows 按**当前**适配器重建一条路。

    配对记录不删 —— 这是它和 purge 的关键区别：删完重建能成功就**不用重新配对**。
    """
    import winreg
    if not d["nodes"]:
        return False, "没有关联节点可重建"
    remote = d["nodes"][0]["remote"]

    # 先保证记录里有当前地址那一份，否则重建出来仍然是"未配对"
    if d["live_addr"] and any(d["live_addr"] not in x["bound"] for x in d["targets"]):
        ok, why = fix_migrate(d, dry=dry)
        print(f"   （先补上 ServicesFor<{pretty(d['live_addr'])}>：{why}）")
        if not ok and not dry:
            return False, f"补配对记录失败：{why}"

    if dry:
        return True, (f"[试运行] 会删除 {len({n['devkey'] for n in d['nodes']})} 个关联节点树，"
                      "等 Windows 按当前适配器重建")

    # 先探能不能删干净：宁可不删，也不要"半删"
    # （半删的症状最恶心：Device Parameters 掉了、主键还在、调用方还当成功）
    devkeys = sorted({n["devkey"] for n in d["nodes"]})
    blocked: list[str] = []
    judged = True
    for dk in devkeys:
        b, v = _undeletable_in_tree(f"{ENUM_BTHLE}\\{dk}")
        blocked += b
        judged = judged and v
    if blocked and own and judged:
        # 先接管所有权（ACL 只放 SYSTEM 时这是唯一出路），接管完**重探**确认 ——
        # 拿不到"现在真能删"的确认就**不删**，绝不半删。
        print("   🔑 接管所有权（一层层往下吃，直到整棵树可删）…")
        blocked, judged = [], True
        for dk in devkeys:
            ok_t, why_t = take_ownership_tree(f"{ENUM_BTHLE}\\{dk}")
            print(f"      {'✅' if ok_t else '❌'} {dk}：{why_t}")
            b, v = _undeletable_in_tree(f"{ENUM_BTHLE}\\{dk}")
            blocked += b
            judged = judged and v
    if not judged:
        # 判不了就不动手 —— 这也是一种"宁可不删"。半删比不删更难收拾。
        return False, "关联节点删不删得动，这次判断不了。\n   " + _need_admin_hint()
    if blocked:
        return False, "关联节点删不动，无法重建。\n   " + _acl_hint(blocked)

    for devkey in devkeys:
        failed: list[str] = []
        n = _del_tree(winreg.HKEY_LOCAL_MACHINE, f"{ENUM_BTHLE}\\{devkey}", failed)
        if failed:
            return False, ("关联节点只删掉一部分（剩下的删不动），已停止。\n   "
                           + _acl_hint(failed))
        print(f"   🗑  已删除关联节点 {devkey}（{n} 个键）")

    ring = [a for a in d["adapters"] if a["present"]]
    restart_radio(ring[0]["instance_id"] if ring else "")

    print(f"   ⏳ 现在**按一下遥控器上任意一个键**把它唤醒（最多等 {int(wait)} 秒）…")
    t0 = time.time()
    while time.time() - t0 < wait:
        # 判据是"重建出来一个**不是幽灵**的节点"，而不是"有节点存在"。
        # 踩过的坑：删除其实没成功，read_aep_nodes() 又把同一批旧节点读回来，
        # 于是打印了一个**假成功** —— 一个永远说 OK 的闸比没有闸更危险。
        fresh = [n for n in read_aep_nodes()
                 if n["remote"] == remote and n.get("present")]
        if fresh:
            print(f"   ✅ 关联节点已按当前适配器重建（{len(fresh)} 个，且不是幽灵）")
            return True, "关联节点已重建（配对记录保留，不用重新配对）"
        time.sleep(2)
    return False, ("等待超时，关联节点还没重建 —— 多半是遥控器没醒。"
                   "按一下遥控器任意键，再跑一次本工具即可")


# ── 修法 4：清掉记录与僵尸节点（要重新配对）──────────────────────────────────
def fix_purge(d: dict, dry: bool = False, own: bool = False) -> tuple[bool, str]:
    import winreg
    if dry:
        return True, (f"[试运行] 会删除 {len(d['targets'])} 个配对记录、"
                      f"{len({n['devkey'] for n in d['nodes']})} 个关联节点")
    # 关联节点删不动就别硬来 —— 半删反而更难收拾（详见 _undeletable_in_tree）
    blocked: list[str] = []
    judged = True
    for dk in sorted({n["devkey"] for n in d["nodes"]}):
        b, v = _undeletable_in_tree(f"{ENUM_BTHLE}\\{dk}")
        blocked += b
        judged = judged and v
    if blocked and own and judged:
        print("   🔑 接管所有权（一层层往下吃，直到整棵树可删）…")
        blocked, judged = [], True
        for dk in sorted({n["devkey"] for n in d["nodes"]}):
            ok_t, why_t = take_ownership_tree(f"{ENUM_BTHLE}\\{dk}")
            print(f"      {'✅' if ok_t else '❌'} {dk}：{why_t}")
            b, v = _undeletable_in_tree(f"{ENUM_BTHLE}\\{dk}")
            blocked += b
            judged = judged and v
    acl_note = ""
    if not judged:
        # 判断不了就**别拿半删去赌**：这次只清记录（记录那棵键删得动），
        # 节点残留留着 —— 重新配对时 Windows 会覆盖掉，一般不影响。
        acl_note = ("\n   ⚠ 不是管理员，判断不了关联节点删不删得动 —— "
                    "这次**只清配对记录**，节点残留留着。")
    elif blocked:
        acl_note = ("\n   ⚠ 关联节点删不动（ACL 只放 SYSTEM），这次**只清配对记录**：\n"
                    + "".join(f"     · {p}\n" for p in blocked)
                    + "   那部分残留在重新配对时会被 Windows 覆盖掉，一般不影响。")

    for x in d["targets"]:
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE,
                             f"{BTHPORT_DEVICES}\\{x['remote']}")
            print(f"   🗑  已删除配对记录 {pretty(x['remote'])}")
        except Exception as e:                  # noqa: BLE001
            return False, (f"删配对记录失败：{e.__class__.__name__} {e}"
                           "（可能需要管理员 / 蓝牙服务正在占用）")
    if not blocked:
        for devkey in {n["devkey"] for n in d["nodes"]}:
            _del_tree(winreg.HKEY_LOCAL_MACHINE, f"{ENUM_BTHLE}\\{devkey}")

    ring = [a for a in d["adapters"] if a["present"]]
    restart_radio(ring[0]["instance_id"] if ring else "")
    return True, ("记录已清掉 —— 现在可以重新配对了"
                  "（遥控器长按 Home + 返回 约 3 秒进配对模式）" + acl_note)


def _copy_key_tree(src: str, dst: str) -> int:
    """递归复制注册表键（连同值、连同值的原始类型）。返回复制的键数。

    `Keys` 这个键的 ACL 只放 SYSTEM，非管理员连读都不行 —— 所以本函数
    只在提权之后才有意义（这也正是整个 --fix 必须先过一次 UAC 的原因）。
    """
    import winreg
    n = 0
    vals = _values_typed(src)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, dst, 0,
                                winreg.KEY_SET_VALUE) as k:
            for name, v, t in vals:
                winreg.SetValueEx(k, name, 0, t, v)
        n += 1
    except Exception as e:                      # noqa: BLE001
        print(f"   ⚠ 建 {dst} 失败：{e.__class__.__name__}")
        return n
    for sub in _subkeys(src):
        n += _copy_key_tree(f"{src}\\{sub}", f"{dst}\\{sub}")
    return n


def _del_tree(hive, path: str, failed: list[str] | None = None) -> int:
    """递归删注册表键（winreg 只删空键）。返回删掉的键数。

    ⚠ 删不掉时**必须记账**，不能只 print 一句就算了 —— 2026-09-15 实测：
    `Enum\\BTHLE\\Dev_<远端>\\…\\Properties` 的 ACL 只放 SYSTEM，管理员也删不动；
    原来的写法把失败吞掉，于是"删节点"变成**半删**（Device Parameters 掉了、
    主键还在），而调用方还当成功。这种"半删"比不删更难查。
    """
    import winreg
    n = 0
    for sub in _subkeys(path, hive):
        n += _del_tree(hive, f"{path}\\{sub}", failed)
    try:
        winreg.DeleteKey(hive, path)
        n += 1
    except Exception as e:                      # noqa: BLE001
        msg = f"{path}（{e.__class__.__name__}）"
        print(f"   ⚠ 删不掉 {msg}")
        if failed is not None:
            failed.append(msg)
    return n


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", ctypes.c_ulong)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.c_ulong),
                ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


# 接管所有权要同时打开这一组：少了谁都谈不成。
#  · SeTakeOwnershipPrivilege —— 允许把所有者改成自己人（ACL 不给也不管）
#  · SeRestorePrivilege      —— **越权打开对象**（WRITE_OWNER|WRITE_DAC），
#                               这正是"ACL 把你锁在门外"时的正主
#  · SeBackupPrivilege       —— 越权读取（要读 SD / 列内容时用得上）
# 实测教训：只开第一个，RegOpenKeyExW 照样 rc=5 —— 看起来像"权限不够"，
# 其实是"开的钥匙不对"。
TAKE_OWNERSHIP_PRIVS = ("SeTakeOwnershipPrivilege", "SeRestorePrivilege",
                        "SeBackupPrivilege")


def enable_privileges(names=TAKE_OWNERSHIP_PRIVS) -> tuple[bool, str]:
    """把一组特权全打开。返回 (是否全部打开, 说明)。"""
    notes = []
    for nm in names:
        ok, why = _enable_privilege(nm)
        if not ok:
            return False, f"{nm} → {why}"
        notes.append(nm)
    return True, "已启用 " + "、".join(notes)


def _enable_privilege(name: str) -> tuple[bool, str]:
    """启用本进程令牌里的某个特权。返回 (成功, 说明)。

    ⚠️ 两个坑，都踩过（2026-09-15）：
    ① 管理员令牌里 `SeTakeOwnershipPrivilege` 是**存在但 DISABLED** 的。
       不显式启用，`SetSecurityInfo` 设 owner 照样报 winerror=5 ——
       看起来像"权限不够"，其实是"特权没开"。
    ② **必须声明 restype/argtypes**。`GetCurrentProcess()` 返回伪句柄 `-1`
       （x64 上是 0xFFFFFFFFFFFFFFFF）；不声明 restype 的话 ctypes 按 c_int
       截成 0x00000000FFFFFFFF，`OpenProcessToken` 立刻以"句柄无效"失败。
       实测症状：接管那一步只报"启用特权失败"，完全看不出根因。
    """
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                     ctypes.POINTER(ctypes.c_void_p)]
    adv.OpenProcessToken.restype = ctypes.c_int
    adv.LookupPrivilegeValueW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                          ctypes.POINTER(_LUID)]
    adv.LookupPrivilegeValueW.restype = ctypes.c_int
    adv.AdjustTokenPrivileges.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.POINTER(_TOKEN_PRIVILEGES),
                                          ctypes.c_ulong, ctypes.c_void_p,
                                          ctypes.c_void_p]
    adv.AdjustTokenPrivileges.restype = ctypes.c_int

    TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY = 0x0020, 0x0008
    SE_PRIVILEGE_ENABLED = 0x00000002
    h = ctypes.c_void_p()
    ctypes.set_last_error(0)
    if not adv.OpenProcessToken(k32.GetCurrentProcess(),
                                TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                ctypes.byref(h)):
        return False, f"OpenProcessToken winerror={ctypes.get_last_error()}"
    try:
        luid = _LUID()
        if not adv.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False, f"LookupPrivilegeValue({name}) winerror={ctypes.get_last_error()}"
        tp = _TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        if not adv.AdjustTokenPrivileges(h, False, ctypes.byref(tp), 0, None, None):
            return False, f"AdjustTokenPrivileges winerror={ctypes.get_last_error()}"
        # AdjustTokenPrivileges 即使"没全给到"也返回真，得再看 last_error。
        # ERROR_NOT_ALL_ASSIGNED = 1300
        err = ctypes.get_last_error()
        if err:
            return False, f"特权没给到本令牌（winerror={err}）"
        return True, "已启用"
    finally:
        k32.CloseHandle(h)


def _admin_sid():
    """BUILTIN\\Administrators（S-1-5-32-544）的 SID 缓冲区。"""
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    size = ctypes.c_ulong(68)               # SECURITY_MAX_SID_SIZE
    buf = ctypes.create_string_buffer(size.value)
    if not adv.CreateWellKnownSid(WIN_BUILTIN_ADMINISTRATORS_SID, None,
                                  buf, ctypes.byref(size)):
        return None
    return buf


def _keypath(display: str) -> str:
    """把"路径  ← 原因"拆回纯净的注册表路径。

    `_undeletable_in_tree` 为了报告好看，会把失败原因缀在路径后面；
    但真去操作（接管 / 删除）时必须用干净的路径。
    """
    return display.split("  ← ")[0].strip()


_ACE_TYPE_NAME = {0: "允许", 1: "拒绝", 2: "审核"}
_ACE_FLAG_NAME = {0x1: "OI", 0x2: "CI", 0x4: "NP", 0x8: "IO", 0x10: "I"}


def dump_acl(reg_path: str) -> list[str]:
    """把一个注册表键的**所有者 + 每条 ACE** 打成人话。

    诊断用：`RegOpenKeyExW(WRITE_OWNER)` 报 rc=5 时，光看错误码根本不知道
    是谁挡的 —— ACL 里只要有一条 DENY（哪怕你已经是管理员），拿所有权也会被拒。
    不看 ACE 就是瞎猜。
    """
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    out: list[str] = []

    # 各种权限位逐个试一遍 —— "哪个权限打开得了"本身就是一条重要线索。
    masks = [("QUERY_VALUE", 0x0001), ("SET_VALUE", 0x0002),
             ("CREATE_SUB_KEY", 0x0004), ("ENUM_SUB_KEYS", 0x0008),
             ("READ_CONTROL", READ_CONTROL), ("WRITE_DAC", WRITE_DAC),
             ("WRITE_OWNER", WRITE_OWNER), ("KEY_READ", 0x20019),
             ("KEY_ALL_ACCESS", 0xF003F)]
    for label, m in masks:
        h = ctypes.c_void_p()
        rc = adv.RegOpenKeyExW(ctypes.c_void_p(0x80000002),
                               ctypes.c_wchar_p(reg_path), 0, m, ctypes.byref(h))
        out.append(f"    {label:<16} rc={rc}" + ("  ✅" if rc == 0 else ""))
        if rc == 0:
            adv.RegCloseKey(h)

    # 拿到 SD 才能看 ACE。READ_CONTROL 不行就只能到此为止。
    h = ctypes.c_void_p()
    rc = adv.RegOpenKeyExW(ctypes.c_void_p(0x80000002),
                           ctypes.c_wchar_p(reg_path), 0,
                           READ_CONTROL, ctypes.byref(h))
    if rc != 0:
        out.append(f"  （连 READ_CONTROL 都打不开 rc={rc} → 看不到 ACE）")
        return out
    try:
        p_sd = ctypes.c_void_p()
        p_owner = ctypes.c_void_p()
        p_dacl = ctypes.c_void_p()
        r = adv.GetSecurityInfo(h, SE_REGISTRY_KEY,
                                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                                ctypes.byref(p_owner), None, ctypes.byref(p_dacl),
                                None, ctypes.byref(p_sd))
        if r != 0:
            out.append(f"  GetSecurityInfo rc={r}")
            return out
        try:
            out.append(f"  所有者：{_sid_str(p_owner)}")
            if not p_dacl:
                out.append("  DACL 为空（NULL = 全体放行）")
            else:
                acl_bytes = ctypes.string_at(p_dacl, 8)
                acl_size = int.from_bytes(acl_bytes[2:4], "little")
                ace_count = int.from_bytes(acl_bytes[4:6], "little")
                buf = ctypes.string_at(p_dacl, acl_size)
                out.append(f"  DACL：{ace_count} 条 ACE，{acl_size} 字节")
                off = 8
                for _ in range(ace_count):
                    if off + 8 > len(buf):
                        out.append("    （ACE 解析越界，停止）")
                        break
                    atype, aflags = buf[off], buf[off + 1]
                    asize = int.from_bytes(buf[off + 2:off + 4], "little")
                    mask = int.from_bytes(buf[off + 4:off + 8], "little")
                    if asize < 8:
                        break
                    # SID 就在 ACE 里 mask 之后那几个字节，直接指过去读。
                    sid_ptr = ctypes.c_char_p(buf[off + 8:off + asize])
                    flags = "|".join(v for k, v in _ACE_FLAG_NAME.items() if aflags & k)
                    out.append(f"    [{off:>3}] {_ACE_TYPE_NAME.get(atype, atype)}"
                               f" mask=0x{mask:08X} flags={flags or '-'}"
                               f" sid={_sid_str(sid_ptr)}")
                    off += asize
        finally:
            k32.LocalFree(ctypes.cast(p_sd, ctypes.c_void_p))
    finally:
        adv.RegCloseKey(h)
    return out


def _sid_str(p_sid) -> str:
    """SID → "S-1-5-32-544" 这种可读串。"""
    if not p_sid:
        return "(无)"
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ps = ctypes.c_wchar_p()
    if not adv.ConvertSidToStringSidW(ctypes.cast(p_sid, ctypes.c_void_p),
                                      ctypes.byref(ps)):
        return f"(转换失败 winerror={ctypes.get_last_error()})"
    try:
        return ps.value or "(空)"
    finally:
        k32.LocalFree(ctypes.cast(ps, ctypes.c_void_p))


def take_ownership(reg_path: str) -> tuple[bool, str]:
    """把注册表键的所有权拿过来，并授予 Administrators 完全控制（含继承）。

    这是「设置里删不动、工具也删不动」的**唯一正规出路**：不是绕过 ACL，
    而是先成为所有者（Windows 明确定义的机制，管理员持 SeTakeOwnershipPrivilege），
    再合法地改 DACL，再删。子键靠 `OI|CI` 继承，所以只处理顶层键即可。

    ⚠️ 这会**改变系统键的 ACL**（所有者从 SYSTEM 变成 Administrators）。
    所以它是显式开关（`--take-ownership`），不进默认阶梯 —— 默认阶梯宁可
    拒绝动手、让你插回原来那个 USB 口。
    """
    if not is_admin():
        return False, "需要管理员权限"
    ok_p, why_p = enable_privileges()
    if not ok_p:
        return False, f"启用越权特权失败：{why_p}"

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    sid = _admin_sid()
    if not sid:
        return False, "取 Administrators 的 SID 失败"

    def _open(access: int):
        h = ctypes.c_void_p()
        rc = adv.RegOpenKeyExW(ctypes.c_void_p(0x80000002),  # HKEY_LOCAL_MACHINE
                               ctypes.c_wchar_p(reg_path), 0, access,
                               ctypes.byref(h))
        return (h, rc)

    # ① 设所有者。有 SeRestorePrivilege/SeTakeOwnershipPrivilege 时，
    #    这些权限位即使 DACL 完全不给管理员，也能拿到手。
    #    先要全（OWNER+DACL+READ），拿不到再退一档 —— 有的机器只给一半。
    h, rc = _open(WRITE_OWNER | WRITE_DAC | READ_CONTROL)
    if rc != 0:
        h, rc = _open(WRITE_OWNER | READ_CONTROL)
    if rc != 0:
        h, rc = _open(WRITE_OWNER)
    if rc != 0:
        return False, ("打不开键（WRITE_OWNER|WRITE_DAC|READ_CONTROL 都试过）"
                       f"rc={rc} —— 特权没生效或还有 DENY ACE。"
                       "用 --acl-dump 看 ACE 明细")
    try:
        r = adv.SetSecurityInfo(h, SE_REGISTRY_KEY, OWNER_SECURITY_INFORMATION,
                                ctypes.cast(sid, ctypes.c_void_p), None, None, None)
        if r != 0:
            return False, f"设所有者失败 rc={r}"
    finally:
        adv.RegCloseKey(h)

    # ② 所有者天然拥有 WRITE_DAC，这一步才拿得到。
    h, rc = _open(WRITE_DAC)
    if rc != 0:
        return False, f"打开键（WRITE_DAC）失败 rc={rc}"
    try:
        acl = ctypes.create_string_buffer(256)
        if not adv.InitializeAcl(acl, 256, ACL_REVISION):
            return False, "InitializeAcl 失败"
        if not adv.AddAccessAllowedAceEx(acl, ACL_REVISION,
                                        OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE,
                                        KEY_ALL_ACCESS, sid):
            return False, "AddAccessAllowedAceEx 失败"
        r = adv.SetSecurityInfo(h, SE_REGISTRY_KEY, DACL_SECURITY_INFORMATION,
                                None, None, acl, None)
        if r != 0:
            return False, f"设 DACL 失败 rc={r}"
    finally:
        adv.RegCloseKey(h)
    return True, "所有权已接管，Administrators 拿到完全控制"


def take_ownership_tree(top: str, rounds: int = 6) -> tuple[bool, str]:
    """反复接管，直到这棵树**整棵**删得动（或连续没有进展）。

    为什么必须循环（2026-09-15 提权实测）：
      · 第 1 轮只解锁 `Properties` —— 探针随即改口报
        `Properties\{104ea319-…}`、`Properties\{3464f7a4-…}` 拒绝访问；
      · 那些 `{GUID}` 子键各有**独立、且拒绝继承**的 DACL，
        父键加继承 ACE 传不下去，所以必须**一层一层往下吃**。

    "一轮接管就完事"是个很自然但错的假设 —— 它在真实键树上只解第一层，
    然后报一个看起来很像结论的失败。
    """
    done: list[str] = []
    blocked: list[str] = []
    for i in range(rounds):
        blocked, judged = _undeletable_in_tree(top)
        if not judged:
            return False, "不是管理员（普通用户连通告权都没有）"
        if not blocked:
            return True, (f"整棵树都删得动了（{i} 轮接管，{len(done)} 个键）"
                          if i else "本来就能删")
        for b in blocked:
            p = _keypath(b)
            if p in done:                       # 这轮解不动了，别再空转
                continue
            ok, why = take_ownership(p)
            done.append(p)
            print(f"      {'✅' if ok else '❌'} {p}：{why}")
    blocked, judged = _undeletable_in_tree(top)
    if judged and not blocked:
        return True, f"整棵树都删得动了（{len(done)} 个键）"
    return False, (f"接管了 {len(done)} 个键，仍有 {len(blocked)} 个删不动"
                   + (f"：{_keypath(blocked[0])}" if blocked else ""))


def _undeletable_in_tree(path: str, limit: int = 4) -> tuple[list[str], bool]:
    """先探一下这棵注册表树**到底删不删得动**，免得删一半。

    判据：能不能用 **DELETE** 权限打开每个子键（删键要的就是这个权限，
    `KEY_SET_VALUE` 会漏判"能写、不能删"那种 ACL）。`Properties` 这类子键的
    ACL 只放 SYSTEM，管理员打开就 PermissionError —— 那它的父键永远删不干净，
    整个删除动作只会落个"半删"。

    ⚠️ 返回 `(删不动的键, 结论是否有效)`。**非管理员下结论一律无效**：
    普通用户连 `Enum\BTHLE` 整棵树都写不动，探出来必然"全都删不动"，
    而那是权限问题、不是 ACL 问题。2026-09-15 自测就栽在这上面 ——
    非管理员跑出来的报告把根键也算成「ACL 只放 SYSTEM，管理员也删不掉」，
    等于把用户直接劝退到"没救"。**结论错比不给结论更糟**，所以宁可不给。
    """
    import winreg
    if not is_admin():
        return [], False

    bad: list[str] = []

    def walk(p: str) -> None:
        if len(bad) >= limit:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, p, 0,
                                DELETE_ACCESS | winreg.KEY_QUERY_VALUE):
                pass
        except Exception as e:                  # noqa: BLE001
            # 把原因带上 —— 光说"删不动"没法分辨是 ACL 锁死、键被占用、
            # 还是路径写错了。winerror 5 = 拒绝访问。
            why = e.__class__.__name__
            if isinstance(e, PermissionError):
                why = f"拒绝访问（winerror={getattr(e, 'winerror', 5)}）"
            bad.append(f"{p}  ← {why}")
            return
        for sub in _subkeys(p):
            walk(f"{p}\\{sub}")

    walk(path)
    return bad, True


def _need_admin_hint() -> str:
    """判不了就得说清楚"为什么判不了"，别让用户以为是"没救"。"""
    return ("这一步要在**管理员**下才做得成 —— 关联节点整棵树普通用户都写不动。\n"
            "   本工具正常会自己弹 UAC 提权；如果你看到这句，说明提权没成功。\n"
            "   手动提权：右键「修复蓝牙配对.bat」→ 以管理员身份运行。")


def _acl_hint(paths: list[str]) -> str:
    """删不动的键该怎么处理 —— 这是「设置里也删不掉」的同一个根。

    ⚠️ 只该在 **管理员** 下判定过之后调用。非管理员跑出来的"删不动"是权限
    问题、不是 ACL 问题，拿它当 ACL 结论会把用户带偏（_undeletable_in_tree
    的 verdict 就是防这个）。
    """
    return (
        "这几个键的 ACL 只放 SYSTEM，**管理员也删不动**（已在管理员下确认）：\n"
        + "".join(f"     · {p}\n" for p in paths)
        + "   所以「删除设备」在 Windows 设置里也会卡死 —— 同一个原因。\n"
        "   能走的路：\n"
        "     ① 先试把蓝牙棒插回**原来那个 USB 口**（配对记录绑的地址就在那个口上，\n"
        "        插回去配对记录往往直接重新有效，不用重新配对、也不用删任何东西）；\n"
        "     ② 实在要清记录重新配对：安全模式 / PE 下删，或用 pnputil /remove-device；\n"
        "     ③ 记录本身（BTHPORT\\Parameters\\Devices）不在此列，它删得动。"
    )


# ── 验证 ─────────────────────────────────────────────────────────────────────
def verify_open(remote: str) -> tuple[bool, str]:
    """真的去把 BLE 设备打开一次 —— 这是唯一可信的验收标准。

    没有 winrt（比如精简环境）就跳过，绝不假装成功。
    """
    try:
        import asyncio
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
    except Exception:                           # noqa: BLE001
        return False, "SKIPPED（本环境没有 winrt，没法做真机验收）"

    async def _try():
        sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        infos = await DeviceInformation.find_all_async_aqs_filter(sel)
        hits = [i for i in infos if hex12(i.id.split("-")[-1]) == hex12(remote)]
        if not hits:
            return None, f"枚举不到该设备（共枚举到 {len(infos)} 个已配对 BLE 设备）"
        errs = []
        for i in hits:
            try:
                dev = await BluetoothLEDevice.from_id_async(i.id)
                if dev is not None:
                    return True, f"成功打开：{i.name}  id={i.id}"
            except OSError as e:
                errs.append(str(e))
        return False, f"枚举到 {len(hits)} 个候选，都打不开：{errs[:2]}"

    try:
        return asyncio.run(_try())
    except Exception as e:                      # noqa: BLE001
        return False, f"{e.__class__.__name__}: {e}"


# ── 回滚：把备份 .reg 导回去 ─────────────────────────────────────────────────
def restore_backup(which: str = "") -> tuple[bool, str]:
    """把 `backup\\` 里最近的备份 .reg 导回注册表。

    为什么必须有这个入口：修复工具动的是**系统**注册表。只会"导出备份"、
    没有"一键导回"，等于把风险留给用户自己承担 —— 那种工具不该发出去。
    默认只导 `bthle-*.reg`（关联节点那棵树，也是唯一会被删改的一棵）；
    想全导就传 `*`。
    """
    if not BACKUP_DIR.is_dir():
        return False, f"没有备份目录：{BACKUP_DIR}"
    pat = f"*{which or 'bthle'}*.reg"
    files = sorted(BACKUP_DIR.glob(pat), key=lambda p: p.stat().st_mtime)
    if not files:
        return False, f"备份目录里没有匹配 {pat} 的文件"
    f = files[-1]
    rc, out = _run(["reg", "import", str(f)], timeout=60)
    if rc != 0:
        return False, f"导入失败：{out.strip()[:200]}"
    print(f"   ↩  已导回 {f.name}")
    return True, f"已从备份还原：{f.name}"


# ── 提权 ─────────────────────────────────────────────────────────────────────
def elevate_and_wait(argv: list[str]) -> int:
    """用 UAC 重新拉起自己，等它跑完，把退出码原样返回。

    怎么拉起自己：源码环境是 python.exe + 本文件路径；打包后是本 exe
    （安装版的 RemoteVoiceBridgeDiag.exe 就靠这条路径实现「双击 .bat 就提权」）。
    """
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, " ".join(f'"{a}"' for a in argv)
    else:
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in [os.path.abspath(__file__)] + argv)

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.lpDirectory = os.path.dirname(exe)
    sei.nShow = 1

    print("需要管理员权限 —— 正在弹出 UAC 授权框，请点「是」。")
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        code = ctypes.get_last_error()
        print(f"❌ 提权被取消或失败（GetLastError={code}）—— "
              "可右键「修复蓝牙配对.bat」→ 以管理员身份运行。")
        return 1
    if not sei.hProcess:
        print("⚠ 拿不到子进程句柄，请看新开的那个窗口里的输出。")
        return 0

    # 等子进程跑完再返回，这样"谁调用我"只看到一个窗口的结果。
    # ⚠ 不用 INFINITE：UAC 框如果没人点，子进程根本不会启动，
    #   INFINITE 会一直挂着（自动化/托盘调用时表现就是"卡死"）。
    WAIT_MS = 240_000
    w = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, WAIT_MS)
    rc = ctypes.c_ulong(0)
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(rc))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    if w == 0x102:                              # WAIT_TIMEOUT
        print("⚠ 等提权窗口超时（可能那个 UAC 框一直没人点）。")
        return 1
    return int(rc.value)


# ── CLI ──────────────────────────────────────────────────────────────────────
def _pause() -> None:
    """安装版双击运行：窗口别一眨眼就关掉。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        input("\n按【回车】键关闭本窗口…")
    except (EOFError, KeyboardInterrupt):
        pass


def main(argv: list[str] | None = None) -> int:
    _setup_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)

    def has(flag: str) -> bool:
        return flag in argv

    def opt(flag: str, default: str = "") -> str:
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
        return default

    # 内部模式：给父进程读一个"新鲜的"地址用
    if has("--pairing-probe-addr"):
        print(f"ADDR={live_addr_winrt() or live_addr_registry()}")
        return 0

    # 只探不删：回答"关联节点到底删不删得动"。**必须管理员**才有意义
    # （非管理员探出来全是"删不动"，那是权限问题，不是 ACL 问题）。
    # 存在的意义：判断要不要动 ACL 接管这一步，先拿到权威答案再决定。
    if has("--acl-probe"):
        # 这里还没解析到 `dry`（它在下面才赋值），所以直接看开关本身。
        if not is_admin() and not has("--dry-run"):
            # 提权时把**所有**开关原样带过去 —— 只传 "--acl-probe" 的话
            # `--take-ownership` 会在提权那一刻丢掉，子进程静悄悄少做一步
            # （本项目的老家系：防御机制在某条路径上没落地，症状还长得一样）。
            child = [a for a in argv if a != "--dry-run"] + ["--elevated"]
            rc = elevate_and_wait(child)
            # 提权那次在**另一个控制台**里跑，这边看不到它的 stdout；
            # 所以子进程把报告落在 REPORT_TXT，这里捞回来再打一遍 ——
            # 发起方只看一个窗口也能拿到结论。
            try:
                print("\n" + REPORT_TXT.read_text(encoding="utf-8"))
            except Exception:                   # noqa: BLE001
                print("（读不到子进程的报告）")
            return rc

        L: list[str] = []
        A = L.append
        d = diagnose(verify=False)
        devkeys = sorted({n["devkey"] for n in d["nodes"]})
        A("=" * 72)
        A(f" 关联节点可删性探测  管理员={is_admin()}  节点树={len(devkeys)} 个"
          f"  {d['time']}")
        A("=" * 72)
        want_own = has("--take-ownership") or has("--own")

        def probe_all() -> tuple[dict, bool, bool]:
            res, ok_all, judged = {}, True, False
            for dk in devkeys:
                blocked, j = _undeletable_in_tree(f"{ENUM_BTHLE}\\{dk}")
                res[dk] = (blocked, j)
                if not j:
                    ok_all = False
                    A(f" ⏸  {dk}：非管理员，不给结论")
                    continue
                judged = True
                if blocked:
                    ok_all = False
                    A(f" ❌ {dk}：删不动")
                    for b in blocked:
                        A(f"      · {b}")
                else:
                    A(f" ✅ {dk}：整棵树删得动")
            return res, ok_all, judged

        res, all_ok, judged_any = probe_all()

        # 显式接管：只改 ACL、**不删**，然后把探测重跑一遍 ——
        # 这样"接管到底解不解得开锁"能在不动任何数据的前提下先验一次。
        if want_own:
            targets = [_keypath(b) for bl, _ in res.values() for b in bl]
            A("")
            if not targets:
                A("（没有删不动的键，接管用不上）")
            else:
                A(f"── 接管 ACL（{len(targets)} 个键起步，逐层往下吃，不删任何东西）──")
                for dk in devkeys:
                    ok, why = take_ownership_tree(f"{ENUM_BTHLE}\\{dk}")
                    A(f"  {'✅' if ok else '❌'} {dk}：{why}")
                A("")
                A("── 接管之后重新探测 ──")
                res, all_ok, judged_any = probe_all()
        A("")
        # "判不了"跟"删不动"必须是两个结论 —— 混在一起说等于把用户劝退。
        if not judged_any:
            A("结论：判断不了（本次不是管理员）。请以管理员身份再跑一次")
            A("      （工具会自己弹 UAC；或右键「修复蓝牙配对.bat」→ 以管理员身份运行）。")
        elif all_ok:
            A("结论：管理员下**能删干净** —— rebuild / purge 可以正常走。")
        else:
            A("结论：管理员下也删不动 → 需要先接管 ACL（拿所有权）才谈得上删。")
            A("     现在可以走的路：把蓝牙棒插回**原来那个 USB 口**")
            A("       （配对记录绑的地址就在那个口上，插回去往往直接恢复）。")
        A("（本模式**没有删除任何东西**。）")
        txt = "\n".join(L)
        print(txt)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            REPORT_TXT.write_text(txt, encoding="utf-8")
            print(f"\n报告：{REPORT_TXT}")
        except Exception as e:                  # noqa: BLE001
            print(f"（报告落盘失败：{e.__class__.__name__}）")
        return 0

    # 把删不动的键的 ACL **原样打出来**：rc=5 只知道"被拒"，不知道"被谁拒"。
    # 诊断"为什么删不动"只能看 ACE —— 尤其有没有 DENY。
    if has("--acl-dump"):
        if not is_admin() and not has("--dry-run"):
            child = [a for a in argv if a != "--dry-run"] + ["--elevated"]
            rc = elevate_and_wait(child)
            try:
                print("\n" + REPORT_TXT.read_text(encoding="utf-8"))
            except Exception:                   # noqa: BLE001
                print("（读不到子进程的报告）")
            return rc

        L: list[str] = []
        A = L.append
        d = diagnose(verify=False)
        devkeys = sorted({n["devkey"] for n in d["nodes"]})
        A("=" * 72)
        A(f" 删不动的键 ACL 明细  管理员={is_admin()}  {d['time']}")
        A("=" * 72)
        ok_pv, why_pv = enable_privileges()
        A(f" 越权特权：{'✅ ' + why_pv if ok_pv else '❌ ' + why_pv}")
        for dk in devkeys:
            top = f"{ENUM_BTHLE}\\{dk}"
            blocked, judged = _undeletable_in_tree(top)
            A("")
            A(f"■ 顶层键 {top}")
            for line in dump_acl(top):
                A(line)
            if not judged:
                A("  ⏸  不是管理员 → 不判可删性")
                continue
            for b in blocked:
                p = _keypath(b)
                A("")
                A(f"■ 删不动的键（{b[len(p):].strip() or '原因未知'}）")
                A(f"  {p}")
                for line in dump_acl(p):
                    A(line)
            if not blocked:
                A("  （整棵树都删得动）")
        txt = "\n".join(L)
        print(txt)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            REPORT_TXT.write_text(txt, encoding="utf-8")
            print(f"\n报告：{REPORT_TXT}")
        except Exception as e:                  # noqa: BLE001
            print(f"（报告落盘失败：{e.__class__.__name__}）")
        return 0

    if not hasattr(ctypes, "WinDLL"):
        print("SKIPPED（这不是 Windows）")
        return 0

    # `--fix-pairing` 是给安装版用的对外名字（「修复蓝牙配对.bat」和托盘菜单
    # 都叫这个）—— 语义与源码环境的 `--fix` 完全一样，两个都认。
    want_fix = has("--fix") or has("--fix-pairing") or has("--restore-backup")
    want_own = has("--take-ownership") or has("--own")
    method = opt("--method", "").lower()
    assume_yes = has("--yes")
    dry = has("--dry-run")

    # ── 回滚：把备份导回去（也要管理员，所以和修复走同一条提权路径）──
    if has("--restore-backup"):
        which = opt("--restore-backup", "")
        if which.startswith("--"):              # 后面跟的是别的开关，不是值
            which = ""
        if not is_admin() and not dry:
            return elevate_and_wait(["--restore-backup", which, "--elevated"])
        ok, msg = restore_backup(which)
        print(("✅ " if ok else "❌ ") + msg)
        d = diagnose()
        print(format_report(d))
        return 0 if ok else 1

    d = diagnose()
    text = format_report(d)
    print(text)
    try:
        REPORT_TXT.write_text(text, encoding="utf-8")
    except Exception:                           # noqa: BLE001
        pass

    if not want_fix:
        print("这是**只读诊断**。要修就加 --fix（会弹 UAC）。")
        print(f"报告：{REPORT_TXT}")
        return 0

    if d["status"] == "OK" and not method:
        print("✅ 配对这边没有问题，不用修。连不上的话请查别的方向"
              "（遥控器没醒 / 桥程序没退出等）。")
        return 0

    # 修复必须提权。`--dry-run` 例外：它什么都不改，也就不需要管理员 ——
    # 这样"方案会不会选对"能在不改任何东西、不弹 UAC 的前提下先验一遍。
    if not is_admin() and not dry:
        child = [a for a in argv if a != "--dry-run"] + ["--elevated"]
        rc = elevate_and_wait(child)
        # 子进程在**另一个控制台**里干活，这边把它的结论捞回来，
        # 这样调用方（托盘菜单 / .bat / 自动化）只看一个窗口也知道结果。
        try:
            info = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
            print()
            print("=" * 72)
            print(f" 子进程（管理员）结果：{'✅ 成功' if info.get('ok') else '❌ 未成功'}")
            print(f" 状态：{info.get('status', '')}  {info.get('why', '')}")
            print(f" 完整报告：{REPORT_TXT}")
            print("=" * 72)
        except Exception:                       # noqa: BLE001
            pass
        return rc

    print("=" * 72)
    print(" 修复演练（--dry-run：不改任何东西）" if dry else " 开始修复（管理员）")
    print("=" * 72)

    # 从这一行起，所有输出都同时进 pairing-fix.log —— 提权那一次的窗口
    # 关掉之后，留下的就是这份日志（支持/回顾只认它）。
    if not dry:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _logf = open(LOG_TXT, "a", encoding="utf-8")
            _logf.write("\n" + "=" * 72 + "\n"
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')}  "
                        f"开始修复（管理员={is_admin()}）\n")
            _logf.flush()
            _orig, sys.stdout = sys.stdout, _Tee(sys.stdout, _logf)
            import atexit
            atexit.register(lambda: (_logf.flush(), _logf.close()))
        except Exception as e:                  # noqa: BLE001
            print(f"（日志落盘失败，不影响修复：{e.__class__.__name__}）")

    if dry:
        print("\n① （演练）跳过注册表备份\n")
    else:
        made = backup(d)
        print(f"\n① 已备份 {len(made)} 个注册表键 → {BACKUP_DIR}")
        print("   出问题可以双击 .reg 文件还原。\n")

    # restore / migrate 能不能成，先看密钥还在不在
    k = d.get("keys") or {}
    if k.get("readable") and not k.get("locals"):
        print("⚠ 配对密钥材料已经不在了 —— restore / migrate 都不会成功，"
              "只能重新配对。直接走 purge。\n")
        method = "purge"

    order = [method] if method else ["migrate", "rebuild"]
    if not method:
        # 为什么默认不走 restore（"把适配器地址改回记录绑的那个"）：
        # 2026-09-15 真机实测**改不动** —— 写进 DeviceAddressCache、重启设备、
        # 重启蓝牙服务，读回来还是原值（驱动自己会把值写回去）。
        # 所以它只在 `--method restore` 时才会被用到，留给"有些机器能改"的情况。
        #
        # 默认这条路完全不碰地址：把配对记录补到当前地址下（migrate），
        # 再把挂在旧适配器实例下的幽灵关联节点删掉让它重建（rebuild）。
        # 两条都不需要重新配对。
        if not any(a["present"] for a in d["adapters"]):
            # 一个在场的蓝牙棒都没有 → 后面 rebuild 要等"节点按当前适配器重建"，
            # 没有适配器就永远等不到。先把话说在前面，别让用户干等 60 秒。
            print("⚠ 没有枚举到**在场**的蓝牙适配器 —— 遥控器不可能连上。"
                  "先确认蓝牙棒插好了、驱动正常，再来修。\n")

    ok = False
    why = ""
    for name in order:
        print(f"② 方案 {name} …")
        if name == "restore":
            good, why = fix_restore(d, target=opt("--addr", ""), dry=dry)
        elif name == "migrate":
            good, why = fix_migrate(d, dry=dry)
        elif name == "rebuild":
            good, why = fix_rebuild(d, dry=dry, own=want_own)
        elif name == "purge":
            if not (assume_yes or dry):
                print("   purge 会清掉配对记录（要重新配对），需要 --yes 确认。跳过。")
                continue
            good, why = fix_purge(d, dry=dry, own=want_own)
        else:
            print(f"   不认识的方法 {name!r}")
            continue
        print(f"   {'✅' if good else '❌'} {why}")
        if good:
            ok = True
            break
        print()

    if not ok:
        print("\n❌ 自动修复没能解决。")
        if not method:
            print("   兜底方案（会清掉记录、需要重新配对遥控器）：")
            print("     python pairing.py --fix --method purge --yes")
            print("   如果上面的原因是「删除设备」卡死（ACL 只放 SYSTEM）：")
            print("     python pairing.py --fix --method purge --yes --take-ownership")
        _write_json(d, ok=False, why=why)
        return 1

    if dry:
        print("\n（试运行模式，什么都没改。）")
        return 0

    # ── 真机验收：把设备真的打开一次 ──
    print("\n③ 验收：重新枚举已配对设备，试着打开它…")
    time.sleep(3)
    remote = d["targets"][0]["remote"] if d["targets"] else ""
    if not remote and d["nodes"]:
        remote = d["nodes"][0]["remote"]
    good, msg = verify_open(remote) if remote else (False, "没有目标设备")
    print(f"   {'✅' if good else '⚠'} {msg}")

    d2 = diagnose()
    print(f"\n④ 复检：{STATUS_TEXT.get(d2['status'], d2['status'])}")
    if d2["status"] == "STALE_ADDR":
        print("   还是 STALE_ADDR —— 说明地址又漂回去了，改用 migrate：")
        print("     python pairing.py --fix --method migrate")

    _write_json(d2, ok=good, why=msg, applied=order[:1])
    print(f"\n报告：{REPORT_TXT}")
    if not dry:
        print(f"过程日志：{LOG_TXT}   （提权窗口关掉之后，留下的就是这份）")
        print(f"改动前备份：{BACKUP_DIR}")
    if good:
        print("接下来：让桥程序（或托盘图标）重连；遥控器按任意键唤醒。")
    else:
        print("若仍然连不上：把上面这份报告发出来，或改用 purge 兜底（需重新配对）。")
    return 0 if good else 2


def _write_json(d: dict, ok: bool, why: str, applied: list[str] | None = None) -> None:
    try:
        payload = dict(d)
        payload.update({"ok": ok, "why": why, "applied": applied or []})
        REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                          default=str), encoding="utf-8")
    except Exception:                           # noqa: BLE001
        pass


if __name__ == "__main__":
    _rc = main()
    # 即使是被父进程用 `--elevated` 拉起来的那一次也要停 —— 那是**唯一**能看到
    # 完整修复过程（备份了哪些键、动了什么、验收结果）的窗口，
    # 一闪而过就等于没有。`_pause()` 本身在源码环境里是空操作。
    _pause()
    raise SystemExit(_rc)
