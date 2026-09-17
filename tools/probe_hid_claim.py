"""
能不能把 HID 服务「要」过来？—— 换几个入口试。

## 背景

`tools/probe_gatt_hid.py` 用 `ble.get_gatt_services_async()` → 逐个
`svc.get_characteristics_async()`，结果：

    ▸ 00001812 (HOGP)  ⚠ 枚举特征失败 status=3   ← 3 = ACCESS_DENIED
    ▸ ab5e0001 (ATVV)  ⚠ 枚举特征失败 status=3   ← 被本机桥程序占着

两个服务都被拒，而 GAP/GATT/DIS/Battery/两个私有服务都读得到 —— 所以这是
**按服务**的独占，不是权限问题。ATVV 那个被我们自己占着能解释；
HID 那个被谁占着、以及**能不能换个入口绕过去**，就是本脚本要回答的。

## 三个入口

  ① `svc.device_id` → `GattDeviceService.from_id_async()`   （官方"按服务重开"的口子）
  ② `GattDeviceService.get_device_selector_from_uuid()` + `DeviceInformation`
  ③ 直接 `get_characteristics_for_uuid_async(0x2A4D)` —— **不枚举、直接点菜**，
     枚举被拒不代表按 UUID 取也被拒

任一条成功，本项目就能在**已有的 ATVV 连接上顺手订阅 HID 报告**，
彻底绕开 Windows 的 HOGP 栈（后者在本机显然没有把报告送上来）。

用法：
    python tools/probe_hid_claim.py                 # 只试入口
    python tools/probe_hid_claim.py --listen 30     # 成功就订阅并听 30 秒
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

from config import APP_VERSION, CONFIG_DIR       # noqa: E402

HOGP = "00001812-0000-1000-8000-00805f9b34fb"
REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"
REPORT_MAP = "00002a4b-0000-1000-8000-00805f9b34fb"
CTRL_POINT = "00002a4c-0000-1000-8000-00805f9b34fb"
PROTOCOL_MODE = "00002a4e-0000-1000-8000-00805f9b34fb"

STATUS = {0: "Success", 1: "Unreachable", 2: "ProtocolError", 3: "ACCESS_DENIED"}


def st(v) -> str:
    try:
        n = int(getattr(v, "value", v))
    except Exception:                                   # noqa: BLE001
        return str(v)
    return f"{n} ({STATUS.get(n, '?')})"


def props(ch) -> str:
    names = [(0x02, "read"), (0x04, "write-noresp"), (0x08, "write"),
             (0x10, "notify"), (0x20, "indicate")]
    v = int(getattr(ch.characteristic_properties, "value",
                    ch.characteristic_properties))
    return "|".join(n for b, n in names if v & b) or f"0x{v:X}"


def main() -> int:
    listen = 0
    a = sys.argv[1:]
    for i, x in enumerate(a):
        if x == "--listen" and i + 1 < len(a):
            try:
                listen = max(0, min(300, int(a[i + 1])))
            except ValueError:
                pass
    try:
        asyncio.run(_run(listen))
    except Exception as e:                              # noqa: BLE001
        print(f"\n❌ 异常：{e.__class__.__name__}: {e}")
        return 1
    return 0


async def _run(listen: int) -> None:
    import uuid as _uuid

    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattCommunicationStatus, GattClientCharacteristicConfigurationDescriptorValue,
        GattDeviceService,
    )

    print("=" * 74)
    print(" HID 服务「要过来」测试")
    print(f" 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  版本：v{APP_VERSION}")
    print("=" * 74)

    sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
    devs = await DeviceInformation.find_all_async_aqs_filter(sel)
    tgt = None
    for d in devs:
        nm = (d.name or "").lower()
        if "remote" in nm or "chromecast" in nm:
            tgt = d
            break
    if tgt is None:
        print("❌ 没找到遥控器")
        return
    print(f"\n→ {tgt.name!r}\n  id = {tgt.id}")

    ble = None
    try:
        ble = await BluetoothLEDevice.from_id_async(tgt.id)
    except OSError as e:
        print(f"❌ from_id_async: {e}")
    if ble is None:
        print("❌ 拿不到设备对象")
        return
    print(f"  连接状态 = {int(ble.connection_status.value)}")

    # ── 先拿到 0x1812 这个 GattDeviceService 对象（服务级枚举本身是成功的）──
    svc = None
    res = await ble.get_gatt_services_async()
    for s in res.services:
        if str(s.uuid).lower() == HOGP:
            svc = s
            break
    if svc is None:
        print("❌ 找不到 0x1812")
        return
    print(f"\n▸ 0x1812 服务对象拿到了：uuid={svc.uuid}")
    try:
        print(f"  device_id        = {svc.device_id}")
    except Exception as e:                              # noqa: BLE001
        print(f"  device_id 读不到：{e}")
    try:
        print(f"  attribute_handle = {svc.attribute_handle}")
    except Exception as e:                              # noqa: BLE001
        print(f"  attribute_handle 读不到：{e}")

    # ── 入口 ①：按 UUID 直接点菜（不枚举）──────────────────────────
    print("\n" + "─" * 74)
    print("入口 ①：get_characteristics_for_uuid_async（不枚举，直接按 UUID 取）")
    print("─" * 74)
    got = {}
    for name, u in [("Report Map 0x2A4B", REPORT_MAP),
                    ("Report     0x2A4D", REPORT),
                    ("ControlPoint 0x2A4C", CTRL_POINT),
                    ("ProtocolMode 0x2A4E", PROTOCOL_MODE)]:
        try:
            r = await svc.get_characteristics_for_uuid_async(_uuid.UUID(u))
            n = len(r.characteristics)
            print(f"  {name}: status={st(r.status)}  拿到 {n} 个")
            if r.status == GattCommunicationStatus.SUCCESS and n:
                got[name] = list(r.characteristics)
                for ch in r.characteristics:
                    print(f"      - {ch.uuid}  [{props(ch)}]")
        except Exception as e:                          # noqa: BLE001
            print(f"  {name}: ❌ {e.__class__.__name__}: {e}")

    if got.get("Report     0x2A4D"):
        print("\n  🎯 入口 ① 成功 —— **不枚举也能拿到 Report 特征**，")
        print("     意味着本项目可以自己订阅 HID 报告，不必依赖 Windows 的 HOGP。")
    else:
        print("\n  ⚠ 入口 ① 没拿到 Report 特征。")

    # ── 入口 ②：from_id_async 重开这个服务 ─────────────────────────
    print("\n" + "─" * 74)
    print("入口 ②：GattDeviceService.from_id_async(svc.device_id)")
    print("─" * 74)
    svc2 = None
    try:
        did = svc.device_id
        svc2 = await GattDeviceService.from_id_async(did)
        print(f"  服务对象：{svc2}")
    except Exception as e:                              # noqa: BLE001
        print(f"  ❌ {e.__class__.__name__}: {e}")
    if svc2 is not None:
        try:
            cr = await svc2.get_characteristics_async()
            print(f"  get_characteristics_async → status={st(cr.status)}")
            for ch in cr.characteristics:
                print(f"      - {ch.uuid}  [{props(ch)}]")
        except Exception as e:                          # noqa: BLE001
            print(f"  ❌ 枚举特征：{e.__class__.__name__}: {e}")

    # ── 入口 ③：HID Control Point —— 试着写 Exit Suspend ────────────
    print("\n" + "─" * 74)
    print("入口 ③：HID Control Point（0x2A4C）—— 试着解除 HID 挂起")
    print("─" * 74)
    print("  HOGP 规范：host 写 0x00=Suspend 后设备**停止发输入报告**；")
    print("  写 0x01=Exit Suspend 才恢复。若 Windows 停在 Suspend，")
    print("  现象正好是「能连、无报告」—— 这一句就是解药。")
    cps = got.get("ControlPoint 0x2A4C") or []
    if not cps:
        print("  ⚠ 没拿到 Control Point 特征，暂时写不了。")
    else:
        from winrt.windows.storage.streams import DataWriter
        for ch in cps:
            for val, label in ((0x01, "Exit Suspend"),):
                try:
                    w = DataWriter()
                    w.write_byte(val)
                    buf = w.detach_buffer()
                    r = await ch.write_value_with_result_async(buf)
                    print(f"  写 0x{val:02X} ({label}) → status={st(r.status)}")
                except Exception as e:                  # noqa: BLE001
                    print(f"  写 0x{val:02X} 失败：{e.__class__.__name__}: {e}")

    # ── 可选：订阅并听 ─────────────────────────────────────────────
    reps = got.get("Report     0x2A4D") or []
    if listen and reps:
        print("\n" + "─" * 74)
        print(f"订阅 {len(reps)} 个 Report 特征，听 {listen} 秒 —— 请按遥控器的键")
        print("─" * 74)
        hits = []
        loop = asyncio.get_running_loop()
        toks = []
        for i, ch in enumerate(reps):
            def mk(i):
                def cb(sender, args):
                    try:
                        data = bytes(args.characteristic_value)
                    except Exception:                   # noqa: BLE001
                        data = b""
                    hits.append((i, data))
                    print(f"    📦 Report#{i}  {data.hex(' ')}")
                return cb

            try:
                toks.append((ch, ch.add_value_changed(mk(i))))
                r = await ch.write_client_characteristic_configuration_descriptor_async(
                    GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
                print(f"  ✅ 订阅 Report#{i}  status={st(r.status) if r else 'ok'}")
            except Exception as e:                      # noqa: BLE001
                print(f"  ❌ 订阅 Report#{i} 失败：{e.__class__.__name__}: {e}")
        t0 = loop.time()
        while loop.time() - t0 < listen:
            await asyncio.sleep(0.25)
        print(f"\n  收到 {len(hits)} 条")
        for ch, t in toks:
            try:
                ch.remove_value_changed(t)
            except Exception:                           # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
