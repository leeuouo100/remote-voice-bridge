"""把蓝牙无线电关掉 3 秒再打开 —— 逼 Windows 重做一遍 HOGP 握手。

## 为什么需要它（2026-09-24）

同一个遥控器在苹果系统上是"插上就能用"，说明**遥控器的 HID 报告本身是正常的**。
这台 Windows 上：HOGP 栈确实占着 HID 服务（`0x1812` 枚举特征报 ACCESS_DENIED），
HID 集合也都在（5 路全"已启动"），可是**一条输入报告都不上来**，而同一台设备
的 ATVV（语音键，走另一条服务）天天在用。

最像的一层是：**HOGP 那次 attach 是半拉子的** —— 服务占住了，但 Report 订阅 /
Control Point 握手没真正建立，于是设备干脆不发。重配对能治，但代价大（要重新
配对、还可能把现在能用的语音键也弄断）。

无线电「关→开」是**不需要重配对**的最小复位：所有 BLE 链路断掉、Windows 的
HOGP 必须重新 attach，这一遍是干净的。开关蓝牙正是 Windows 设置面板那个开关
（`Windows.Devices.Radios`），不动配对记录、不动注册表。

## 用法

    python tools\\radio_cycle.py            # 关 3 秒再开，然后等遥控器重连
    python tools\\radio_cycle.py --off-only # 只关（应急）
    python tools\\radio_cycle.py --status   # 只看状态

退出码：0 正常；2 拿不到无线电控制权（多半要在设置里手动开一下）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()


def say(s: str = "") -> None:
    print(s, flush=True)


# ⚠ 这个模块是本机唯一"不用管理员就能开关蓝牙"的路子（设置面板那个开关就是它）。
#   它不在默认依赖里（需求是可选的）⇒ 缺了要给**能照着做的手动步骤**，不能崩。
RADIOS_OK = True
try:
    from winrt.windows.devices.radios import Radio, RadioKind, RadioState  # noqa: F401
except ImportError:                                     # noqa: BLE001
    RADIOS_OK = False

_HOWTO = """\
本机没有 `winrt-Windows.Devices.Radios` 模块，所以脚本没法替你开关。
手动做**完全等价**，三步：
  1) 点任务栏右下角的「网络/音量/电池」那一块 → 找到【蓝牙】磁贴 → 关掉
     （或者：设置 → 蓝牙和其他设备 → 蓝牙开关 → 关）
  2) 等 3 秒，再打开
  3) 按一下遥控器任意键把它叫醒，然后去按【确认/返回/主页】试试

要装模块再跑（可选）：
  <你的 Python> -m pip install winrt-Windows.Devices.Radios
"""


async def radios():
    if not RADIOS_OK:
        return []
    rs = await Radio.get_radios_async()
    return [r for r in rs if r.kind == RadioKind.BLUETOOTH]


def state_name(v) -> str:
    return {0: "Unknown", 1: "On", 2: "Off", 3: "Disabled"}.get(int(getattr(v, "value", v)), str(v))


async def set_state(r, want) -> str:
    st = await r.set_state_async(want)
    return {0: "Unspecified", 1: "Allowed", 2: "DeniedByUser", 3: "DeniedBySystem"}.get(
        int(getattr(st, "value", st)), str(st))


async def remote_connected() -> list:
    """已配对的、名字像遥控器的 BLE 设备，各自的连接状态。"""
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
    devs = await DeviceInformation.find_all_async_aqs_filter(sel)
    out = []
    for d in devs:
        nm = (d.name or "")
        if "remote" not in nm.lower() and "chromecast" not in nm.lower():
            continue
        try:
            b = await BluetoothLEDevice.from_id_async(d.id)
            out.append((nm, int(b.connection_status.value)))
        except Exception as e:                          # noqa: BLE001
            out.append((nm, f"打不开：{e.__class__.__name__}"))
    return out


async def main(argv: list) -> int:
    say(f"蓝牙无线电复位 —— {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if not RADIOS_OK:
        say("⚠ 缺模块，改走手动步骤：\n")
        say(_HOWTO)
        return 2
    rs = await radios()
    if not rs:
        say("❌ 没找到蓝牙无线电（这台机器没有蓝牙？）")
        return 2
    for r in rs:
        say(f"  ▸ {r.name!r}  当前状态={state_name(r.state)}")

    if "--status" in argv:
        return 0

    bad = []
    for r in rs:
        st = await set_state(r, RadioState.OFF)
        say(f"  关 {r.name!r} → {st}")
        if st != "Allowed":
            bad.append((r.name, st))

    if "--off-only" in argv:
        say("（--off-only：只关不开。要恢复请再跑一次不带参数的）")
        return 0

    time.sleep(3.0)
    for r in rs:
        st = await set_state(r, RadioState.ON)
        say(f"  开 {r.name!r} → {st}")
        if st != "Allowed":
            bad.append((r.name, st))
    if bad:
        say(f"❌ 有无线电没被允许改状态：{bad}")
        say("   ⇒ 请到「设置 → 蓝牙和其他设备」手动关一下再打开（效果一样）。")
        return 2

    say("\n  等遥控器重连（最多 60 秒；此时按一下遥控器任意键能把它叫醒）…")
    t0 = time.time()
    last = None
    while time.time() - t0 < 60:
        rows = await remote_connected()
        txt = " / ".join(f"{n}:{s}" for n, s in rows) or "（一个都没枚举到）"
        if txt != last:
            say(f"    [{time.time() - t0:4.1f}s] {txt}")
            last = txt
        if any(str(s) == "1" for _, s in rows):
            say(f"\n✅ 遥控器已重连（{time.time() - t0:.1f}s）—— 现在去按它的【确认/返回/主页】试试。")
            return 0
        await asyncio.sleep(1.5)
    say("\n⚠ 60 秒内没看到它连上。按一下遥控器的键把它叫醒，再跑一次 "
        "`--status` 看连接状态。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
