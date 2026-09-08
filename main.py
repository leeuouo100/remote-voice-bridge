"""
remote-voice-bridge — Windows ATVV bridge for Bluetooth voice remotes.
融合 vRemoter (完整 ATVV 协议栈 + 会话协调) 和 leufon/remote-voice-bridge
(VB-CABLE 音频管道 + 按键触发重连) 的技术。
"""

from __future__ import annotations
import asyncio
import logging
import os
import queue
import sys
import time
import uuid
from collections import deque
from typing import Optional

from config import DEVICES, Config, find_device_by_name
import state
from atvv import (
    ATVVProtocol, SERVICE_UUID, TX_UUID, AUDIO_UUID, CTL_UUID,
    GET_CAPS_CMD, _OP_AUDIO_START, _OP_AUDIO_STOP, _OP_MIC_OPEN_R,
)
from adpcm import IMAADPCMDecoder
from session import SessionCoordinator, Phase
from keys import trigger_voice_hotkey
from buttons import resolve_button

# ── Logging ────────────────────────────────────────────────────────────────────
# 必须写到用户目录而不是程序目录：打包成 exe 后程序目录是 PyInstaller 的
# 临时解压路径（_MEIxxxx），退出即被清理，日志会全部丢失。
from config import CONFIG_DIR
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
log_file = str(CONFIG_DIR / "bridge.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("rvb")


# ── Audio queue ────────────────────────────────────────────────────────────────
_sample_queue = queue.Queue(maxsize=65536)
_pending_samples = deque()
_GAIN = 10.0


def _find_cable(device_name: str = "CABLE Input"):
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if device_name.lower() in d["name"].lower() and d["max_output_channels"] > 0:
            return i
    return None


# ── Device discovery ───────────────────────────────────────────────────────────
async def list_devices():
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
    devices = await DeviceInformation.find_all_async_aqs_filter(selector)
    result = []
    for d in devices:
        sig = find_device_by_name(d.name or "")
        result.append({"name": d.name or "(unnamed)", "id": d.id, "sig": sig})
    return result


async def find_remote(device_type: str | None = None, name_hint: str | None = None):
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.bluetooth import BluetoothLEDevice

    selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
    devices = await DeviceInformation.find_all_async_aqs_filter(selector)

    # Try VID/PID first
    if device_type and device_type in DEVICES:
        sig = DEVICES[device_type]
        for d in devices:
            pnp = getattr(d, "pnp_id", "") or ""
            if sig.vid and sig.pid:
                vid_str = f"VID_{sig.vid:04X}&PID_{sig.pid:04X}"
                if vid_str in pnp:
                    return d, sig

    # Fallback to name pattern
    pattern = name_hint
    for d in devices:
        n = (d.name or "").lower()
        if pattern and pattern in n:
            return d, find_device_by_name(d.name or "")
        # Auto-detect common patterns
        if any(k in n for k in ["google", "chromecast", "x6", "xiaomi"]):
            return d, find_device_by_name(d.name or "")

    if not devices:
        devices = await DeviceInformation.find_all_async()
        for d in devices:
            n = (d.name or "").lower()
            if any(k in n for k in ["google", "chromecast", "remote", "x6"]):
                return d, find_device_by_name(d.name or "")

    return None, None


# ── Audio stream ───────────────────────────────────────────────────────────────
def _create_stream(out_dev: int, sample_rate: int):
    import sounddevice as sd

    def cb(outdata, frames, timeinfo, status):
        if status:
            logger.debug(f"Audio status: {status}")
        for i in range(frames):
            if _pending_samples:
                val = int(_pending_samples.popleft()) * _GAIN
                outdata[i, 0] = max(-32768, min(32767, val))
            else:
                try:
                    batch = _sample_queue.get_nowait()
                    _pending_samples.extend(batch)
                    val = int(_pending_samples.popleft()) * _GAIN
                    outdata[i, 0] = max(-32768, min(32767, val))
                except queue.Empty:
                    outdata[i, 0] = 0

    return sd.OutputStream(
        device=out_dev, channels=1, dtype="int16",
        samplerate=sample_rate, blocksize=240, callback=cb,
    )


# ── Main bridge ────────────────────────────────────────────────────────────────
async def run_bridge(device_type: str | None = None, name_hint: str | None = None):
    cfg = Config.load()
    if device_type:
        cfg.device = device_type

    sig = DEVICES.get(cfg.device)

    logger.info("=" * 60)
    logger.info("remote-voice-bridge starting")
    logger.info(f"  Device : {cfg.device}  ({sig.vid:04X}:{sig.pid:04X})" if sig else f"  Device : {cfg.device}")
    logger.info(f"  IM     : {cfg.input_method}  audio: {cfg.audio_output}  gain: {_GAIN}x")
    logger.info(f"  Watchdog: {cfg.watchdog_timeout}s  Reconnect: {cfg.reconnect_delay}s")
    logger.info("=" * 60)

    # ── Discover ──
    logger.info("🔍 Searching for remote...")
    dev_info, matched_sig = await find_remote(device_type=cfg.device, name_hint=name_hint or (sig.name_patterns[0] if sig and sig.name_patterns else None))
    if dev_info is None:
        logger.error("❌ Remote not found. Pair it in Windows Settings → Bluetooth first.")
        return False
    logger.info(f"✅ Found: {dev_info.name}  ID: {dev_info.id[:50]}...")

    # ── Connect BLE ──
    from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattCommunicationStatus, GattClientCharacteristicConfigurationDescriptorValue,
    )
    from winrt.windows.storage.streams import DataWriter

    logger.info("🔗 Connecting...")
    ble = await BluetoothLEDevice.from_id_async(dev_info.id)
    if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
        logger.warning("⚠️  Not connected yet — remote may be sleeping. Press any button to wake.")
        for _ in range(10):
            await asyncio.sleep(0.5)
            if ble.connection_status == BluetoothConnectionStatus.CONNECTED:
                break
        if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
            logger.error("❌ Connection failed")
            return False
    logger.info(f"✅ Connected  status={ble.connection_status}")
    state.update(connected=True, device=dev_info.name or "", last_event="已连接")

    # ── GATT characteristics ──
    logger.info("📡 Discovering ATVV service...")
    svc = await ble.get_gatt_services_for_uuid_async(uuid.UUID(SERVICE_UUID))
    if svc.status != GattCommunicationStatus.SUCCESS or not svc.services:
        logger.error("❌ ATVV service not found")
        return False
    service = svc.services[0]

    tx_r   = await service.get_characteristics_for_uuid_async(uuid.UUID(TX_UUID))
    aud_r  = await service.get_characteristics_for_uuid_async(uuid.UUID(AUDIO_UUID))
    ctl_r  = await service.get_characteristics_for_uuid_async(uuid.UUID(CTL_UUID))

    if not all(r.status == GattCommunicationStatus.SUCCESS and r.characteristics
               for r in [tx_r, aud_r, ctl_r]):
        logger.error("❌ ATVV characteristic(s) not found")
        return False

    tx_char   = tx_r.characteristics[0]
    audio_char= aud_r.characteristics[0]
    ctl_char  = ctl_r.characteristics[0]
    logger.info("✅ ATVV characteristics ready")

    # ── Protocol + Session ──
    atvv = ATVVProtocol()

    def _send_tx(cmd: bytes, tag: str = "TX") -> None:
        """Write raw bytes to the ATVV TX characteristic (fire-and-forget)."""
        if not cmd:
            return

        async def _write():
            try:
                w = DataWriter()
                w.write_bytes(cmd)
                r = await tx_char.write_value_with_result_async(w.detach_buffer())
                logger.info(f"📤 {tag} [{cmd.hex(' ')}] status={r.status}")
            except Exception as e:
                logger.error(f"{tag} write failed: {e}")

        try:
            asyncio.get_event_loop().create_task(_write())
        except Exception as e:
            logger.error(f"{tag} schedule failed: {e}")

    def _on_mic_open(sid: int):
        """Host 主动开麦 — 必须真正写入 BLE，否则遥控器不会推流。"""
        try:
            cmd = atvv.mic_open_cmd()
        except Exception as e:
            logger.error(f"mic_open_cmd failed: {e}")
            return None
        _send_tx(cmd, "MIC_OPEN")
        return cmd

    def _on_mic_close(sid: int):
        try:
            cmd = atvv.mic_close_cmd(sid)
        except Exception as e:
            logger.error(f"mic_close_cmd failed: {e}")
            return None
        _send_tx(cmd, "MIC_CLOSE")
        return cmd

    session = SessionCoordinator(
        on_mic_open=_on_mic_open,
        on_mic_close=_on_mic_close,
        on_phase=lambda phase, reason: logger.debug(f"Phase: {phase.value}  ({reason})"),
    )
    session.set_mode(cfg.voice_mode)

    # ── Callbacks ──
    last_ble_activity = time.time()
    last_key_activity  = 0.0
    last_health_check  = 0.0
    last_start_search  = 0.0   # START_SEARCH 去抖（对标 vRemoter 0.5s）
    ctl_token = aud_token = None
    stream    = None

    def on_control(sender, args):
        nonlocal last_ble_activity, last_start_search
        last_ble_activity = time.time()
        try:
            data = bytes(args.characteristic_value)
            op   = data[0] if data else 0
            logger.debug(f"CTL: 0x{op:02X}  len={len(data)}")

            event = atvv.parse_control(data)
            if event is None:
                return

            if event["type"] == "capabilities":
                caps = atvv.state.caps
                if caps:
                    logger.info(f"📋 CAPS: v{caps.version[0]}.{caps.version[1]}  "
                                f"codec=0x{caps.codecs:02X}  sr={caps.sample_rate}Hz  frame={caps.frame_size}")
            elif event["type"] == "audio_start":
                session.on_audio_start(event["codec"], event["stream_id"])
                atvv.state.decoder.reset()
                _pending_samples.clear()
                logger.info("▶ Audio START")
                state.update(streaming=True, last_event="语音中…")
                trigger_voice_hotkey()
            elif event["type"] == "audio_stop":
                session.on_audio_stop(event["reason"])
                logger.info("⏹ Audio STOP")
                state.update(streaming=False, level=0, last_event="语音结束")
                trigger_voice_hotkey()
            elif event["type"] == "mic_open_result":
                session.on_mic_open_result(event["code"])
            elif event["type"] == "start_search":
                # 遥控器语音键按下 → host 必须回 MIC_OPEN，否则遥控器不推音频流。
                now = time.time()
                if atvv.state.stream_active:
                    return                        # 已在推流，忽略重复事件
                if now - last_start_search < 0.5:
                    return                        # 去抖（对标 vRemoter 0.5s）
                last_start_search = now
                logger.info("🔎 START_SEARCH (语音键) → 请求开麦")
                session.voice_key_down()
            elif event["type"] == "audio_sync":
                pass  # handled by decoder internally
        except Exception as e:
            logger.error(f"CTL handler error: {e}")

    def on_audio(sender, args):
        nonlocal last_ble_activity
        last_ble_activity = time.time()
        if not atvv.state.stream_active:
            return
        try:
            raw = bytes(args.characteristic_value)
            samples = atvv.decode_audio(raw)
            if samples is None:
                return
            _pending_samples.extend(samples)
            try:
                _sample_queue.put_nowait(samples)
            except queue.Full:
                logger.debug("Queue full, dropping frame")
        except Exception as e:
            logger.error(f"AUD handler error: {e}")

    # ── Subscribe BEFORE sending CAPS ──
    logger.info("📡 Subscribing to notifications...")
    ctl_token  = ctl_char.add_value_changed(on_control)
    try:
        await ctl_char.write_client_characteristic_configuration_descriptor_async(
            GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
        logger.info("✅ CTL subscribed")
    except Exception as e:
        logger.error(f"CTL subscribe failed: {e}")
        return False

    aud_token  = audio_char.add_value_changed(on_audio)
    try:
        await audio_char.write_client_characteristic_configuration_descriptor_async(
            GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
        logger.info("✅ AUD subscribed")
    except Exception as e:
        logger.error(f"AUD subscribe failed: {e}")
        return False

    writer = DataWriter()
    writer.write_bytes(GET_CAPS_CMD)
    wr = await tx_char.write_value_with_result_async(writer.detach_buffer())
    logger.info(f"📤 CAPS request sent  status={wr.status}")

    # ── Audio output ──
    loop = asyncio.get_event_loop()
    out_dev = await loop.run_in_executor(None, _find_cable, cfg.audio_output)
    if out_dev is None:
        logger.error(
            f"❌ '{cfg.audio_output}' not found!\n"
            "   Install VB-CABLE: https://vb-audio.com/Cable/\n"
            "   ⚠ VB-CABLE 命名是反的，别选错：\n"
            "     · 本桥把语音『播放』到 CABLE Input（播放设备）\n"
            "     · 要收声的 App（微信等）麦克风应选 CABLE Output（录音设备）"
        )
        return False
    logger.info(f"✅ Audio output: device {out_dev}")

    def _start_stream():
        return _create_stream(out_dev, atvv.state.sample_rate or 16000)

    stream = await loop.run_in_executor(None, _start_stream)
    await loop.run_in_executor(None, stream.start)
    logger.info("✅ Audio stream started")

    # ── Keyboard hooks ──
    import keyboard as _kb

    KEY_MAP = {
        "browser start and home":     "home",
        "back":                       "back",
        "enter":                      "ok",
        "escape":                     "back",
        "volume mute":                "mute",
        "volume up":                  "vol_up",
        "volume down":                "vol_down",
        "media play pause":           "ok",
    }

    def _on_key(e):
        nonlocal last_key_activity
        if e.event_type == "down":
            last_key_activity = time.time()

        # Map key name → button_id
        btn_id = KEY_MAP.get(e.name, "")
        if not btn_id:
            for k, v in KEY_MAP.items():
                if k in (e.name or ""):
                    btn_id = v
                    break

        if btn_id and btn_id != "voice":
            handled = resolve_button(
                btn_id, event_type=e.event_type,
                on_voice=None,
            )
            if handled:
                return False

        return True

    # suppress=True 会连物理键盘的 Enter/Esc/方向键一起吞掉（遥控器 HID 事件
    # 与物理键盘不可区分），默认关闭；确需拦截请在 config.json 设 suppress_keys:true
    _kb.hook(_on_key, suppress=bool(cfg.suppress_keys))
    logger.info(f"✅ Keyboard hooks active (suppress={cfg.suppress_keys})")

    # ── Health check ──
    async def check_health() -> bool:
        nonlocal last_health_check
        now = time.time()
        if now - last_health_check < cfg.heartbeat_cooldown:
            return True
        last_health_check = now
        try:
            result = await asyncio.wait_for(
                ctl_char.read_value_async(), timeout=cfg.gatt_timeout,
            )
            ok = result and result.status == GattCommunicationStatus.SUCCESS
            if not ok:
                logger.warning("💓 Health check failed → reconnect")
            return ok
        except Exception as e:
            logger.warning(f"💓 Health check error: {e} → reconnect")
            return False

    # ── Main loop ──
    logger.info("=" * 60)
    logger.info("✅ Bridge ready!  Press remote voice button and speak.")
    logger.info("   💡 After sleep: press HOME/arrow → wait 1s → press mic")
    logger.info("=" * 60)

    ran_ok = False
    try:
        while True:
            await asyncio.sleep(0.2)

            if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
                logger.warning("📡 Disconnected")
                break

            idle = time.time() - last_ble_activity
            if idle > cfg.watchdog_timeout:
                logger.warning(f"⏱️  Watchdog: {idle:.0f}s idle → reconnect")
                break

            if (time.time() - last_key_activity < cfg.key_check_window and
                    time.time() - last_health_check > cfg.heartbeat_cooldown):
                if not await check_health():
                    break

        ran_ok = True
    except KeyboardInterrupt:
        ran_ok = True
        logger.info("\n⏹️  Interrupted")
    finally:
        state.reset()
        if stream:
            try: stream.stop(); stream.close()
            except Exception: pass
        if ctl_char and ctl_token:
            try: ctl_char.remove_value_changed(ctl_token)
            except Exception: pass
        if audio_char and aud_token:
            try: audio_char.remove_value_changed(aud_token)
            except Exception: pass
        try: _kb.unhook_all()
        except Exception: pass
        try: ble.close()
        except Exception: pass
        logger.info("🧹 Cleanup done")
    return ran_ok


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="remote-voice-bridge")
    parser.add_argument("--device", choices=list(DEVICES.keys()), help="Force device type")
    parser.add_argument("--name",   help="Force device name pattern")
    parser.add_argument("--list",   action="store_true", help="List paired BLE devices")
    args = parser.parse_args()

    if args.list:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        devices = loop.run_until_complete(list_devices())
        print(f"\nPaired BLE devices ({len(devices)}):")
        for d in devices:
            tag = f"  [{d['sig'].vid:04X}:{d['sig'].pid:04X}]" if d["sig"] else ""
            print(f"  • {d['name']}{tag}")
        return

    cfg = Config.load()      # 供下方重连延时使用（原代码此处 cfg 未定义 → NameError）
    fail_count = 0
    while True:
        try:
            ok = asyncio.run(run_bridge(device_type=args.device, name_hint=args.name))
            fail_count = 0 if ok else fail_count + 1
        except Exception as e:
            logger.error(f"💥 Fatal: {e}", exc_info=True)
            fail_count += 1

        if fail_count >= 1:
            logger.info("=" * 60)
            logger.info("📢 Reconnecting... Press HOME/arrow to wake remote!")
            logger.info(f"   Retry in {Config.load().reconnect_delay}s (fail #{fail_count})")
            logger.info("=" * 60)

        if fail_count >= 5:
            logger.warning("⚠️  5 failures — check battery, pairing, and Bluetooth status")

        try:
            time.sleep(cfg.reconnect_delay)
        except KeyboardInterrupt:
            logger.info("👋 Bye!")
            break


if __name__ == "__main__":
    main()
