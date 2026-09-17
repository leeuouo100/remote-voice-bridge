"""
一次按键，看它落在哪条通道 —— 把**所有可能的路**同时挂上。

## 为什么要做这个（前面查出来的事实）

已经确认的事（见 tools/probe_gatt_hid.py / probe_hid_claim.py / dump_report_descriptor.py）：

  · 遥控器有 5 路 HID 集合（键盘/消费类/鼠标/厂商页 0xFF01/厂商页 0xFF80），
    都在线（DIGCF_PRESENT），厂商页**打得开**，但按遍所有键**一条报告都不来**。
  · 遥控器的 HID 服务 0x1812 被**别人独占**着：`get_characteristics_async`
    一律返回 status=3 (ACCESS_DENIED)，连按 UUID 单独取、from_id_async 重开
    都被拒。占它的不是本程序（本程序只开 ATVV），所以只能是 **Windows 的 HOGP 栈**。
  · 语音键**能用**：走 ATVV CTL 的 0x04 audio_start，每次都到。

于是"按键没反应"只剩两种可能，而它们在 HID 层长得一模一样：

  ① 遥控器把这些按键发在 HID 上，但 Windows 的 HOGP **没把报告送上来**
     （挂起 / 订阅没建立 / 驱动没绑）
  ② 遥控器**根本不往 HID 发**，而是发在别的通道上
     —— 它身上还挂着两个**私有服务**：
        0000ae40  →  ae41 [write-noresp] / ae42 [notify]
        d343bfc0  →  c1..c4 [write] / c5 [notify]      ← 后缀和 ATVV 同厂
     这两个服务**没有**被独占，我们订得上。

## 本脚本要做的事

按一次键，同时记录下面 5 条通道有没有动静：

  ⌨ 键盘钩子（Windows 键盘事件）
  📦 HID 5 路集合的原始报告（含两个厂商页）
  🔵 ae42          私有通知
  🔵 d343bfc5      私有通知
  🟢 ATVV CTL 的**每一个字节**（含认不出的 opcode）

哪一条亮，就说明按键走哪条 —— 这是唯一能把 ①② 分开的办法。

## 用法

    # 1) 先在托盘右键「退出」关掉桥程序（它占着 ATVV，不关就听不到 CTL）
    # 2) 然后：
    python tools/watch_all_channels.py --seconds 45

    关不掉 / 就想带着它测：加 --force（CTL 那条会显示"被占用，跳过"）

不按任何键也能跑 —— 静态部分照样出（服务清单、HID 集合、描述符尝试）。

产出：屏幕实时打印 + %APPDATA%\\remote-voice-bridge\\watch-all-channels.txt
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

import hidinfo      # noqa: E402
import hidwatch     # noqa: E402
from config import APP_VERSION, CONFIG_DIR  # noqa: E402

OUT = CONFIG_DIR / "watch-all-channels.txt"

ATVV_CTL = "ab5e0004-5a21-4f05-bc7d-af01f617b664"
ATVV_AUD = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
SKIP_SERVICES = {
    # 音频流特征：订阅了会灌进来海量字节，淹掉按键证据
    ATVV_AUD,
}
HOGP = "00001812-0000-1000-8000-00805f9b34fb"

# ATVV CTL 上已知的 opcode（用来把"认不出的"标出来）
ATVV_OPS = {
    0x0B: "capabilities", 0x04: "audio_start", 0x00: "audio_stop",
    0x0C: "mic_open_result", 0x08: "start_search", 0x0A: "audio_sync",
}


def bridge_running() -> bool:
    try:
        p = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RemoteVoiceBridge.exe", "/NH"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000)
        return "RemoteVoiceBridge.exe" in (p.stdout or "")
    except Exception:                                   # noqa: BLE001
        return False


def main() -> int:
    seconds = 45
    a = sys.argv[1:]
    for i, x in enumerate(a):
        if x == "--seconds" and i + 1 < len(a):
            try:
                seconds = max(5, min(600, int(a[i + 1])))
            except ValueError:
                pass
    force = "--force" in a

    lines: list[str] = []
    lock = threading.Lock()

    def say(s: str = "") -> None:
        with lock:
            print(s, flush=True)
            lines.append(s)

    say("=" * 78)
    say(" remote-voice-bridge · 全通道监听（一次按键，看它走哪条路）")
    say(f" 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  版本：v{APP_VERSION}")
    say("=" * 78)

    if bridge_running() and not force:
        say("\n⛔ 桥程序（RemoteVoiceBridge.exe）在跑，它占着 ATVV，")
        say("   会让我们听不到 CTL 通道。请托盘右键「退出」后重跑；")
        say("   确实要带着它测就加 --force。")
        return 3

    # ── 静态：HID 集合 + 尝试原始描述符 ─────────────────────────────
    say("\n" + "─" * 78)
    say("【静态】遥控器的 HID 集合")
    say("─" * 78)
    cols = [d for d in hidinfo.live_hid_collections() if d.get("is_google")]
    for d in cols:
        say(f"  0x{d['usage_page']:04X}/0x{d['usage']:04X}  in_len={d['in_len']:<3} "
            f"{hidinfo.collection_role(d['usage_page'], d['usage'])[0]}")
    if not cols:
        say("  ⚠ 一个都没枚举到 —— 遥控器可能没连上。")

    # ── 静态：GATT 服务 + HID 服务能否订阅 ──────────────────────────
    static_report: list[str] = []
    live_hits: dict[str, list] = {}
    stop = threading.Event()

    async def gatt_part() -> None:
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import (
            BluetoothLEDevice, BluetoothConnectionStatus)
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus, GattClientCharacteristicConfigurationDescriptorValue)

        sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        devs = await DeviceInformation.find_all_async_aqs_filter(sel)
        tgt = None
        for d in devs:
            n = (d.name or "").lower()
            if "remote" in n or "chromecast" in n:
                tgt = d
                break
        if tgt is None:
            say("\n❌ 没找到已配对的遥控器（BLE）")
            return
        try:
            ble = await BluetoothLEDevice.from_id_async(tgt.id)
        except OSError as e:
            say(f"\n❌ 连接失败：{e}")
            return
        if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
            say("\n⚠ BLE 未连接，等遥控器醒来（按任意键）最多 10 秒…")
            for _ in range(20):
                await asyncio.sleep(0.5)
                if ble.connection_status == BluetoothConnectionStatus.CONNECTED:
                    break
        say(f"\n{'─' * 78}")
        say(f"【静态】GATT 服务（连接状态={int(ble.connection_status.value)}）")
        say("─" * 78)
        res = await ble.get_gatt_services_async()

        subs: list[tuple[str, object, object]] = []
        loop = asyncio.get_running_loop()

        for svc in res.services:
            su = str(svc.uuid).lower()
            cr = await svc.get_characteristics_async()
            if cr.status != GattCommunicationStatus.SUCCESS:
                why = "ACCESS_DENIED（被别人独占）" if int(cr.status.value) == 3 else str(cr.status)
                say(f"  ▸ {su}  ⚠ 特征枚举被拒：{why}")
                if su == HOGP:
                    say("     └ 这就是 Windows 的 HOGP 栈。它占着服务，")
                    say("       却一条输入报告都没送到 HID 集合上（下面实测）。")
                continue
            notify_chars = []
            for ch in cr.characteristics:
                v = int(getattr(ch.characteristic_properties, "value",
                                ch.characteristic_properties))
                if v & 0x10:
                    notify_chars.append(str(ch.uuid).lower())
            say(f"  ▸ {su}  特征 {len(cr.characteristics)} 个，"
                f"其中可 notify {len(notify_chars)} 个")
            for ch in cr.characteristics:
                if su in SKIP_SERVICES:
                    continue
                v = int(getattr(ch.characteristic_properties, "value",
                                ch.characteristic_properties))
                if not (v & 0x10):
                    continue
                cu = str(ch.uuid).lower()
                key = f"{su[:8]}/{cu[:8]}"

                def mk(key, cu, su):
                    def cb(sender, args):
                        try:
                            data = bytes(args.characteristic_value)
                        except Exception:               # noqa: BLE001
                            data = b""
                        tag = key
                        if cu.startswith("00002a4d"):
                            tag = "HID Report 0x2A4D"
                        live_hits.setdefault(tag, []).append((time.time(), data))
                        note = ""
                        if cu == ATVV_CTL and data:
                            op = data[0]
                            note = f"  ← op 0x{op:02X} {ATVV_OPS.get(op, '【认不出】')}"
                        print(f"  🔵 {tag}  {data.hex(' ')}{note}", flush=True)
                    return cb

                try:
                    tok = ch.add_value_changed(mk(key, cu, su))
                    await ch.write_client_characteristic_configuration_descriptor_async(
                        GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
                    subs.append((key, ch, tok))
                    say(f"     ✅ 已订阅 {key}")
                except Exception as e:              # noqa: BLE001
                    say(f"     ❌ 订阅 {key} 失败：{e.__class__.__name__}: {e}")

        # ── 实时监听 ──────────────────────────────────────────────
        say("\n" + "=" * 78)
        say(f" 现在开始 {seconds} 秒实时监听 —— 请**依次按遥控器的每个键**：")
        say("   方向上下左右 → 确认 → 返回 → 主页 → 音量＋ → 音量－ → 静音")
        say("   （语音键走 ATVV，这里会看到 op 0x04；其它键看落在哪条通道）")
        say("=" * 78)
        t0 = time.time()
        while time.time() - t0 < seconds and not stop.is_set():
            await asyncio.sleep(0.25)
            el = time.time() - t0
            if int(el) % 5 == 0:
                tot = sum(len(v) for v in live_hits.values())
                print(f"  … {el:4.1f}s / {seconds}s，共 {tot} 条", end="\r", flush=True)
        say("")
        for key, ch, tok in subs:
            try:
                ch.remove_value_changed(tok)
            except Exception:                           # noqa: BLE001
                pass

    # HID 集合的原始报告读取（后台线程，和 GATT 并行）
    w = hidwatch.ReportWatcher(only_google=True)
    n_open = w.start(with_hooks=True)
    say(f"\n【静态】HID 集合打开 {n_open} 路，"
        f"键盘钩子{'已挂' if w._hooks else '没挂上'}")

    def run_gatt():
        try:
            asyncio.run(gatt_part())
        except Exception as e:                          # noqa: BLE001
            say(f"\n❌ GATT 部分异常：{e.__class__.__name__}: {e}")

    th = threading.Thread(target=run_gatt, daemon=True, name="gatt")
    th.start()
    th.join(timeout=seconds + 25)
    stop.set()
    time.sleep(0.5)
    w.stop()

    # ── 汇总 ─────────────────────────────────────────────────────────
    say("\n" + "=" * 78)
    say("【结论】各通道收到多少次")
    say("=" * 78)
    kb_n = len([1 for e in w.key_events if e[1] == "keyboard"])
    say(f"  {'通道':<46}{'次数':>6}")
    say("  " + "-" * 60)
    say(f"  {'⌨ 键盘钩子（Windows 键盘事件）':<44}{kb_n:>6}")
    for c in w.collections:
        say(f"  {'📦 HID ' + c.key:<44}{len(c.reports):>6}")
    for k in sorted(live_hits):
        say(f"  {'🔵 ' + k:<44}{len(live_hits[k]):>6}")

    say("")
    say("【判读】")
    hid_n = sum(len(c.reports) for c in w.collections)
    gatt_n = sum(len(v) for v in live_hits.values())
    if kb_n:
        say("  → 键盘事件收到了：按键**能**到 Windows，问题在我们这层的映射/注入。")
    elif hid_n:
        say("  → 🎯 按键报告出现在 HID 集合上：Windows 的 HOGP 能送报告，")
        say("     之前 0 条是**读法/时机**的问题（对不对、够不够早）。")
        for c in w.collections:
            if c.reports:
                say(f"     · {c.key} 样本 {c.reports[0][1].hex(' ')}")
    elif any(k.startswith(("0000ae40", "d343bfc0")) for k in live_hits):
        say("  → 🎯 按键报告落在**私有服务**上（不是 HID、也不是 ATVV）！")
        say("     这就是「改映射表永远没用」的真正原因。")
        say("     修法：本项目订阅这些私有特征，按它们自己的格式解按键。")
    elif gatt_n:
        say("  → 只有 ATVV/其它服务有流量，HID 集合仍然 0 条。")
        say("     说明遥控器的按键**确实不走 HID**（或 Windows 的 HOGP 彻底不工作），")
        say("     下一步要按上面亮起来的那个通道去解。")
    else:
        say("  → ⚠ 一条都没收到。请确认：遥控器已连接、刚才**确实按了键**、")
        say("     并且桥程序已退出（否则 ATVV 被它占着）。")

    text = "\n".join(lines) + "\n"
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        Path(OUT).write_text(text, encoding="utf-8")
        print(f"\n报告已写出：{OUT}")
    except Exception as e:                              # noqa: BLE001
        print(f"\n⚠ 写报告失败：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
