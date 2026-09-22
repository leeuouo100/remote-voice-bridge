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
    # 默认 90 秒：测 "遥控器按键有没有到 Windows" 要按 11 个键，
    # 45 秒太赶（一边读说明一边按），而窗口不够长时读到的 "0 条"
    # 会被当成"按键没来"—— 那是**测量误差**冒充结论（09-22 就误读过一次）。
    seconds = 90
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
    subs: list[str] = []
    subs_ok: list[str] = []
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
            say("   带着桥程序跑（--force）时，蓝牙连接常被它占着；"
                "退出桥程序后重跑通常就好。")
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
                        # 走 say（进报告文件），别用 print：不然报告里只有计数、
                        # 没有原始字节，事后没法二次判读。
                        say(f"  🔵 [{time.time() - _t_start:7.2f}s] "
                            f"{tag}  {data.hex(' ')}{note}")
                    return cb

                try:
                    tok = ch.add_value_changed(mk(key, cu, su))
                    await ch.write_client_characteristic_configuration_descriptor_async(
                        GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
                    subs.append((key, ch, tok))
                    subs_ok.append(key)
                    say(f"     ✅ 已订阅 {key}")
                except Exception as e:              # noqa: BLE001
                    say(f"     ❌ 订阅 {key} 失败：{e.__class__.__name__}: {e}")

        # ── 实时监听 ──────────────────────────────────────────────
        say("\n" + "=" * 78)
        say(f" 现在开始 {seconds} 秒实时监听 —— 请**依次按遥控器的每个键**：")
        say("   ① 先按这三个（当前最要紧）：确认 OK → 返回 ← → 主页 ⌂")
        say("   ② 再按：方向上下左右 → 音量＋ → 音量－ → 静音")
        say("   （语音键走 ATVV，这里会看到 op 0x04；其它键看落在哪条通道）")
        say("=" * 78)
        # ⚠ 倒计时不是装饰：这一段以前是"打印完立刻开始计时"，
        #   用户还在读说明、手还没伸到遥控器上，窗口已经烧掉一截，
        #   最后看到"0 条"还以为按键没来 —— 其实是**没来得及按**。
        #   这也是本工具上最容易被误读成"结论"的地方（09-22 那次就是这么读的）。
        for n in (3, 2, 1):
            say(f"   ⏳ {n} 秒后开始，把手放到遥控器上…")
            await asyncio.sleep(1.0)
        say("   🟢 开始！现在按（下面这行会实时跳秒，跳满就是结束）")
        t0 = time.time()
        last_tick = -1
        while time.time() - t0 < seconds and not stop.is_set():
            await asyncio.sleep(0.2)
            el = time.time() - t0
            tick = int(el)
            # 每秒刷**一次**（原来是 int(el) % 5 == 0 → 同一秒里刷 5 遍，白屏）
            if tick != last_tick:
                last_tick = tick
                tot = sum(len(v) for v in live_hits.values())
                print(f"  ⏱ 还剩 {seconds - tick:3d}s ｜ 已收到 {tot} 条  "
                      f"（键盘事件会直接打印在下面）", end="\r", flush=True)
        say("")
        for key, ch, tok in subs:
            try:
                ch.remove_value_changed(tok)
            except Exception:                           # noqa: BLE001
                pass

    # HID 集合的原始报告读取（后台线程，和 GATT 并行）
    _t_start = time.time()

    def _on_watch_event(item) -> None:
        # ⚠ 这里以前**只 print、不进报告文件** —— 于是报告里只剩一个干巴巴的
        #   "33"，看不到键名，谁也没法判断那 33 个到底是什么键（09-19 就是这样）。
        ts, _kind, name, scan, injected = item
        tag = {True: "注入", False: "真实", None: "分不出"}[injected]
        say(f"  ⌨ [{ts - _t_start:7.2f}s] 键盘事件[{tag}] "
            f"name={name!r} scan={scan}")

    w = hidwatch.ReportWatcher(only_google=True, on_event=_on_watch_event)
    n_open = w.start(with_hooks=True)
    _hook_kind = ("低级钩子（能分「注入/真实」）" if w._kb_hook is not None
                  else "退路钩子（只有键名，分不出注入）" if w._hooks
                  else "⚠ 没挂上")
    say(f"\n【静态】HID 集合打开 {n_open} 路，键盘钩子：{_hook_kind}")
    if w.hook_error:
        say(f"  ⚠ {w.hook_error}")

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
    real = [e for e in w.key_events if e[4] is False]
    inj = [e for e in w.key_events if e[4] is True]
    unk = [e for e in w.key_events if e[4] is None]
    say(f"  {'通道':<46}{'次数':>6}")
    say("  " + "-" * 60)
    say(f"  {'⌨ 键盘钩子 · 真实（非注入）':<44}{len(real):>6}   ← 只有这行能作证")
    say(f"  {'⌨ 键盘钩子 · 注入（本程序自己发的）':<44}{len(inj):>6}")
    if unk:
        say(f"  {'⌨ 键盘钩子 · 分不出是否注入':<44}{len(unk):>6}")
    for c in w.collections:
        say(f"  {'📦 HID ' + c.key:<44}{len(c.reports):>6}")
    # ⚠ 用 set(subs_ok) | set(live_hits)：**订上了但 0 条**的通道也必须进表。
    #   只列 live_hits 的话，报告里看不到 ae42 / d343bfc5 这两路到底听了没有 ——
    #   09-19 那份报告就是这个形态，事后谁也判断不出「私有服务是 0」还是「没测」。
    for k in sorted(set(subs_ok) | set(live_hits)):
        n_k = len(live_hits.get(k, []))
        mark = "" if n_k else "（已订阅，0 条）"
        say(f"  {'🔵 ' + k + mark:<44}{n_k:>6}")
    if w.hook_error:
        say(f"  （键盘钩子备注：{w.hook_error}）")

    say("")
    # ⚠ 判读**只此一处**（`hidwatch.ReportWatcher.summary_lines`），这里不再自己
    #   抄一份 —— 抄一份的下场就是两边慢慢跑偏。09-19 那份报告里 HID 五路全 0，
    #   结论却写着"按键能到 Windows"：就因为判读用的是 `if kb_n:` 这个**恒真**
    #   条件，把本程序自己注入的语音热键当成了遥控器的按键。
    _sl = w.summary_lines()
    try:
        _i = next(n for n, ln in enumerate(_sl) if ln.startswith("【判读】"))
    except StopIteration:                                   # noqa: BLE001
        _i = 0
    for ln in _sl[_i:]:
        say(ln)

    if not subs_ok:
        say("")
        say("  ⚠ 这一轮**一个 GATT 特征都没订阅上**（原因见上面）——")
        say("     私有服务 ae42 / d343bfc5 等于**没测**，")
        say("     不能当成「它们也是 0」。带着桥程序跑就属于这种情形。")

    # 私有服务的光是 hidwatch 看不到的（它只管 HID 集合），单独补一段。
    priv = sorted(k for k in live_hits
                  if k.startswith("0000ae40") or k.startswith("d343bfc0"))
    if priv:
        say("")
        say("  → 🎯 **私有服务**上有流量（不是 HID、也不是 ATVV）：")
        for k in priv:
            d = live_hits[k]
            say(f"     · {k} 收到 {len(d)} 条，样本 {d[0][1].hex(' ')}")
        say("     按键很可能走这里。修法：本项目订阅这些私有特征、")
        say("     按它们自己的格式解出按键 —— 改映射表永远碰不到它。")

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
