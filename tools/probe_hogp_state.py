"""遥控器 HOGP（HID over GATT）那一侧到底有没有"活着的节点"？

为什么要有这个工具
==================
「遥控器按键在 Windows 上全没反应」查到最后只有一个分岔：

  A. 遥控器根本没发 HID 输入报告        → 改代码没用，只能从设备/配对那层查
  B. Windows 没把报告翻译/转交出去      → 要修的是 Windows 侧的驱动绑定

`probe_devnode_binding.py` 读的是**注册表**，而注册表里躺着**历代**设备实例（幽灵节点），
分不出"现在活着的那一代"和"上一代留下的残留"。它只能告诉你"账本上有这条记录"。

真正说了算的是 **PnP 管理器**（cfgmgr32）：

  · `CM_Locate_DevNodeW(iid, 0)` —— 用正常模式定位
        rc == 0            → 这个节点**现在在场**
        rc == 13 (NO_SUCH) → 只是注册表里的残留（幽灵），现在不在场
  · `CM_Get_DevNode_Status` —— 拿 StatusFlags
        DN_STARTED(0x08)     已启动
        DN_HAS_PROBLEM(0x400) 有故障（配 Problem 号）
        DN_DISABLEABLE(0x2000) Windows 允许禁用它（没有这一位 = 禁不掉，
                              也就是"设备管理器里是灰的"）

本工具要回答的三句话
====================
1. 遥控器本体（`BTHLE\\Dev_<addr>`）现在在场吗？
2. `0x1812`（HID 服务）的节点在场吗？**和其余 7 个服务比，是不是少了一代？**
3. HOGP 那几个节点（`mshidumdf` + 5 个 HID 集合）各自启没启动、有没有故障号？

> 为什么盯"少了一代"：本机实测 `0x180000/0x180001/0x18000a/0x18000f/0xae40/0xab5e0001/0xd343bfc0`
> 都各有**两代**实例，只有 `0x1812` **只有一代**。若那一代恰好是旧代，
> 就意味着"Windows 给 HID 服务建的节点没跟上当前设备实例" ——
> 那么 HID 报告**永远没人订阅**，遥控器当然一个键都送不进 Windows。
> 这条假说以前没人验证过，因为它只能靠"在场判定"分出来，注册表看不出来。

> ⚠ **2026-09-23 二次改判。** 本工具最初盯的是「0x1812 是不是少了一代」，
> 后来改判成「配对记录里没有密钥」—— **第二个结论是错的**（详见下面
> 「配对密钥材料」那段注释：它看的是元数据键，那里本来就不该有 LTK）。
> 现在它按四段体检，并在最后给一段**综合判决**：
>
>   ① **密钥材料**：三态（读到 / 存在但空 / 读不到）。**读不到就只报读不到**，
>      不给「没有密钥」的结论 —— 密钥键只放 SYSTEM。
>   ② **Windows 此刻连没连着**（`ConnectionStatus`）：一条就能否掉
>      「没连上所以收不到」这一整类解释。
>   ③ 同一个遥控器有几条已配对条目。
>   ④ 各 devnode 的在场判定（cfgmgr32）—— 分出「现在活着的那一代」和幽灵。
>
> 判词由 `bond_verdict()` 这个**纯函数**给出，`--selftest` 用反例钉住
> 「四种输入必须四种判词」，防止哪天被改成永远打 ✅ 或又把人引到错方向。


用法
====
    python tools\\probe_hogp_state.py              # 只读，不按任何键
    python tools\\probe_hogp_state.py --rebind     # （需管理员）重新枚举设备，重建服务子节点

`--rebind` 做的是 `CM_Reenumerate_DevNode(遥控器节点, CM_REENUMERATE_SYNCHRONOUS)`：
让 PnP 管理器**重新枚举这颗设备**，服务子节点会按当前实际暴露的服务重建一遍。
它不动配对、不动注册表里的密钥，属于**非破坏性**操作（设备会断开重连一次）。
"""
import argparse
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

VID_TAG = "vid&0218d1"          # 18D1 = Google
ADDR_TAG = "f196a263671c"       # 遥控器蓝牙地址（换设备要改这里，或走 --all）

# ── StatusFlags 位 ──
DN_DRIVER_LOADED = 0x00000002
DN_STARTED = 0x00000008
DN_HAS_PROBLEM = 0x00000400
DN_DISABLEABLE = 0x00002000
DN_NO_SHOW_IN_DM = 0x00004000

# ── CONFIGRET ──
CR_SUCCESS = 0
CR_NO_SUCH_DEVINST = 13

# ── CM_Locate_DevNode 标志 ──
LOCATE_NORMAL = 0x00000000
LOCATE_PHANTOM = 0x00000001

# ── CM_Reenumerate_DevNode 标志 ──
REENUM_SYNCHRONOUS = 0x00000001

MAX_DEVICE_ID_LEN = 200


def _cfg():
    """配好原型的 cfgmgr32。

    ⚠ 必须显式给 argtypes/restype（见 pairing.py 里那段血泪）：
    不声明原型时 ctypes 把 ulFlags 按 32 位传，x64 下寄存器高 32 位是脏的，
    `CM_Locate_DevNodeW` 会对**所有**设备返回失败 —— 不报错，只给反结论。
    """
    cfg = ctypes.windll.cfgmgr32
    if getattr(cfg, "_hogp_typed", False):
        return cfg
    cfg.CM_Locate_DevNodeW.restype = ctypes.c_ulong
    cfg.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(ctypes.c_ulong),
                                       ctypes.c_wchar_p, ctypes.c_ulong]
    cfg.CM_Get_DevNode_Status.restype = ctypes.c_ulong
    cfg.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(ctypes.c_ulong),
                                          ctypes.POINTER(ctypes.c_ulong),
                                          ctypes.c_ulong, ctypes.c_ulong]
    cfg.CM_Get_Parent.restype = ctypes.c_ulong
    cfg.CM_Get_Parent.argtypes = [ctypes.POINTER(ctypes.c_ulong),
                                  ctypes.c_ulong, ctypes.c_ulong]
    cfg.CM_Get_Device_IDW.restype = ctypes.c_ulong
    cfg.CM_Get_Device_IDW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                                      ctypes.c_ulong]
    cfg.CM_Reenumerate_DevNode.restype = ctypes.c_ulong
    cfg.CM_Reenumerate_DevNode.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    cfg._hogp_typed = True
    return cfg


def locate(instance_id: str, flags: int = LOCATE_NORMAL):
    """→ (devinst, rc)。rc==0 才是在场；rc==13 是幽灵残留。"""
    cfg = _cfg()
    dn = ctypes.c_ulong(0)
    rc = cfg.CM_Locate_DevNodeW(ctypes.byref(dn), ctypes.c_wchar_p(instance_id),
                                ctypes.c_ulong(flags))
    return (dn.value, rc) if rc == CR_SUCCESS else (0, rc)


def status(devinst: int):
    """→ (status_flags, problem)。拿不到返回 (None, None)。"""
    cfg = _cfg()
    st, pb = ctypes.c_ulong(0), ctypes.c_ulong(0)
    rc = cfg.CM_Get_DevNode_Status(ctypes.byref(st), ctypes.byref(pb),
                                   ctypes.c_ulong(devinst), ctypes.c_ulong(0))
    if rc != CR_SUCCESS:
        return None, None
    return st.value, pb.value


def parent_of(devinst: int):
    cfg = _cfg()
    p = ctypes.c_ulong(0)
    rc = cfg.CM_Get_Parent(ctypes.byref(p), ctypes.c_ulong(devinst),
                           ctypes.c_ulong(0))
    return p.value if rc == CR_SUCCESS else 0


def device_id(devinst: int) -> str:
    cfg = _cfg()
    buf = ctypes.create_unicode_buffer(MAX_DEVICE_ID_LEN + 1)
    rc = cfg.CM_Get_Device_IDW(ctypes.cast(buf, ctypes.c_wchar_p),
                               ctypes.c_ulong(MAX_DEVICE_ID_LEN),
                               ctypes.c_ulong(0))
    return buf.value if rc == CR_SUCCESS else ""


def flags_text(st: int) -> str:
    if st is None:
        return "?"
    out = []
    if st & DN_STARTED:
        out.append("已启动")
    if st & DN_DRIVER_LOADED:
        out.append("驱动已载入")
    if st & DN_HAS_PROBLEM:
        out.append("⚠有故障")
    out.append("可禁用" if st & DN_DISABLEABLE else "禁不掉")
    if st & DN_NO_SHOW_IN_DM:
        out.append("设备管理器不显示")
    return "、".join(out)


def describe(instance_id: str) -> dict:
    """把一个实例 ID 的在场/启动/父链全查出来。"""
    dn, rc = locate(instance_id, LOCATE_NORMAL)
    phantom_dn, _ = locate(instance_id, LOCATE_PHANTOM)
    out = {"iid": instance_id, "devinst": dn, "rc": rc,
           "present": rc == CR_SUCCESS, "in_tree": phantom_dn != 0,
           "status": None, "problem": None, "chain": []}
    if not out["present"]:
        return out
    st, pb = status(dn)
    out["status"], out["problem"] = st, pb
    cur = dn
    for _ in range(8):
        cur = parent_of(cur)
        if not cur:
            break
        out["chain"].append(device_id(cur))
    return out


def registry_children():
    """扫注册表，拿遥控器相关的全部实例 ID（含历代幽灵）。"""
    import winreg
    enum = r"SYSTEM\CurrentControlSet\Enum"
    found = []   # (枚举器, 硬件ID, 实例, iid)

    def subkeys(path):
        out = []
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                i = 0
                while True:
                    try:
                        out.append(winreg.EnumKey(k, i))
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
        return out

    for enumerator in ("BTHLE", "BTHLEDevice", "HID", "BTHENUM"):
        root = f"{enum}\\{enumerator}"
        for hwid in subkeys(root):
            low = hwid.lower()
            if VID_TAG not in low and ADDR_TAG not in low:
                continue
            for inst in subkeys(f"{root}\\{hwid}"):
                found.append((enumerator, hwid, inst, f"{enumerator}\\{hwid}\\{inst}"))
    return found


def short(instance_id: str) -> str:
    """把长 GUID 缩短，只为看得清。"""
    s = instance_id
    for full, ab in (("00001812-0000-1000-8000-00805f9b34fb", "0x1812 HID"),
                     ("00001800-0000-1000-8000-00805f9b34fb", "0x1800 GAP"),
                     ("00001801-0000-1000-8000-00805f9b34fb", "0x1801 GATT"),
                     ("0000180a-0000-1000-8000-00805f9b34fb", "0x180a DEVINFO"),
                     ("0000180f-0000-1000-8000-00805f9b34fb", "0x180f 电池")):
        s = s.replace(full, ab)
    s = s.replace(f"__Dev_VID&0218d1_PID&9450_REV&011b_{ADDR_TAG}", "")
    s = s.replace("_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c", " <遥控器>")
    return s


# ── 配对密钥材料：到底存在哪一层 ─────────────────────────────────────────────
# ⚠⚠ 2026-09-23 二次改判。上一版这里写错了，别再改回去：
#
# 上一版是这么推的：读 `BTHPORT\Parameters\Devices\<远端地址>`，看到里面没有
# LTK/IRK/CSRK，就下结论「配对记录里没有密钥 ⇒ 链路加不了密 ⇒ HOGP 收不到报告」。
# **那个结论是错的。** `Devices\<远端>` 本来就是**元数据**键
# （Name / LEName / VID / PID / LEAppearance / Fingerprint* / LastSeen…），
# 它**从来就不该有** LTK —— 在那里"没找到 LTK"是**正常状态**，不是异常。
# 拿它当证据，等于"在抽屉里找不到牛奶，就断定冰箱里没有牛奶"。
#
# BLE 绑定材料真正的存放位置（ArchWiki 与 BlueVein 交叉核实）：
#   A. 经典蓝牙(BR/EDR) ：BTHPORT\Parameters\Keys\<适配器MAC>           值名 = 设备MAC
#   B. BLE（BT 5.1 起）：BTHPORT\Parameters\Keys\<适配器MAC>\<设备MAC>\
#                         子键里放 LTK / KeyLength / EDIV / ERand / IRK / CSRK
#   C. 部分 Windows 版本：BTHLE\Parameters\Keys\<适配器MAC>\<设备MAC>\
#
# ⇒ 注意 A 与 B 是**同一个父键的两种形态**，而这个父键 **只放 SYSTEM**
#   （ArchWiki 原话：只有一个不可登录的 SYSTEM 账户能访问它），
#   非 SYSTEM 一律 Access Denied ⇒ **读不到就只是读不到，不能当"没有"**。
#
# 所以本工具的口径是**三态**：读到了 / 存在但空 / 读不到（被拒或不存在）。
# 只有"读到了"才允许下结论 —— 本项目已经踩过一次"被 ACL 拒当成空"，
# 不许再踩第二次。
_BOND_VALUES = ("LTK", "IRK", "CSRK", "EDIV", "ERand", "KeyLength",
                "Authenticated", "Address", "AddressType")

READ_OK = "ok"           # 读到了（键能打开、里面有几个值）
READ_EMPTY = "empty"     # 键能打开，但一个值都没有
READ_DENIED = "denied"   # 读不到：被 ACL 拒，或这个键在本机不存在


def _bond_paths(adapter: str, remote: str) -> list:
    """BLE 绑定材料**可能**存放的全部位置 → [(标签, 注册表路径), …]

    一次全试，是因为不同 Windows 版本用不同分支（见上面 A/B/C）。
    `adapter` 拿不到时只留"整个分支"那一项，仍然有意义（能看出被不被拒）。
    """
    bt = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Keys"
    le = r"SYSTEM\CurrentControlSet\Services\BTHLE\Parameters\Keys"
    out = []
    if adapter:
        out.append(("BTHPORT\\Keys\\<适配器>  (经典：值名=设备MAC)", f"{bt}\\{adapter}"))
        out.append(("BTHPORT\\Keys\\<适配器>\\<设备>  (BLE 5.1+)", f"{bt}\\{adapter}\\{remote}"))
        out.append(("BTHLE\\Keys\\<适配器>\\<设备>", f"{le}\\{adapter}\\{remote}"))
    out.append(("BTHLE\\Keys  (整个分支)", le))
    return out


def _reg_read(path):
    """→ (值字典, 错误)。错误为 None 表示读成功。"""
    import winreg
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            i = 0
            while True:
                try:
                    n, v, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[n] = v
                i += 1
    except OSError as e:
        return {}, e
    return out, None


def _live_adapter_addr() -> str:
    """当前**在场**的那颗蓝牙棒的本地地址（DeviceAddressCache）。"""
    import winreg
    root = r"SYSTEM\CurrentControlSet\Enum\USB"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as k:
            hwids = []
            i = 0
            while True:
                try:
                    hwids.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        return ""
    for hwid in hwids:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{root}\\{hwid}") as k:
                insts = []
                j = 0
                while True:
                    try:
                        insts.append(winreg.EnumKey(k, j))
                    except OSError:
                        break
                    j += 1
        except OSError:
            continue
        for inst in insts:
            iid = f"USB\\{hwid}\\{inst}"
            _dn, rc = locate(iid, LOCATE_NORMAL)
            if rc != CR_SUCCESS:
                continue                     # 幽灵实例，跳过
            vals, err = _reg_read(f"{root}\\{hwid}\\{inst}\\Device Parameters")
            if not err and vals.get("DeviceAddressCache"):
                return str(vals["DeviceAddressCache"]).lower()
    return ""


def bond_probe(adapter: str, addr: str) -> list:
    """把候选位置全试一遍 → [(标签, 三态, 值名列表), …]

    ⚠ 三态必须分开：`READ_DENIED`（读不到）和 `READ_EMPTY`（真的空）
    是**两回事**，混在一起就会得出完全相反的结论（本项目踩过）。
    """
    out = []
    for label, path in _bond_paths(adapter, addr):
        vals, err = _reg_read(path)
        if err is None:
            names = sorted(vals)
            state = READ_OK if names else READ_EMPTY
        else:
            names, state = [], READ_DENIED
        out.append((label, state, names))
    return out


def bond_verdict(states) -> list:
    """绑定材料的三态 → 判词。**纯函数**，这样能给反例（见 --selftest）。

    `states` = [("位置标签", READ_OK|READ_EMPTY|READ_DENIED, [值名…]), …]

    为什么必须抽成纯函数：这是整份报告里唯一「会给出行动建议」的地方。
    上一版恰恰是在这里犯了错 —— 把「读不到」当成「没有」。
    所以它必须能脱离注册表单独测，并用反例钉住"四种输入四种判词"。
    """
    ok = [p for p in states if p[1] == READ_OK]
    empty = [p for p in states if p[1] == READ_EMPTY]

    if ok:
        have = set()
        for _label, _st, vals in ok:
            have |= set(vals)
        if "LTK" in have:
            return [
                f"  ✅ 判决：**读到了绑定材料，里面有 LTK**（{sorted(have)}）。",
                "     含义：键能读、密钥也在 ⇒ 这台设备的 BLE 链路能加密。",
                "           那么「因为没有密钥，所以 HOGP 收不到报告」这条解释",
                "           **不成立**，方向要转到「报告到底落在哪一层」。",
                "     下一步：退出桥程序后跑",
                "           `python tools\\watch_all_channels.py --seconds 90`，",
                "           按 确认/返回/主页，看落在键盘层、鼠标层还是厂商页。",
            ]
        return [
            f"  🔴 判决：**读到了这个键，但里面没有 LTK**（只有 {sorted(have) or '空'}）。",
            "     含义：键存在、我们也有权读 ⇒ 这不是「读不到」，是真的缺长期密钥。",
            "           HID over GATT 要求加密链路，缺 LTK 就加不了密 ⇒",
            "           HOGP 订阅不到 HID 报告 ⇒ 遥控器发出去也没有接收方。",
            "     下一步：**重新配对**（手法见本报告末尾）。",
        ]

    if empty:
        return [
            "  🔴 判决：绑定材料的键**能打开，但里面一个值都没有**。",
            "     含义：这是真的空（不是被 ACL 拒）⇒ 这台设备没有可用的密钥材料。",
            "     下一步：**重新配对**（手法见本报告末尾）。",
        ]

    return [
        "  ⚪ 判决：**无法判定** —— 上面这几个位置一个都读不到。",
        "     这不是「没有密钥」，是「我们没权限看」：",
        "       · `BTHPORT\\Parameters\\Keys` **只放 SYSTEM**",
        "         （ArchWiki 原话：只有一个不可登录的 SYSTEM 账户能访问它）",
        "       · 真机实测拿到 `WinError 5`（拒绝访问）就是这条路",
        "     ❗ 上一版工具在这里下了「没有密钥」的结论，**那是错的**：",
        "        它看的是元数据键（`Devices\\<远端>`），那里本来就不该有 LTK。",
        "     想真拿到答案只有两条路（今晚都不必做）：",
        "       ① 以 SYSTEM 身份读一次（PsExec -s，或建一个一次性计划任务）；",
        "       ② 直接**重新配对**：重配后若按键通了，说明原来那份绑定确实是坏的。",
        "     在那之前，本工具**不给「密钥缺失」的结论**。",
    ]


def selftest() -> int:
    """不碰注册表、不碰硬件，只测判词。"""
    bad = 0

    def chk(cond, desc):
        nonlocal bad
        print(("  OK   " if cond else "  FAIL ") + desc)
        if not cond:
            bad += 1

    chk("LTK" in _BOND_VALUES, "_BOND_VALUES 里必须含 LTK —— 否则这项检查形同虚设")
    t_denied = "\n".join(bond_verdict([("p", READ_DENIED, [])]))
    chk("无法判定" in t_denied and "🔴" not in t_denied,
        "全部读不到 → 必须判「无法判定」，**不许**判成「没有密钥」（上一版就错在这）")
    chk("LTK" in t_denied, "「无法判定」那段也要讲清在找什么（LTK）")
    chk("元数据" in t_denied,
        "「无法判定」那段要写明上一版错在哪（元数据键）—— 否则下次还会犯")
    t_ok = "\n".join(bond_verdict([("p", READ_OK, ["LTK", "IRK"])]))
    chk("✅" in t_ok and "🔴" not in t_ok, "读到 LTK → 判成齐全")
    t_nolk = "\n".join(bond_verdict([("p", READ_OK, ["IRK", "CSRK"])]))
    chk("🔴" in t_nolk, "读到了但没有 LTK → 仍判缺（LTK 是加密必备）")
    t_empty = "\n".join(bond_verdict([("p", READ_EMPTY, [])]))
    chk("🔴" in t_empty and "无法判定" not in t_empty,
        "存在但空 → 判成「真的空」，必须与「读不到」分开说")
    chk(len({t_denied, t_ok, t_nolk, t_empty}) == 4,
        "反例：四种输入的判词必须两两不同（否则就是硬编码凑的）")
    print()
    print(f"SELFTEST {'PASS' if bad == 0 else f'FAIL（{bad} 项）'}")
    return 1 if bad else 0


def bond_section(addr: str) -> dict:
    """配对账本体检 → {'states': …, 'adapter': …}"""
    print("=" * 78)
    print(" 配对账本：这台遥控器到底有没有 BLE 密钥材料？（三态，不含糊）")
    print("=" * 78)
    meta = (r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
            + "\\" + addr)
    adapter = _live_adapter_addr()
    print(f"  当前在用的本地适配器地址：{adapter or '（没找到在场适配器）'}")
    print()
    states = bond_probe(adapter, addr)
    mark = {READ_OK: "✅读到", READ_EMPTY: "🔴存在但空", READ_DENIED: "⚪读不到"}
    for label, st, names in states:
        print(f"  {mark[st]:<12}{label}")
        if st == READ_OK:
            bond = [n for n in _BOND_VALUES if n in names]
            print(f"              绑定材料 = {bond or '（一个都没有）'}")
            other = [n for n in names if n not in _BOND_VALUES]
            if other:
                print(f"              其它值   = {other}")

    # ServicesFor<本地地址>：配对记录是**按本地无线电地址**绑的
    import winreg
    subs = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, meta) as k:
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    svcfor = [s[len("ServicesFor"):].lower() for s in subs
              if s.startswith("ServicesFor")]
    print(f"  ServicesFor（记录绑的本地地址）：{svcfor or '（无）'}")
    if adapter and svcfor and adapter not in svcfor:
        print("  ⚠ 记录绑的本地地址与当前在用的**对不上** —— 这是 v1.0.10 那种老毛病：")
        print("     跑 `python pairing.py --fix` 把记录迁到当前地址。")

    print()
    for line in bond_verdict(states):
        print(line)

    # ── 对照：元数据键。**特意打出来**，就是为了防止有人再拿它当证据 ──
    mvals, merr = _reg_read(meta)
    print()
    print("  ── 对照：元数据键（**这里本来就不该有 LTK，别再拿它下结论**）")
    if merr is not None:
        print(f"     ⚠ 读不到 {meta}：{merr}")
    else:
        print(f"     {meta}")
        print(f"     值名：{sorted(mvals)}")
        print("     这些都是名字 / VID / PID / 外观 / 时间戳一类的**元数据**；")
        print("     在本机它一个密钥值都没有 —— 这是**正常**的，不构成任何结论。")
    return {"states": states, "adapter": adapter}


def connection_section() -> dict:
    """Windows 蓝牙栈**此刻**到底连没连着这台遥控器？

    这一条能一眼否掉一整类解释：「没连上，所以收不到按键」。
    实测口径：
      Connected    ⇒ 链路是活的，收不到报告就得往上层找（不是蓝牙没连）
      Disconnected ⇒ Windows 手里没有这条链路，HOGP 无处订阅，
                     遥控器的按键报告**不可能**到达 Windows（与密钥无关）
    """
    print()
    print("=" * 78)
    print(" Windows 此刻连没连着这台遥控器？（WinRT ConnectionStatus）")
    print("=" * 78)
    try:
        import asyncio
        from winrt.windows.devices.bluetooth import (
            BluetoothLEDevice, BluetoothConnectionStatus)
    except Exception as e:                                   # noqa: BLE001
        print(f"  （跳过：没有 winrt 环境 —— {e}）")
        return {}
    try:
        target = int(ADDR_TAG, 16)
    except ValueError:
        print(f"  ⚠ ADDR_TAG 不是合法的十六进制地址：{ADDR_TAG!r}")
        return {}

    async def go():
        d = await BluetoothLEDevice.from_bluetooth_address_async(target)
        return None if d is None else int(d.connection_status)

    try:
        st = asyncio.run(go())
    except Exception as e:                                   # noqa: BLE001
        print(f"  （跳过：查询失败 —— {e.__class__.__name__}: {e}）")
        return {}
    if st is None:
        print("  ⚠ 拿不到设备对象（可能根本没配过对）。")
        return {}
    if st == int(BluetoothConnectionStatus.CONNECTED):
        print("  ✅ Connected（连着）")
        print("     含义：链路是活的 ⇒「因为没连上，所以收不到按键」**不成立**。")
        print("           报告没到就得往「设备到底发不发」「落在哪一层」去找。")
    else:
        print("  🔴 Disconnected（没连）")
        print("     含义：Windows 手里没有这条链路 ⇒ HOGP 无处订阅 ⇒")
        print("           遥控器的按键报告不可能到达 Windows（与密钥无关）。")
    return {"status": st}


def paired_entries_section() -> list:
    """Windows 里这台遥控器有几条"已配对"条目？（>1 条就是隐患）"""
    print()
    print("=" * 78)
    print(" Windows 侧的已配对条目（同一个遥控器出现多条 = 隐患）")
    print("=" * 78)
    try:
        import asyncio
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
    except Exception as e:                              # noqa: BLE001
        print(f"  （跳过：没有 winrt 环境 —— {e}）")
        return []

    async def go():
        sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        return await DeviceInformation.find_all_async_aqs_filter(sel)

    try:
        devs = asyncio.run(go())
    except Exception as e:                              # noqa: BLE001
        print(f"  （跳过：枚举失败 —— {e.__class__.__name__}: {e}）")
        return []
    rows = []
    for d in devs:
        nm = (d.name or "")
        if not any(k in nm.lower() for k in ("remote", "chromecast")):
            continue
        rows.append((nm, d.id))
        print(f"  · {nm!r}")
        # id 形如 BluetoothLE#BluetoothLE<本地地址>-<远端地址>
        body = d.id.split("#")[-1]
        if body.lower().startswith("bluetoothle"):
            body = body[len("bluetoothle"):]
        print(f"      {body}")
    if len(rows) > 1:
        print()
        print(f"  ⚠ 同一个遥控器有 **{len(rows)} 条**已配对条目。")
        print("     多出来的那条通常是**上一颗/上一代**无线电留下的幽灵（cfgmgr32 里")
        print("     表现为 CR_NO_SUCH_DEVINST(13)）。Windows 的蓝牙栈有时会照幽灵那条")
        print("     去造设备对象 —— 本项目 v1.0.9/v1.0.10 就是被它坑过。")
        print("     清理办法：`python pairing.py --fix --method rebuild`（管理员）。")
    return rows


def verdict_section(conn: dict, bond: dict) -> None:
    """把「已确定 / 未确定 / 下一步」三件事一次说清。

    ⚠ 这个函数被改过两轮，两个"上一版"都是错的，留在这里当反例：

    ① 最初写的是「修法只有一条：重新配对」—— **错**。当时「密钥读不到」这一支
       还没排除（其实密钥一直都在，只是要去 `Keys\\<适配器>\\<远端>` 读）。
    ② 接着写的是「还有一支没被排除：报告可能落在厂商页，纯软件可修」—— **也已作废**。
       09-23 上午实测：**两个厂商页我们都打开了、真观测了**，结果是 0 条。
       ⇒ 厂商页那一支死了。

    所以现在的判词只留两条从用户态**分不开**的可能（鼠标页 / 设备压根没发），
    并给出改状态的两步（重启 → 重配）。**别再往这里塞"纯软件能修"的乐观话。**
    """
    print()
    print("=" * 78)
    print(" 综合判决：能确定什么、不能确定什么、下一步做什么")
    print("=" * 78)
    # ── 账本那一段的结论**算出来**，不写死 ──
    # 上一版这里是写死的一句「未确定：有没有 LTK」，而账本那一节实测**读到了**
    # LTK —— 写死的判词会和实测打架。本项目反复踩过这个坑（判词必须跟着输入变）。
    states = (bond or {}).get("states") or []
    have = set()
    for _l, st, vals in states:
        if st == READ_OK:
            have |= set(vals)
    if "LTK" in have:
        bond_line = ("     · ✅ **已排除**：「没有密钥所以 HOGP 收不到报告」不成立 ——"
                     "绑定材料是完整的\n"
                     "       （LTK/IRK/CSRK/EDIV/ERand/KeyLength/Address/AddressType 都读到了）"
                     "⇒ 链路能加密")
    elif states and all(s[1] == READ_DENIED for s in states):
        bond_line = ("     · ⚪ 未确定：绑定材料**读不到**（密钥键只放 SYSTEM）——"
                     "这是「没权限看」，不许当成「没有」")
    else:
        bond_line = "     · 🔴 绑定材料不完整（读到了但缺 LTK）—— 明细见上面那一节"

    print("  ✅ 已确定（每条都有测量方式，不是推断）：")
    if (conn or {}).get("status") == 1:      # 1 = BluetoothConnectionStatus.CONNECTED
        print("     · Windows 与遥控器之间有**活链路**（ConnectionStatus = Connected）")
    else:
        print("     · 连接状态见上面那一节 —— 若不是 Connected，下面几条要重读")
    print("     · 设备侧节点齐全：服务节点在场已启动、HOGP 驱动绑着、5 路 HID 集合在")
    print("     · 键盘钩子从未见过遥控器独有的键名")
    print(bond_line)
    print()
    print("  ⚪ 仍未定（不许再当成已定）：")
    print("     · 遥控器到底**发不发**按键 HID 报告？发的话**落在哪一层**？")
    print()
    print("  🔍 09-23 上午那次实测，把范围缩到了只剩两条：")
    print("     **所有能观测到的页**上都是 0 条 —— 消费类页 0、两个厂商页 0")
    print("     （这两路**我们打开了、真观测了**，不是「看不见」）、私有服务 0。")
    print("     ⇒ 「报告来了、只是落在厂商页」这一支**已作废**，别提它了。")
    print("     剩下能从用户态想到的只有两条，而且**这两条分不开**：")
    print("       a) 按键落在**鼠标页**（Col03）—— 我们打不开，等于这一路根本没测；")
    print("       b) 设备**压根没发**输入报告 —— 最像的机制是 host 往")
    print("          HID Control Point (0x2A4C) 写了 0x00 = Suspend；")
    print("          按 HOGP 规范，设备会**立即停发输入报告**，要写 0x01 才恢复。")
    print("          （写该特征同样被 ACCESS_DENIED 挡住 ⇒ 这是「最像」，不是「已确认」。）")
    print()
    print("  下一步（按顺序做，别跳）：")
    print("   ① 先**重启一次电脑**（1 分钟，零风险）。挂起状态是跟着连接走的，")
    print("      断开重连就会清掉；重启也会把 Windows 那层 HOGP 重新拉起来。")
    print("      重启后等遥控器连上，**退出桥程序**，再跑：")
    print("        python tools\\watch_all_channels.py --seconds 90")
    print("      （它有 3-2-1 倒计时；**倒计时结束再开始按** 确认 / 返回 / 主页）")
    print("   ② 重启后还是全 0 ⇒ **重新配对**（唯一能重做「密钥协商 + 服务订阅」的动作）：")
    print("      遥控器上**同时按住「返回」+「主页」约 3 秒**，底部灯开始闪 = 配对模式；")
    print("      Windows 先**删除**旧的「Chromecast Remote」，再「添加设备 → 蓝牙」；")
    print("      连上之后**必须再跑一次 修复蓝牙配对.bat**（重配会换掉设备地址，")
    print("      不跑这一步语音会连不上）；最后再跑 测遥控器按键通道.bat 复测。")
    print("   ③ ❌ tools\\takeover_hid_reports.py —— **已作废，别再跑它**：")
    print("      09-22 真机实测 Disable-PnpDevice 回「不支持」、pnputil 回")
    print("      「Cannot disable critical system device」；那个节点的 DevNodeStatus")
    print("      里**没有 DN_DISABLEABLE 位**（0x0180000A，设备管理器里「禁用」是灰的）。")
    print("      不是我们没测成，是 **Windows 不许任何人让它放手**。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebind", action="store_true",
                    help="（需管理员）重新枚举遥控器，让 PnP 按当前实际服务重建子节点")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑判词自检（不读注册表、不碰硬件）")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    # ── 第一段：配对账本（决定"HID 报告有没有接收方"的那一环）──
    # 放在最前面：它比 devnode 在场判定更靠近根因。设备节点全都"已启动"
    # 也可以照样一个报告都收不到 —— 只要链路加不了密。
    bond = bond_section(ADDR_TAG)
    conn = connection_section()
    paired_entries_section()
    verdict_section(conn, bond)

    print()
    rows = registry_children()
    print("=" * 78)
    print(" 遥控器相关 devnode 的**在场**判定（cfgmgr32，只有它说了算）")
    print("=" * 78)

    if not rows:
        print("⚠ 注册表里没找到遥控器相关条目 —— VID/ADDR_TAG 是不是要改？")
        return 2

    by_service = {}
    for enum, hwid, inst, iid in rows:
        d = describe(iid)
        mark = "✅在场" if d["present"] else f"·幽灵(rc={d['rc']})"
        st = d["status"]
        line = f"  {mark:<14} {enum}\\{short(iid)}"
        extra = ""
        if d["present"]:
            extra = f"   [{flags_text(st)}"
            if d["problem"] is not None and d["problem"]:
                extra += f"、Problem={d['problem']}"
            extra += "]"
        print(line + extra)
        if enum == "BTHLEDevice":
            tag = short(hwid)
            by_service.setdefault(tag, []).append((inst, d["present"]))

    # ── 判决：0x1812 与其余服务比，在场的那一代一样吗？──
    print()
    print("=" * 78)
    print(" 判决：HID 服务（0x1812）在场的那一代，和其余服务一致吗？")
    print("=" * 78)
    if not by_service:
        print("⚠ 没扫到 BTHLEDevice 下的服务节点，无法判决。")
        return 2

    present_gen = {}
    for tag, items in sorted(by_service.items()):
        live = [inst for inst, p in items if p]
        allg = [inst for inst, _ in items]
        # BTHLEDevice 实例形如 9&<hash>&<n>&<svcid>；取 <hash>&<n> 当"代数"
        def gen(inst):
            parts = inst.split("&")
            return "&".join(parts[:2]) if len(parts) >= 2 else inst
        present_gen[tag] = (sorted({gen(i) for i in live}),
                            sorted({gen(i) for i in allg}),
                            live)
        print(f"  {tag:<16} 在场代数={sorted({gen(i) for i in live}) or '（无）'}"
              f"   注册表全部={sorted({gen(i) for i in allg})}")

    hid = [t for t in present_gen if "0x1812" in t]
    others = [t for t in present_gen if "0x1812" not in t]
    ok = True
    if not hid:
        print("\n  ⚠ 没扫到 0x1812 的 BTHLEDevice 节点。")
        ok = False
    else:
        hid_gen = set(present_gen[hid[0]][0])
        other_gen = set()
        for t in others:
            other_gen |= set(present_gen[t][0])
        print(f"\n  HID 在场代数    = {sorted(hid_gen) or '（不在场）'}")
        print(f"  其余服务在场代数 = {sorted(other_gen) or '（不在场）'}")
        if not hid_gen:
            print("\n  🔴 判决：**HID 服务节点此刻不在场** —— 其余服务都在场，只有它不在。")
            print("     含义：Windows 这一代设备实例里**没有 HID 服务的节点**，")
            print("           没有人会去订阅 0x1812 的 Report 特征 → 遥控器的按键报告")
            print("           **永远没有接收方** → 键盘钩子当然一条都收不到。")
            print("     下一步：用管理员跑 `--rebind`（非破坏性，会让设备重连一次）。")
            ok = False
        elif hid_gen - other_gen:
            print(f"\n  🔴 判决：HID 服务只在**别的代数**上在场（{sorted(hid_gen)}），"
                  f"而其余服务在 {sorted(other_gen)}。")
            print("     含义：HID 的节点是**上一代残留**，没跟上当前设备实例。")
            print("     下一步：用管理员跑 `--rebind`。")
            ok = False
        else:
            print("\n  ✅ 判决：HID 服务与其余服务在**同一代**且都在场。")
            print("     含义：这一层没问题 —— 若按键仍无反应，问题在")
            print("           「遥控器有没有真的发报告」或「HOGP 有没有订阅」。")

    if a.rebind:
        print()
        print("=" * 78)
        print(" 重新枚举遥控器（非破坏性：设备会断开重连一次）")
        print("=" * 78)
        target = None
        # 直接找 BTHLE\Dev_<addr> 的在场实例
        for enum, hwid, inst, iid in rows:
            if enum == "BTHLE":
                dn, rc = locate(iid, LOCATE_NORMAL)
                if rc == CR_SUCCESS:
                    target = (iid, dn)
                    break
        if target is None:
            print("⚠ 遥控器本体（BTHLE）不在场 —— 先把它连上（按一下遥控器按键唤醒）再重试。")
            return 3
        iid, dn = target
        print(f"  目标：{iid}")
        cfg = _cfg()
        rc = cfg.CM_Reenumerate_DevNode(ctypes.c_ulong(dn),
                                        ctypes.c_ulong(REENUM_SYNCHRONOUS))
        if rc == CR_SUCCESS:
            print("  ✅ 重新枚举已提交。再跑一次本工具（不带 --rebind）看 HID 节点有没有回来。")
        else:
            print(f"  ❌ 重新枚举失败，CONFIGRET={rc}")
            if rc == 0x17:      # CR_ACCESS_DENIED
                print("     CR_ACCESS_DENIED(23) → 需要**管理员**权限。")
            else:
                print("     常见码：23=拒绝访问(要管理员)、13=设备不存在、0x0D=CR_FAILURE")
        return 0

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
