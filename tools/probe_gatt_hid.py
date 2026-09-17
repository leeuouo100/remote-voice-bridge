"""
GATT 层直探：绕开 Windows HID 栈，问蓝牙本身三个问题。

## 为什么必须下到这一层

`tools/watch_reports.py` 读的是 **Windows 的 HID 集合**（`\\?\HID#...`）。
那已经是「蓝牙 → HoGP 驱动 → HID 栈」好几层之后的东西了。5 路集合全 0 条报告，
这个现象有**两个完全相反的成因**，在 HID 层看长得一模一样：

  ① 遥控器根本没往 HID 服务发报告
  ② 遥控器发了，但 **Windows 的 HoGP 从来没订阅那些 Report 特征**
     （订阅是按报告 ID 逐个做的，落在 CCCD 里）

只有下到 GATT 才能分开。本探针问三件事：

  【问 1】遥控器到底暴露了哪些服务/特征？—— 有没有除了 ATVV + HID 以外的
          「谷歌私有服务」（有些固件把按键放在那里）
  【问 2】**每个 Report 特征的 CCCD 值是多少？**
          0x0000 = 谁都没订阅 → 报告根本不会推上来（成因 ②，实锤）
          0x0001/0x0002 = 已订阅 NOTIFY/INDICATE
  【问 3】HID Report Map（0x2A4B）里声明了哪些 report ID / 用法页 / 长度？
          这份描述符是设备自报的，能直接对照"21 字节厂商页"的说法
          以及 config.CHROMECAST_BUTTONS 的用法码表。

不按任何键也能出结果 —— 问 1/2/3 都是读属性。

## 用法

    python tools/probe_gatt_hid.py            # 只做静态三问
    python tools/probe_gatt_hid.py --listen 40  # 附带订阅并实时听 40 秒

⚠ 不需要退出桥接程序：本探针只挂 HID 服务的 Report 特征，
  和桥程序挂的 ATVV 特征互不干扰，反而能验证"连接是活的"。
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

from config import APP_VERSION, CONFIG_DIR      # noqa: E402

OUT = CONFIG_DIR / "gatt-probe.txt"

# ATVV（本项目已经在用的那套），列出来是为了和别的服务对照
KNOWN = {
    "ab5e0001-5a21-4f05-bc7d-af01f617b664": "ATVV 主服务（本项目在用）",
    "ab5e0002-5a21-4f05-bc7d-af01f617b664": "ATVV TX（host→remote）",
    "ab5e0003-5a21-4f05-bc7d-af01f617b664": "ATVV AUDIO（音频流）",
    "ab5e0004-5a21-4f05-bc7d-af01f617b664": "ATVV CTL（控制指令）",
    "00001812-0000-1000-8000-00805f9b34fb": "HID over GATT（HOGP）",
    "00001800-0000-1000-8000-00805f9b34fb": "GAP",
    "00001801-0000-1000-8000-00805f9b34fb": "GATT",
    "0000180a-0000-1000-8000-00805f9b34fb": "Device Information",
    "0000180f-0000-1000-8000-00805f9b34fb": "Battery",
    "00001816-0000-1000-8000-00805f9b34fb": "Cycling Speed and Cadence",
    "00001805-0000-1000-8000-00805f9b34fb": "Current Time",
    "00002a4b-0000-1000-8000-00805f9b34fb": "Report Map",
    "00002a4d-0000-1000-8000-00805f9b34fb": "HID Report",
    "00002a4a-0000-1000-8000-00805f9b34fb": "HID Information",
    "00002a4c-0000-1000-8000-00805f9b34fb": "HID Control Point",
    "00002a4e-0000-1000-8000-00805f9b34fb": "Protocol Mode",
    "00002a19-0000-1000-8000-00805f9b34fb": "Battery Level",
    "00002902-0000-1000-8000-00805f9b34fb": "CCCD（订阅配置）",
}

# GattCharacteristicProperties 的位定义
PROPS = [
    (0x01, "broadcast"),
    (0x02, "read"),
    (0x04, "write-noresp"),
    (0x08, "write"),
    (0x10, "notify"),
    (0x20, "indicate"),
    (0x40, "auth-write"),
    (0x80, "ext-props"),
]


def props_str(v: int) -> str:
    got = [n for bit, n in PROPS if v & bit]
    return "|".join(got) or f"0x{v:X}"


def who(uuid_s: str) -> str:
    return KNOWN.get(uuid_s.lower(), "")


def main() -> int:
    listen = 0
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--listen" and i + 1 < len(argv):
            try:
                listen = max(0, min(300, int(argv[i + 1])))
            except ValueError:
                pass
    try:
        asyncio.run(_run(listen))
    except Exception as e:                              # noqa: BLE001
        print(f"\n❌ 探针异常：{e.__class__.__name__}: {e}")
        return 1
    return 0


async def _run(listen: int) -> None:
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattCommunicationStatus, GattClientCharacteristicConfigurationDescriptorValue,
    )

    L: list[str] = []

    def say(s: str = "") -> None:
        print(s)
        L.append(s)

    say("=" * 74)
    say(" remote-voice-bridge · GATT 层直探（绕开 Windows HID 栈）")
    say(f" 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  版本：v{APP_VERSION}")
    say("=" * 74)

    selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
    devices = await DeviceInformation.find_all_async_aqs_filter(selector)

    target = None
    say("\n【已配对的 BLE 设备】")
    for d in devices:
        nm = d.name or ""
        say(f"  · {nm!r}  id={d.id[:70]}")
        if target is None and ("remote" in nm.lower() or "chromecast" in nm.lower()):
            target = d
    if target is None:
        say("\n❌ 没找到名字像遥控器的已配对 BLE 设备。")
        _save(L)
        return

    say(f"\n→ 目标：{target.name!r}")

    try:
        ble = await BluetoothLEDevice.from_id_async(target.id)
    except OSError as e:
        say(f"❌ from_id_async 失败：{e}")
        ble = None
    if ble is None:
        say("⚠ from_id_async 返回 None —— 配对记录可能已失效（见 --fix）。")
        _save(L)
        return

    if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
        say("⚠ 当前未连接，等遥控器醒来（按任意键）最多 8 秒…")
        for _ in range(16):
            await asyncio.sleep(0.5)
            if ble.connection_status == BluetoothConnectionStatus.CONNECTED:
                break
    say(f"连接状态：{ble.connection_status}")

    # ── 问 1：全部服务 ────────────────────────────────────────────────
    say("\n" + "─" * 74)
    say("【问 1】遥控器暴露的全部 GATT 服务")
    say("─" * 74)
    res = await ble.get_gatt_services_async()
    if res.status != GattCommunicationStatus.SUCCESS:
        say(f"❌ 枚举服务失败 status={res.status}")
        _save(L)
        return

    hid_service = None
    all_report_chars = []
    for svc in res.services:
        su = str(svc.uuid)
        note = who(su)
        say(f"\n  ▸ {su}  {('← ' + note) if note else '（未知/私有）'}")
        cr = await svc.get_characteristics_async()
        if cr.status != GattCommunicationStatus.SUCCESS:
            say(f"      ⚠ 枚举特征失败 status={cr.status}")
            continue
        for ch in cr.characteristics:
            cu = str(ch.uuid)
            pv = int(getattr(ch.characteristic_properties, "value",
                             ch.characteristic_properties))
            cn = who(cu)
            say(f"      - {cu}  [{props_str(pv)}]{('  ' + cn) if cn else ''}")
            if cu.lower().startswith("00002a4d"):
                all_report_chars.append(ch)
        if su.lower().startswith("00001812"):
            hid_service = svc
            hid_handles = cr.characteristics

    if hid_service is None:
        say("\n❌ 没有 HID 服务 0x1812 —— 遥控器的 HID 服务在当前连接上不可见。")
        say("   （Windows 侧的 5 路集合若还在，那是**上一次配对**遗留的旧账本）")
        _save(L)
        return

    # ── 问 2：每个 Report 特征的 CCCD ─────────────────────────────────
    say("\n" + "─" * 74)
    say("【问 2】每个 HID Report 特征的 CCCD —— 谁订阅了？")
    say("─" * 74)
    say("  ⚠ 这是最关键的一问：CCCD=0x0000 表示**没有任何人订阅**，")
    say("     那个报告 ID 的按键永远不会被推上来 —— Windows 的 HID 栈也读不到。")
    say("")
    cccd_rows = []
    for i, ch in enumerate(all_report_chars):
        pv = int(getattr(ch.characteristic_properties, "value",
                         ch.characteristic_properties))
        line = f"  Report#{i:<2} [{(props_str(pv)):<22}] "
        # 报告 ID：Report 特征如果带 report reference 描述符，才有；先试着读值
        val = None
        if pv & 0x02:                                   # READ
            try:
                vr = await ch.read_value_async()
                if vr.status == GattCommunicationStatus.SUCCESS and vr.value:
                    val = bytes(vr.value)
            except Exception:                           # noqa: BLE001
                pass
        cccd = None
        try:
            descs = await ch.get_descriptors_async()
            for d in descs.descriptors:
                if str(d.uuid).lower().startswith("00002902"):
                    dr = await d.read_value_async()
                    if dr.status == GattCommunicationStatus.SUCCESS and dr.value:
                        cccd = bytes(dr.value)
        except Exception:                               # noqa: BLE001
            pass

        def cccd_txt(c):
            if c is None:
                return "读不到（无 CCCD 或权限不足）"
            # ⚠ 小端：Windows 写的是 little-endian 的两个字节
            v = int.from_bytes(c, "little")
            if v == 0:
                return f"{c.hex(' ')}  → ⛔ **无人订阅**（报告不会推上来）"
            if v & 0x0003:
                return f"{c.hex(' ')}  → ✅ 已订阅 {'NOTIFY' if v & 1 else ''}{'INDICATE' if v & 2 else ''}"
            return f"{c.hex(' ')}  → ❓ 未知值"

        line += f"cccd={cccd_txt(cccd)}"
        if val:
            line += f"   读值={val.hex(' ')}"
        say(line)
        cccd_rows.append((i, cccd))

    n_sub = len([1 for _, c in cccd_rows if c and int.from_bytes(c, "little")])
    say("")
    say(f"  → {len(all_report_chars)} 个 Report 特征，其中 **{n_sub} 个已被订阅**。")
    if n_sub == 0:
        say("  → 🎯 实锤：一个都没订阅。这不是「遥控器不发」，是「没人听」。")
        say("     修法：本项目自己在 ATVV 连接上**顺手订阅 HID 服务的那几个 Report 特征**，")
        say("     就不必再依赖 Windows 的 HID 集合了。")

    # ── 问 3：Report Map ──────────────────────────────────────────────
    say("\n" + "─" * 74)
    say("【问 3】HID Report Map（0x2A4B）—— 设备自报的描述符")
    say("─" * 74)
    for ch in hid_handles:
        if not str(ch.uuid).lower().startswith("00002a4b"):
            continue
        vr = await ch.read_value_async()
        if vr.status != GattCommunicationStatus.SUCCESS or not vr.value:
            say(f"  ❌ 读不到 Report Map（status={vr.status}）")
            continue
        raw = bytes(vr.value)
        say(f"  ✅ 读到 {len(raw)} 字节：")
        say("")
        dump = _dump_descriptor(raw)
        for ln in dump:
            say("    " + ln)
        # 存原始字节，方便别人复核
        try:
            (CONFIG_DIR / "hid-report-map.bin").write_bytes(raw)
            say(f"\n    原始字节已存：{CONFIG_DIR / 'hid-report-map.bin'}")
        except Exception:                               # noqa: BLE001
            pass

    # ── 可选：订阅并实时听 ────────────────────────────────────────────
    if listen:
        say("\n" + "─" * 74)
        say(f"【实时】订阅全部 Report 特征，听 {listen} 秒 —— 请现在按遥控器的键")
        say("─" * 74)
        hits: list[tuple[float, int, bytes]] = []
        tokens = []
        loop = asyncio.get_running_loop()

        for i, ch in enumerate(all_report_chars):
            idx = i

            def mk(idx):
                def cb(sender, args):
                    try:
                        data = bytes(args.characteristic_value)
                    except Exception:                   # noqa: BLE001
                        data = b""
                    hits.append((loop.time(), idx, data))
                    print(f"    📦 Report#{idx}  {data.hex(' ')}")
                return cb

            try:
                tokens.append((ch, ch.add_value_changed(mk(idx))))
                await ch.write_client_characteristic_configuration_descriptor_async(
                    GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
                say(f"  ✅ 已订阅 Report#{idx}")
            except Exception as e:                      # noqa: BLE001
                say(f"  ❌ 订阅 Report#{idx} 失败：{e.__class__.__name__}: {e}")

        t0 = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - t0 < listen:
            await asyncio.sleep(0.25)
        say(f"\n  共收到 {len(hits)} 条报告")
        if not hits:
            say("  → ⚠ 订阅成功了但一条都没来：遥控器的按键**确实不走 HID**。")
        for ch, tok in tokens:
            try:
                ch.remove_value_changed(tok)
            except Exception:                           # noqa: BLE001
                pass

    _save(L)


def _dump_descriptor(raw: bytes) -> list[str]:
    """把 HID 报告描述符按条目拆开，标出 Report ID / 用法页 / 报告长度。

    只做"够看懂"的程度，不追求完整实现 HID 描述符解析。
    """
    out: list[str] = []
    i = 0
    cur_page = 0
    cur_rid = None
    in_bits = 0
    usage_stack: list[tuple[int, int]] = []
    while i < len(raw):
        b = raw[i]
        if b == 0xFE:                                   # long item
            size = raw[i + 1] if i + 1 < len(raw) else 0
            tag = raw[i + 2] if i + 2 < len(raw) else 0
            data = raw[i + 3:i + 3 + size]
            out.append(f"{i:04X}: LONG tag=0x{tag:02X} {data.hex(' ')}")
            i += 3 + size
            continue
        size = {0: 0, 1: 1, 2: 2, 3: 4}[b & 0x03]
        typ = (b >> 2) & 0x03
        tag = (b >> 4) & 0x0F
        data = raw[i + 1:i + 1 + size]
        val = int.from_bytes(data, "little") if data else 0
        desc = ""
        if typ == 0x01 and tag == 0x0:                  # Usage Page
            cur_page = val
            desc = f"UsagePage  0x{val:04X}"
        elif typ == 0x02 and tag == 0x0:                # Usage (local)
            usage_stack.append((cur_page, val))
            desc = f"Usage      0x{cur_page:04X}/0x{val:04X}"
        elif typ == 0x01 and tag == 0x8:                # Report ID
            cur_rid = val
            desc = f"** ReportID = {val} **"
        elif typ == 0x01 and tag == 0x9:                # Report Count
            desc = f"ReportCount {val}"
        elif typ == 0x01 and tag == 0x7:                # Report Size
            desc = f"ReportSize  {val} (bits)"
        elif typ == 0x00 and tag == 0x8:                # Input
            in_bits = 0
            desc = f"◆ Input      (bitfield 0x{val:02X})"
        elif typ == 0x00 and tag == 0x9:                # Output
            desc = f"◆ Output     (bitfield 0x{val:02X})"
        elif typ == 0x00 and tag == 0xB:                # Feature
            desc = f"◆ Feature    (bitfield 0x{val:02X})"
        elif typ == 0x01 and tag == 0xA:                # Push
            desc = "Push"
        elif typ == 0x01 and tag == 0xB:                # Pop
            desc = "Pop"
        elif typ == 0x01 and tag == 0x2:                # Logical Minimum
            desc = f"LogicalMin {val}"
        elif typ == 0x01 and tag == 0x1:                # Logical Maximum
            desc = f"LogicalMax {val}"
        elif typ == 0x01 and tag == 0x6:                # Usage Minimum
            desc = f"UsageMin   0x{cur_page:04X}/0x{val:04X}"
        elif typ == 0x01 and tag == 0x5:                # Usage Maximum
            desc = f"UsageMax   0x{cur_page:04X}/0x{val:04X}"
        elif typ == 0x01 and tag == 0x4:                # Physical Maximum
            desc = f"PhysicalMax {val}"
        elif typ == 0x01 and tag == 0x3:                # Physical Minimum
            desc = f"PhysicalMin {val}"
        else:
            desc = f"(typ={typ} tag={tag} val=0x{val:X})"
        out.append(f"{i:04X}: {b:02X}{data.hex(' '):<9} {desc}")
        i += 1 + size
    return out


def _save(L: list[str]) -> None:
    text = "\n".join(L) + "\n"
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text, encoding="utf-8")
        print(f"\n报告已写出：{OUT}")
    except Exception as e:                              # noqa: BLE001
        print(f"\n⚠ 写报告失败：{e}")


if __name__ == "__main__":
    sys.exit(main())
