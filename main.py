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
import re
import sys
import threading
import time
import uuid
from collections import deque
from typing import Optional

from config import DEVICES, Config, find_device_by_name, hotkey_label, CONFIG_PATH
import state
from atvv import (
    ATVVProtocol, SERVICE_UUID, TX_UUID, AUDIO_UUID, CTL_UUID,
    GET_CAPS_CMD, _OP_AUDIO_START, _OP_AUDIO_STOP, _OP_MIC_OPEN_R,
)
from adpcm import IMAADPCMDecoder
from session import SessionCoordinator, Phase
from keys import voice_hotkey_down, voice_hotkey_up, hotkey_up
from buttons import resolve_button
import mixer

# 语音会话最长持续时间（秒）。超过就由程序自动收尾 ——
# 用户按了「开始」却忘了按第二下（或遥控器没电/走开了）时，
# 不能让输入法一直挂着听。这是本程序"管好语音功能"的一部分，不能指望用户记得。
VOICE_MAX_SECONDS = 600

# 松手后自动重开麦 → 遥控器多半会回一个 audio_start。
# 这段时间内收到的 audio_start 是**我们自己触发的**，不是用户"第二次按下"，
# 绝不能当成结束信号，否则会"刚开立刻被关"。
MIC_REOPEN_GRACE = 1.5

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
# 增益不再是常量：控制台的滑块通过 state.set_mix() 随时改，
# 输出回调每个块读一次（见 _create_stream），改完立即生效。

# 诊断计数：用来判断"遥控器到底有没有把音频推上来"。
# 之前这两个数字完全没有记录，导致"输入法没反应"无法区分是没收到音频、
# 还是收到了但没触发输入法 —— 只能靠猜。
_audio_frames = 0
_audio_peak   = 0

# 手动重连请求。控制台点「重新连接」时置位，主循环看到就断开重来。
# 为什么不在控制台里 Popen 一个新进程：那样会出现两个实例同时抢同一个 BLE
# 连接和同一个托盘图标，谁赢不确定，表现为"点了重连就时好时坏"。
_reconnect_request = threading.Event()


def request_reconnect() -> None:
    """由 UI 线程调用：请求桥线程断开并按新配置重连。"""
    _reconnect_request.set()


def _downsample(samples: list[int], n: int = 4) -> list[int]:
    """把一帧音频压成 n 个代表点，给 UI 画波形用。

    每段取**绝对值最大**的那个，而不是平均 —— 波形的用途是"一眼看出有没有
    声音进来"，平均值会把语音削平成一条直线，峰值能保住轮廓。
    """
    if not samples:
        return [0] * n
    step = max(1, len(samples) // n)
    out: list[int] = []
    for i in range(0, len(samples), step):
        seg = samples[i:i + step]
        if seg:
            out.append(max(seg, key=abs))
    return (out + [0] * n)[:n]


def _find_cable(device_name: str = "CABLE Input"):
    """按名字找输出设备，返回 sounddevice 的设备序号。

    ⚠ 必须先**精确匹配**再退回子串匹配。
    装了多只虚拟声卡（VB-CABLE + CABLE 2 + Voicemeeter）时，
    `CABLE Input` 是 `CABLE 2 Input` 的子串，而 PortAudio 的设备顺序由驱动
    枚举顺序决定、不保证谁在前 —— 子串优先会挑中 `CABLE 2 Input`，
    用户那边微信选的却是 `CABLE Output`，于是「一点声音都没有」，
    而且从日志上完全看不出选错了设备。
    """
    import sounddevice as sd
    low = device_name.lower()
    outs = [(i, d) for i, d in enumerate(sd.query_devices())
            if d["max_output_channels"] > 0]
    for i, d in outs:                       # ① 全名相等
        if d["name"].lower() == low:
            return i
    for i, d in outs:                       # ② 前缀（"CABLE Input (VB-Audio Virtual Cable)"）
        if d["name"].lower().startswith(low):
            return i
    for i, d in outs:                       # ③ 兜底子串
        if low in d["name"].lower():
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

    # ── 先把作废的配对记录挑出去 ──
    # AQS 按"配对状态"筛，只看记录**在不在**，不看记录还**对不对**。
    # 换过 USB 口之后，那张记录绑的本地地址已经不存在了，
    # 但它照样会被枚举出来 —— 再拿它去 from_id_async 只会
    # E_INVALIDARG，且每轮重连都要再抛一次（日志刷屏、一句有用的话都没有）。
    # 这里直接不选它，然后把「为什么」和「点哪」说清楚。
    now_addr = await _live_adapter_addr()
    devices, stale = _pick_live_devices(devices, now_addr)
    if stale and not devices:
        d0, local0 = stale[0]
        logger.error(
            "❌ 枚举到「%s」，但它的配对记录已经作废 —— 连不上就是这个原因。\n"
            "     记录绑定的本地蓝牙地址 : %s\n"
            "     当前生效的无线电地址   : %s\n"
            "   常见起因：蓝牙棒换过 USB 口 / 换过蓝牙模块 / 换过机器。\n"
            "   配对记录是按**本地地址**存的，地址一变，记录就跟着作废；\n"
            "   而 Windows 设置里仍然写着「已配对」（甚至能报电量），所以很难看出来。\n"
            "   处置（不用重新配对，也不用删设备）：%s",
            d0.name, _fmt_bt_addr(local0),
            _fmt_bt_addr(now_addr) if now_addr else "(读不到)", _repair_hint(),
        )
        state.update(last_event="配对记录已失效 → 跑「修复蓝牙配对」")
        return None, None

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


# ── BLE 连接：把"连不上"的原因说清楚 ──────────────────────────────────────────
def _fmt_bt_addr(v: int) -> str:
    """48 位整数地址 → AA:BB:CC:DD:EE:FF。"""
    return ":".join(f"{(v >> (8 * i)) & 0xFF:02X}" for i in range(5, -1, -1))


def _addr_from_ble_id(device_id: str):
    """从 `BluetoothLE#BluetoothLE<本地MAC>-<远端MAC>` 抠出两个地址。

    返回 (本地适配器地址, 远端地址)，解析不出就是 (None, None)。
    """
    m = re.search(r"BluetoothLE([0-9a-f:]{17})-([0-9a-f:]{17})", device_id or "", re.I)
    if not m:
        return None, None
    return int(m.group(1).replace(":", ""), 16), int(m.group(2).replace(":", ""), 16)


def _repair_hint() -> str:
    """告诉用户"下一步该点哪" —— 光说"配对失效了"没用。

    安装版：开始菜单 →「修复蓝牙配对」，或双击安装目录里的 .bat。
    源码版：python pairing.py --fix-pairing
    """
    if getattr(sys, "frozen", False):
        return ("开始菜单 →「修复蓝牙配对」"
                "（或双击安装目录里的『修复蓝牙配对.bat』）")
    return "python pairing.py --fix-pairing"


async def _live_adapter_addr():
    """当前真正生效的无线电地址（读不到返回 None）。"""
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        ad = await BluetoothAdapter.get_default_async()
        return getattr(ad, "bluetooth_address", None) if ad is not None else None
    except Exception:                           # noqa: BLE001
        return None


def _pick_live_devices(devices, now_addr):
    """把"设备 ID 里带的本地地址 ≠ 当前无线电地址"的那些挑出去。

    为什么必须挑出去：那张配对记录已经作废了，但 Windows **依然会把它枚举出来**
    （AQS 是按"配对状态"筛的，它只看记录在不在，不看记录还对不对）。
    于是每轮重连都会拿一个打不开的 ID 去 from_id_async → 抛异常 → 重连 →
    再抛一次，日志刷屏却一句有用的话都没有。

    返回 (可用的, [(过期设备, 它的本地地址)])。
    """
    good, stale = [], []
    for d in devices:
        local, _remote = _addr_from_ble_id(getattr(d, "id", "") or "")
        if local is None or now_addr is None or local == now_addr:
            good.append(d)
        else:
            stale.append((d, local))
    return good, stale


async def _open_ble_device(dev_info):
    """拿到一个可用的 BluetoothLEDevice；拿不到就把**原因和处置**写清楚再返回 None。

    为什么不能一句 `await BluetoothLEDevice.from_id_async(...)` 了事：
    "能枚举到"和"能连"是两件事。配对记录是**按本地蓝牙无线电**存的
    （注册表 `BTHPORT\\Parameters\\Devices\\<远端MAC>\\ServicesFor<本地适配器MAC>`），
    所以这块无线电换了、USB 口换了、地址变了之后：AQS 仍然能列出这台设备、
    Windows 设置里也仍然写着「已配对」，但 from_id_async 会直接抛
    `OSError: [WinError -2147024809] 提供的设备 ID 不是有效的 BluetoothLEDevice 对象`
    （E_INVALIDARG），from_bluetooth_address_async 则返回 None。

    2026-09-15 真机事故：桥每 3 秒崩一次 + 日志一片空白（异常被 print 丢掉）
    + 托盘还写着「按遥控器任意键唤醒」，三件事叠起来把方向带偏了两小时。
    """
    from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothAdapter

    # 主路径：按设备 ID
    err = None
    try:
        dev = await BluetoothLEDevice.from_id_async(dev_info.id)
        if dev is not None:
            return dev
    except OSError as e:
        err = e

    # 备用路径：按远端 MAC（有些机器上缓存 ID 失效，但地址仍然能用）
    local_addr, remote_addr = _addr_from_ble_id(dev_info.id)
    if remote_addr is not None:
        try:
            logger.warning("⚠️  按设备 ID 连接失败（%s），改用远端地址 %s 重试…",
                           err, _fmt_bt_addr(remote_addr))
            dev = await BluetoothLEDevice.from_bluetooth_address_async(remote_addr)
            if dev is not None:
                logger.info("✅ 备用方式（按远端地址）连接成功")
                return dev
        except OSError as e2:
            logger.warning("⚠️  按远端地址也失败：%s", e2)

    # 两条路都不通 —— 把"为什么"和"怎么办"一起写进日志
    now_addr = await _live_adapter_addr()

    name = dev_info.name or "该设备"
    if local_addr is not None and now_addr and local_addr != now_addr:
        # 最典型、也最难猜到的一种：配对记录绑在**另一块无线电**（或这块 dongle 的旧地址）上
        logger.error(
            "❌ 配对记录与当前蓝牙无线电对不上 —— 这就是连不上的原因。\n"
            "     配对记录绑在的本地地址 : %s\n"
            "     当前生效的无线电地址   : %s\n"
            "   设备能被枚举到、Windows 设置里也显示「已配对」，所以看着一切正常；\n"
            "   但蓝牙栈造不出可用的设备对象（E_INVALIDARG），怎么重试都一样。\n"
            "   处置（v1.0.10 起**不用**再删设备/重新配对了）：%s\n"
            "   它会把配对记录迁到当前地址下（改注册表前自动备份）。",
            _fmt_bt_addr(local_addr), _fmt_bt_addr(now_addr),
            _repair_hint(),
        )
        state.update(last_event="配对记录已失效 → 跑「修复蓝牙配对」")
    else:
        logger.error(
            "❌ 连接失败（%s）—— 设备枚举到了，但蓝牙栈给不出可用的设备对象。\n"
            "   处置：设置 → 蓝牙和其他设备 → 删除「%s」后重新配对。", err, name,
        )
        state.update(last_event="连接失败，建议删除设备后重新配对")
    return None


# ── Audio stream ───────────────────────────────────────────────────────────────
def _create_stream(out_dev: int, sample_rate: int, sysmic=None):
    """输出流：把「遥控器麦克风」和「电脑麦克风」按各自的增益/静音/独奏相加后写出去。

    为什么输出流要**一直开着**（而不是只在遥控器推流时开）：
    电脑麦克风那一路是持续的，关掉输出流等于把房间里的声音也一起掐了 ——
    微信就完全听不见你说话了，那还不如不装这个软件。
    """
    import sounddevice as sd

    def cb(outdata, frames, timeinfo, status):
        if status:
            logger.debug(f"Audio status: {status}")
        # 混合波形取点密度：按"每秒约 200 个点"折算，让三路波形的时间窗口一致
        # （遥控器那一路是每 20ms 帧推 4 点，电脑麦克风是每块推 4 点）。
        # 不折算的话 48kHz 输出下每块只有 5ms，波形会被压成短短一小段。
        wave_step = max(1, int(frames * 200 / max(1, sample_rate)))
        wave_pts: list[int] = []
        mx = state.mix_params()
        # 增益为 0 == 这一路不发声（静音/未参与混音/被别人独奏压掉）
        r_gain = float(mx["remote_gain"]) if state.source_audible("remote") else 0.0
        s_gain = float(mx["sys_gain"]) if state.source_audible("sys") else 0.0

        sys_blk = None
        if sysmic is not None and s_gain > 0.0:
            try:
                sys_blk = sysmic.read(frames)
            except Exception:                      # noqa: BLE001
                sys_blk = None
        sys_len = len(sys_blk) if sys_blk is not None else 0

        peak = 0
        for i in range(frames):
            v = 0.0
            if r_gain > 0.0:
                if _pending_samples:
                    v = int(_pending_samples.popleft()) * r_gain
                else:
                    try:
                        _pending_samples.extend(_sample_queue.get_nowait())
                        v = int(_pending_samples.popleft()) * r_gain
                    except queue.Empty:
                        v = 0.0
            if i < sys_len:
                v += float(sys_blk[i]) * s_gain

            iv = int(v)
            if iv > 32767:
                iv = 32767
            elif iv < -32768:
                iv = -32768
            outdata[i, 0] = iv
            a = iv if iv >= 0 else -iv
            if a > peak:
                peak = a
            if i % wave_step == 0:
                wave_pts.append(iv)

        # 混合输出的电平 + 波形：UI 上「混合输出」那张卡片就是看这两个
        state.push_levels(mix_db=state.db_from_peak(peak))
        if wave_pts:
            state.push_mix_audio(wave_pts)

    return sd.OutputStream(
        device=out_dev, channels=1, dtype="int16",
        samplerate=sample_rate, blocksize=240, callback=cb,
    )


# ── Main bridge ────────────────────────────────────────────────────────────────
async def run_bridge(device_type: str | None = None, name_hint: str | None = None):
    cfg = Config.load()
    if device_type:
        cfg.device = device_type

    # ⚠ 主事件循环引用 —— BLE 回调线程往回投递任务全靠它，见 _send_tx。
    # v1.0.3 真机事故：MIC_OPEN/MIC_CLOSE 在 BLE 回调线程上直接
    # asyncio.get_event_loop()，那里没有事件循环，写入**一次都没发出去**。
    _bridge_loop = asyncio.get_running_loop()

    sig = DEVICES.get(cfg.device)

    # 配置文件里的增益是"开机值"，启动时灌进实时增益；之后控制台说了算。
    state.set_gain(cfg.gain)
    state.set_mix(
        remote_gain=cfg.gain,
        sys_gain=cfg.system_mic_gain,
        sys_enabled=cfg.system_mic_enabled,
        remote_enabled=cfg.remote_mic_enabled,
    )

    logger.info("=" * 60)
    logger.info("remote-voice-bridge starting")
    logger.info(f"  Device : {cfg.device}  ({sig.vid:04X}:{sig.pid:04X})" if sig else f"  Device : {cfg.device}")
    logger.info(f"  IM     : {cfg.input_method}  audio: {cfg.audio_output}  gain: {state.get_gain()}x")
    _vk = cfg.trigger_keys_windows()
    logger.info(f"  Voice  : {hotkey_label(_vk) or '(未配置)'}  [{'+'.join(_vk) or '-'}]  mode={cfg.hotkey_mode}")
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
    ble = await _open_ble_device(dev_info)
    if ble is None:
        return False
    if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
        logger.warning("⚠️  Not connected yet — remote may be sleeping. Press any button to wake.")
        for _ in range(10):
            await asyncio.sleep(0.5)
            if ble.connection_status == BluetoothConnectionStatus.CONNECTED:
                break
        if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
            logger.error("❌ Connection failed")
            # 让托盘/控制台说真话，而不是继续显示「按遥控器任意键唤醒」
            state.update(last_event="连接超时（遥控器可能没醒）")
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

    def _send_tx(cmd: bytes, tag: str = "TX") -> bool:
        """Write raw bytes to the ATVV TX characteristic (fire-and-forget).

        返回 True = **已成功投递到主事件循环**（写入本身异步进行）。

        ⚠ 必须用 run_coroutine_threadsafe 投递回主循环，不能在当前线程直接
        asyncio.get_event_loop().create_task() —— BLE 通知回调跑在 WinRT/COM
        线程池线程（线程名 Dummy-XXXX）上，那里**没有事件循环**，get_event_loop()
        会抛 "There is no current event loop in thread 'Dummy-XXXX'"。
        v1.0.3 的真机事故正是这个：MIC_OPEN/MIC_CLOSE 一次都没发出去，
        而调用方（session.ensure_mic_open）只看命令构造成功就打了"补发成功"，
        日志一片祥和、实际全是假的。
        """
        if not cmd:
            return False

        async def _write():
            try:
                w = DataWriter()
                w.write_bytes(cmd)
                r = await tx_char.write_value_with_result_async(w.detach_buffer())
                logger.info(f"📤 {tag} [{cmd.hex(' ')}] status={r.status}")
                return r.status == GattCommunicationStatus.SUCCESS
            except Exception as e:
                logger.error(f"{tag} write failed: {e}")
                return False

        coro = _write()
        try:
            asyncio.run_coroutine_threadsafe(coro, _bridge_loop)
            return True
        except Exception as e:
            # 先把没被接管的协程关掉：否则解释器会在 GC 时甩一条
            # "coroutine was never awaited" RuntimeWarning，
            # 让真正的错误信息（下面那行 logger.error）被噪声淹没。
            coro.close()
            logger.error(f"{tag} schedule failed: {e}")
            return False

    def _on_mic_open(sid: int):
        """Host 主动开麦 — 必须真正写入 BLE，否则遥控器不会推流。

        返回 None = 没发出去（构造失败或调度失败），调用方据此不置 mic_open_sent。
        """
        try:
            cmd = atvv.mic_open_cmd()
        except Exception as e:
            logger.error(f"mic_open_cmd failed: {e}")
            return None
        return cmd if _send_tx(cmd, "MIC_OPEN") else None

    def _on_mic_close(sid: int):
        try:
            cmd = atvv.mic_close_cmd(sid)
        except Exception as e:
            logger.error(f"mic_close_cmd failed: {e}")
            return None
        return cmd if _send_tx(cmd, "MIC_CLOSE") else None

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

    # ── 语音会话状态 —— **由本程序自己记账**，不能甩给输入法 ──────────────
    #
    # 为什么必须有这一层：遥控器语音键是 PTT 硬件，一次按键必然产生
    # audio_start(按下) + audio_stop(松开) 两个事件；而用户要的是
    # "按一下开始、松手继续说、再按一下才结束" —— **松手 ≠ 语音结束**。
    # 这个区别只有本程序知道：输入法只认按键本身，它分不清
    # "用户松手了" 和 "用户想结束这一段"。
    #
    # 分工是这样的：
    #   · 输入法那一半 = 它原生就有的能力（按一下开始听 / 再按一下结束），我们去**适配**它
    #   · 程序这一半   = 什么时候算开始、什么时候算结束、会不会卡死、异常怎么恢复
    # 两边都要有，缺一个都不行。
    voice_active     = False   # 语音会话是否正在进行
    voice_started_at = 0.0     # 本次会话开始时刻（超时兜底用）
    mic_reopen_at    = 0.0     # 上次"松手后自动重开麦"的时刻（防自激用）
    ctl_token = aud_token = None
    stream    = None
    sysmic    = None
    # 认不出来的控制指令 / 键名，各自只报一次（去重集合）。
    # 见 on_control 与 _on_key 里的注释：这两个洞让"按键没反应"永远查不出结论。
    _unknown_ops: set[int] = set()

    def on_control(sender, args):
        nonlocal last_ble_activity, last_start_search, voice_active, voice_started_at
        nonlocal mic_reopen_at
        global _audio_frames, _audio_peak
        last_ble_activity = time.time()
        try:
            data = bytes(args.characteristic_value)
            op   = data[0] if data else 0
            logger.debug(f"CTL: 0x{op:02X}  len={len(data)}")

            event = atvv.parse_control(data)
            if event is None:
                # ⚠ 认不出来的控制指令原先**只在 DEBUG 打一行、然后静默 return**，
                #   而正式版日志级别是 INFO —— 等于遥控器发来的任何"我们还没实现"
                #   的指令，在日志里是一片空白。
                #   武哥报的「静音键按了没反应」正好卡死在这里：日志既不能证明
                #   遥控器发了它，也不能证明没发，只能靠猜。
                #   现在按 **opcode 去重** 报一次（同一个 opcode 只吵一次，不刷屏），
                #   按一下键就能在日志/控制台日志页里看到答案。
                if op not in _unknown_ops:
                    _unknown_ops.add(op)
                    logger.info(
                        f"🔘 收到未处理的控制指令 0x{op:02X}（len={len(data)}）"
                        f" 原始={data.hex(' ')}"
                    )
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
                _audio_frames = 0
                _audio_peak   = 0
                state.clear_audio()          # 清掉上一次的波形，UI 从空开始画
                state.update(streaming=True, last_event="语音中…")
                # ⚠ 必须补发 MIC_OPEN。
                # 遥控器按语音键时只会上报 AUDIO_START(0x04)，从不发 START_SEARCH(0x08)，
                # 而开麦命令原先只挂在 start_search 分支 → 遥控器永远收不到 MIC_OPEN
                # → 一帧音频都不推，日志表现就是连续「本次共收到 0 个音频帧」。
                #
                # ⚠⚠ 补发成功后**必须**顺手记一次 mic_reopen_at。
                #   遥控器收到 MIC_OPEN 会立刻回一个 audio_start（意思是"我开始推流了"），
                #   那是我们要来的回响，**不是用户按了第二次**。不挡掉它会一路向下走到
                #   else 分支，后果是连环的：
                #     ① 立刻 voice_hotkey_up() → 输入法那边的语音键被松开
                #     ② voice_active 变 False → 等真松手(audio_stop)时不再补开麦
                #     ③ 遥控器保持停推流 → 「本次共收到 0 个音频帧」
                #   用户看到的就是「混音卡在等待录音 / 转文字逐字卡 / 一松手就不再调用输入法」。
                #   2026-09-15 真机日志正是这个时序：补发 36.944s → 回响 36.995s，
                #   只隔 51ms，远在 MIC_REOPEN_GRACE(1.5s) 之内 —— 只要这里记了就能挡住。
                if session.ensure_mic_open():
                    mic_reopen_at = time.time()
                # ── 语音会话状态机：**每一次按下 = 一次翻转** ──────────────
                #   第 1 次按下 → 开始   第 2 次按下 → 结束
                # 松手（audio_stop）不参与翻转，见下面那个分支的注释。
                #
                # 底层用的是微信输入法原生就有的能力，我们只是去适配它：
                #   tap  模式 → 「启动语音输入」左Ctrl+左Win+左Shift，点一下开始/再点一下结束
                #   hold 模式 → 「按住说话」Ctrl+Win，按下保持/再按松开
                # 至于"现在到底算不算在说话"，是本程序在这里记账。
                # ⚠ 防自激：刚自动重开麦后，遥控器多半会回一个 audio_start。
                #   那是我们自己触发的，不是用户"第二次按下" —— 当成结束就会
                #   "刚开立刻被关"，必须在这里挡掉。
                if mic_reopen_at and (time.time() - mic_reopen_at) < MIC_REOPEN_GRACE:
                    mic_reopen_at = 0.0
                    logger.debug("↩ 忽略自动重开麦触发的 audio_start（非用户第二次按下）")
                elif not voice_active:
                    voice_hotkey_down()      # tap=点按开始 / hold=按下并保持
                    voice_active     = True
                    voice_started_at = time.time()
                    state.update(streaming=True, last_event="语音中…")
                    logger.info("🎙️ 语音会话【开始】—— 可以松手了，会一直听着")
                else:
                    voice_hotkey_up()        # tap=再点按结束 / hold=松开结束
                    voice_active     = False
                    voice_started_at = 0.0
                    state.update(streaming=False, level=0, last_event="语音结束")
                    logger.info("🎙️ 语音会话【结束】（第二次按下语音键）")
            elif event["type"] == "audio_stop":
                session.on_audio_stop(event["reason"])
                # ⚠ 松手 **不等于** 语音结束。
                #
                # 遥控器语音键是 PTT 硬件：按下必发 audio_start、松开必发 audio_stop。
                # 可用户要的是「按一下开始、松手继续说」—— 所以这里**只结算音频统计，
                # 绝不结束语音会话**。结束只可能来自两处：
                #   ① 再一次按下语音键（上面的 audio_start 分支翻转）
                #   ② 按下确认键（见 _on_key）
                #
                # 早前正是这里无条件调用 voice_hotkey_up()，于是「按下开启 → 松手立刻结束」，
                # 每次只能录到松手前那一小段 —— 「按一下长输」完全不生效的根因。
                if voice_active:
                    # 让遥控器麦克风**继续收音** —— 不用一直按着。
                    #
                    # 遥控器松手时会自己停推流（audio_stop），但它只认 host 的开麦命令：
                    # 再发一次 MIC_OPEN，它就会重新开始推。
                    # vRemoter v1.1.1 修的「短按只打开输入法、遥控器麦克风却未持续收音」
                    # 说的正是这件事 —— 所以这不是硬件的墙，是我们该做到而没做到的。
                    #
                    # ⚠ 两件事必须一起做，少一件都收不到声音：
                    #   ① session.ensure_mic_open()     → 让遥控器重新开始推流
                    #   ② atvv.state.stream_active=True  → 否则 atvv 层把后续帧**全部丢掉**
                    #      （on_audio 与 decode_audio 都先看这个标志，audio_stop 时已被置 False）
                    if session.ensure_mic_open():
                        atvv.state.stream_active = True
                        mic_reopen_at = time.time()
                        logger.info("🎤 松手后自动重新开麦 → 遥控器麦克风继续收音")
                    elif not session.state.mic_open_sent:
                        # ensure_mic_open 返回 False 有两种：已发过（正常），
                        # 和**根本没发出去**（事故）。必须分开说，不许报喜不报忧 ——
                        # v1.0.3 就是在这里假成功，武哥松手后录音直接断流。
                        logger.error(
                            "❌ MIC_OPEN 未能发出 —— 松手后遥控器不会再推流，"
                            "语音会断！请把这段日志发出来定位"
                        )
                    logger.info(
                        f"⏹ 松手（语音仍在继续 · 本次 {_audio_frames} 帧，峰值 {_audio_peak}）"
                    )
                else:
                    logger.info(f"⏹ Audio STOP （本次共收到 {_audio_frames} 个音频帧，峰值 {_audio_peak}）")
                    state.update(streaming=False, level=0, last_event="语音结束")
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
        global _audio_frames, _audio_peak
        last_ble_activity = time.time()
        if not atvv.state.stream_active:
            return
        try:
            raw = bytes(args.characteristic_value)
            samples = atvv.decode_audio(raw)
            if samples is None:
                return
            _audio_frames += 1
            peak = max((abs(s) for s in samples), default=0)
            if peak > _audio_peak:
                _audio_peak = peak
            # 电平表：按 int16 满量程折算，×3 让正常说话也能推出半格
            level = min(100, int(peak * 300 / 32768))
            # 喂给 UI：波形点 + 诊断指标（帧数/峰值/采样率）
            state.push_audio(_downsample(samples, 4), level, _audio_frames, _audio_peak,
                             atvv.state.sample_rate, len(raw))

            # ⚠⚠ 遥控器那一路的**电平**必须在这里生产，不能指望别人。
            #
            # 控制台「遥控器麦克风」卡片读的是 state.remote_level_db，而它只由
            # state.push_levels(remote_db=…) 写入 —— 全项目里此前**没有任何一处
            # 产品代码传过 remote_db**（只有 tools/serve_console.py 那个假数据
            # 演示服务器传过）。于是真机上它永远是初始值 -96 dBFS：
            #   波形在动（走 push_audio 的 _wave），状态却死死卡在「等待语音」、
            #   电平条一格都不亮 —— 看着就像"这一路完全没反应"。
            # 2026-09-15 武哥报的正是这个：混音正常、能说话，就是遥控麦克风那张卡
            # 一直是「等待语音」。根因是"只有消费者、没有生产者"，不是音频本身。
            #
            # 这里用**原始解码峰值**算 dBFS，而不是上面那个 0-100 的 level ——
            # UI 显示的是 dB，用 int16 满量程折算才和另外两路同一把尺子。
            state.push_levels(remote_db=state.db_from_peak(peak))
            if _audio_frames == 1:
                logger.info(f"🔊 收到第一个音频帧：{len(raw)}B → {len(samples)} 采样")
            elif _audio_frames % 100 == 0:
                logger.info(f"🔊 音频帧 {_audio_frames}（{len(raw)}B/帧，峰值 {_audio_peak}）")

            # ⚠ 只能走队列这一条路。
            # 早前这里还额外做了一次 `_pending_samples.extend(samples)`，
            # 而播放回调在 `_pending_samples` 空时也会从同一个队列里取同一批数据
            # —— 等于每个采样被播两遍，音频变成断续碎片，喂给输入法必然是垃圾。
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
    state.update(out_dev_name=cfg.audio_output)

    # ── 电脑麦克风（混音的另一路）──
    # 采不到不影响遥控器那一路能用 —— 降级成"只有遥控器麦克风"，而不是整体失败。
    sysmic = None
    if cfg.system_mic_enabled:
        sysmic = mixer.SystemMic(cfg.system_mic_device, atvv.state.sample_rate or 16000)
        ok = await loop.run_in_executor(None, sysmic.start)
        if ok:
            state.update(sys_mic_ready=True, sys_mic_name=sysmic.device_label)
        else:
            state.update(sys_mic_ready=False, sys_mic_name="")
            sysmic = None
    else:
        logger.info("ℹ️  电脑麦克风已在设置里关闭，本次只桥接遥控器一路")

    def _start_stream():
        return _create_stream(out_dev, atvv.state.sample_rate or 16000, sysmic)

    stream = await loop.run_in_executor(None, _start_stream)
    await loop.run_in_executor(None, stream.start)
    logger.info("✅ Audio stream started")

    # ── Keyboard hooks ──
    import keyboard as _kb
    from keys import was_self_injected

    # 遥控器按键（HID）→ 内部 button_id。
    # 键名是 keyboard 库的写法，必须和它实际报出来的名字一致，
    # 否则这个按键在映射界面里改了也不会有反应。
    KEY_MAP = {
        "browser start and home":     "home",
        "browser home":               "home",
        "home":                       "home",
        "back":                       "back",
        "browser back":               "back",   # HID 消费键，遥控器返回键常报这个名字
        "enter":                      "ok",
        "return":                     "ok",
        "escape":                     "back",
        "up":                         "up",
        "down":                       "down",
        "left":                       "left",
        "right":                      "right",
        "volume mute":                "mute",
        "volume up":                  "vol_up",
        "volume down":                "vol_down",
        "media play pause":           "ok",
    }

    # keymap 缓存：按键事件可能一秒来好几个，不能每次都读一遍 config.json。
    # 用 mtime 判断是否被控制台/手改过 —— 改了立即生效，不用重启。
    _cfg_cache: dict = {}
    _cfg_mtime: float = -1.0
    _keymap_warned: set[tuple[str, str]] = set()
    # 首次见到的键名只报一次，见 _on_key 里那两处。
    _seen_keys: set[str] = set()

    def _warn_bad_keymap(keymap: dict) -> None:
        """开机/热重载时检查一遍：映射值里的键名是不是真的能发出去。

        为什么要它：`send_key` 遇到解析不出来的键名会**静默跳过** ——
        用户看到的是「这个按键没反应」，而日志里一个字都没有。
        手改过 config.json、或从旧版本带过来的配置很容易踩到。

        `check_keymap.py` 只在 CI 里校验**默认表**，管不到用户实际在用的那份；
        这里补上运行时的那半边。虚拟目标（原生直通 / 语音 / 按住说话）不是组合键，
        按 VIRTUAL_TARGETS 跳过。
        """
        from config import VIRTUAL_TARGETS
        from keys import combo_bad_parts
        for btn, target in (keymap or {}).items():
            t = str(target or "").strip()
            if not t or t in VIRTUAL_TARGETS:
                continue
            bad = combo_bad_parts(t)
            if not bad:
                continue
            # 同一个坏值只吵一次，别在热重载后每按一次键刷一屏
            sig = (str(btn), t)
            if sig in _keymap_warned:
                continue
            _keymap_warned.add(sig)
            logger.warning(
                f"⚠ 按键映射「{btn}」→ {t!r} 里有发不出去的键名 {bad}，"
                f"这个键按下去不会有任何反应。请到控制台「按键映射」页改掉。"
            )

    def _get_cfg() -> dict:
        nonlocal _cfg_cache, _cfg_mtime
        try:
            mt = CONFIG_PATH.stat().st_mtime
        except OSError:
            mt = -1.0
        if mt != _cfg_mtime:
            _cfg_mtime = mt
            try:
                new = Config.load()
                _cfg_cache = {
                    "keymap": new.keymap,
                    "mapping_enabled": bool(getattr(new, "mapping_enabled", True)),
                    # 语音会话中要不要吞掉确认键的 Enter（见 config 里的长注释：
                    # 物理键盘与遥控器在钩子层不可区分，所以这个开关是必要的逃生口）
                    "swallow_ok": bool(getattr(new, "swallow_ok_during_voice", True)),
                }
                _warn_bad_keymap(_cfg_cache["keymap"])
                logger.debug(f"config reloaded (mtime={mt})")
            except Exception as e:
                logger.error(f"config reload failed: {e}")
        return _cfg_cache

    def _on_key(e):
        nonlocal last_key_activity, last_ble_activity, voice_active, voice_started_at
        if e.event_type == "down":
            last_key_activity = time.time()
            # ⚠ 这里**分不出遥控器和物理键盘**（低级钩子看不到来源设备，
            #    两者都没有 LLKHF_INJECTED 标志），所以注释不能说成"遥控器按键"。
            #    敲物理键盘同样会刷新它 —— 这是**刻意接受的保守偏差**：
            #    watchdog 的判断是"ATVV 静默 180 秒 + GATT 读也失败 → 重连"，
            #    重连一次遥控器要哑 4 秒左右。宁可偶尔漏判一次失联，
            #    也不要因为"你在敲键盘"就误判掉线。
            #    （换个角度：遥控器的按键走 HID 通道、不产生 ATVV 通知，
            #      不把 HID 活动算进来，就会出现"明明在按遥控器却被判失联"。）
            last_ble_activity = time.time()

        # ⚠ 丢掉自己的回声。
        # 遥控器与物理键盘在 Windows 上不可区分，键盘钩子既能看到用户按的键，
        # 也能看到我们自己注入的键。若映射目标正好又落回同一个键
        # （确认键 → Enter 就是典型），不挡掉就会无限自激。
        if was_self_injected(e.name):
            return True

        # ⚠ 第二个盲区：**键名本身为空**。
        #   `keyboard` 库遇到它解析不出来的键，会把 e.name 报成 None/空串。
        #   这种事件在下面每一个分支里都是 falsy，会**一路静默滑到最后**：
        #   不映射、不记日志、不报错，用户看到的就是"这个按键没反应"。
        #   比下面那个"键名认得出但表里没有"的盲区更靠前、更难查
        #   （连"它报了什么名字"都拿不到）。
        #   这里补一条带 scan/vk 的日志：至少能看出"确实收到过一个键"。
        if e.event_type == "down" and not e.name:
            if "<无名>" not in _seen_keys:
                _seen_keys.add("<无名>")
                logger.info(
                    "🔘 HID 按键【无名】—— keyboard 库没能解析出键名"
                    f"（scan={getattr(e, 'scan_code', None)}、"
                    f"vk={getattr(e, 'vk', None)}）→ 无法映射，已忽略。"
                    " 若这是遥控器上的键，请把这一行发出来。"
                )
            return True

        # Map key name → button_id
        btn_id = KEY_MAP.get(e.name, "")
        if not btn_id and e.name:
            # ⚠ 兜底只跑**多词复合键**（键名里带空格），单词键一律不动。
            # 单词键（up/down/left/right/back/home/enter/escape…）已由第 567 行
            # 的 `KEY_MAP.get` 精确匹配兜住，绝不能进兜底循环：
            # 词边界下 "up" 会误中物理键盘的 "page up"、"left" 误中 "left windows"，
            # 导致按方向/翻页键时顺手注入一个方向键。
            # 复合键（"browser back" / "browser start and home" / "volume up" 等）
            # 即便未来 keyboard 库换种写法、精确匹配失手，也能兜底接住。
            for k, v in KEY_MAP.items():
                if " " in k and re.search(rf"\b{re.escape(k)}\b", e.name):
                    btn_id = v
                    break

        # 语音进行中按「确认键」= 结束这一段（用户要的"最后按确认就算完成"）。
        #
        # ⚠ 这个判断只能靠程序自己记的 voice_active：输入法分不清用户这一下
        #   是想"确认输入文字"还是"结束这段语音"，它只看到来了一个键。
        #   由程序拍板：语音开着的时候，确认键就专管收尾，不再当 Enter 往外发。
        #
        # ⚠⚠ 已知代价：Windows 低级钩子**分不出遥控器和物理键盘**，
        #    所以"吞掉"会把物理键盘的 Enter 一起吞掉。
        #    suppress_keys=False 时 return False 本来就是空操作（keyboard 库只在
        #    suppress=True 时把回调挂进 blocking_hooks），所以这里必须看配置。
        #    给用户的逃生口：config.json 里 swallow_ok_during_voice=false。
        if voice_active and btn_id == "ok" and e.event_type == "down":
            swallow = bool(_get_cfg().get("swallow_ok", True))
            voice_hotkey_up()
            voice_active     = False
            voice_started_at = 0.0
            state.update(streaming=False, level=0, last_event="语音结束（确认键）")
            logger.info(
                "🎙️ 语音会话【结束】（确认键）"
                + ("；这一下不再当 Enter 发出" if swallow else "；Enter 照常放行")
            )
            return not swallow               # 吞掉这一下，不让它再当 Enter 发出去

        if btn_id and btn_id != "voice":
            cached = _get_cfg()
            if not cached.get("mapping_enabled", True):
                return True                  # 映射总开关关掉 → 遥控器当普通遥控器用
            # ⚠ 每个**首次出现**的键名都在这里报一次（之后静默）。
            #   这是"某个按键没反应"唯一能靠一次按键就查清的办法：
            #     · 日志里有这一行  → 键已经进了 Windows，问题在我们这层或下游
            #     · 日志里没有这一行 → 键根本没进 Windows，得从蓝牙/HID 那一层查
            #   2026-09-15「遥控器除了语音键其他键全没反应」就卡在这个盲区里：
            #   当时日志对 HID 按键**一个字都不写**，无法区分上面两种情形。
            #   去重是必须的：物理键盘敲字母也走这里，不去重会瞬间刷屏。
            if e.event_type == "down" and e.name and e.name not in _seen_keys:
                _seen_keys.add(e.name)
                logger.info(
                    f"🔘 HID 按键 {e.name!r}（scan={getattr(e, 'scan_code', None)}）"
                    f" → 按钮「{btn_id}」→ 动作 "
                    f"{(cached.get('keymap') or {}).get(btn_id, '')!r}"
                )
            handled = resolve_button(
                btn_id, event_type=e.event_type,
                keymap=cached.get("keymap") or {},
                on_voice=None,
            )
            if handled:
                return False
        elif e.event_type == "down" and e.name and e.name not in _seen_keys:
            # ⚠ 认不出的键名是**静默丢弃**的：映射表里没有它，程序什么都不做、
            #   也不留痕。用户看到的现象就是「这个按键没反应」，而日志里一个字
            #   都没有 —— 到底是遥控器根本没发这个键，还是发了但键名对不上号，
            #   无法区分。武哥的「静音键没反应」就卡在这个盲区里。
            # 同样只报一次（去重），物理键盘最多吵几十行就安静了。
            _seen_keys.add(e.name)
            logger.info(
                f"🔘 HID 按键 {e.name!r}（scan={getattr(e, 'scan_code', None)}）"
                f" → 没有对应按钮，已忽略"
                f"（若这是遥控器上的键，请到控制台「按键映射」页给它指定动作）"
            )

        return True

    # suppress=True 会连物理键盘的 Enter/Esc/方向键一起吞掉（遥控器 HID 事件
    # 与物理键盘不可区分），默认关闭；确需拦截请在 config.json 设 suppress_keys:true
    #
    # ⚠ 关键机制（读 keyboard 库源码确认，别凭印象）：
    #   keyboard.hook(cb, suppress=False) 把 cb 挂到 `handlers`，
    #   而真正决定"拦不拦"的 direct_callback 只看 `blocking_hooks`：
    #       if not all(hook(event) for hook in self.blocking_hooks): return False
    #   也就是说 —— **suppress=False 时，回调里 return False 是空操作**，
    #   一个键都拦不住。遥控器的原生按键会照旧生效，和我们的注入叠加成"按一下出两个动作"。
    #   这就是"不开启拦截遥控器原生按键就会输入失败"的真正原因。
    _kb.hook(_on_key, suppress=bool(cfg.suppress_keys))
    logger.info(f"✅ Keyboard hooks active (suppress={cfg.suppress_keys})")

    # 开机就把配置读一遍：既预热缓存，也顺手把"写了但发不出去"的映射值报出来。
    # 不这么做的话，第一次按键才发现问题，而"没反应"的用户一般不会去翻日志。
    _get_cfg()

    # ── Health check ──
    async def check_health(force: bool = False) -> bool:
        nonlocal last_health_check, voice_active, voice_started_at
        now = time.time()

        # 语音会话超时兜底：用户按了「开始」却忘了收尾，程序自己收场。
        # 「管理好语音输入功能」包含这一条 —— 不能指望用户永远记得按第二下。
        if voice_active and voice_started_at and (now - voice_started_at) > VOICE_MAX_SECONDS:
            logger.warning(
                f"⏰ 语音会话已持续 {int(now - voice_started_at)} 秒，超过上限 "
                f"{VOICE_MAX_SECONDS} 秒 → 自动结束，避免一直挂着听"
            )
            voice_hotkey_up()
            voice_active     = False
            voice_started_at = 0.0
            state.update(streaming=False, level=0, last_event="语音结束（超时自动收尾）")

        if not force and now - last_health_check < cfg.heartbeat_cooldown:
            return True

        # ⚠ 语音会话进行中**绝不做 GATT 读**。
        #
        # 为什么：CTL 特征既承载控制事件（audio_start / audio_stop），又被拿来当
        # 健康检查用。Windows 的 GATT 栈同一时刻只允许一个 ATT 事务在跑，
        # 读操作占着通道期间到达的通知会被压后、极端情况下直接丢。
        # 一旦丢掉 audio_stop，voice_active 就永远回不到 False ——
        # 下一次按语音键被当成"第二次按下"直接收尾，用户看到的是
        # 「说着说着自己断了，还得再按一下」。触发条件极日常：
        # 按过任意键后 3 秒内（key_check_window）就会来一次读。
        #
        # 语音期间本来就有音频帧在按 ~50Hz 刷新 last_ble_activity，
        # 链路活没活一眼就知道，这个读纯属多余。
        # force=True（watchdog 判定 ATVV 静默超时）时照读不误 —— 那才是真需要确认。
        if not force and state.get().streaming:
            last_health_check = now
            return ble.connection_status == BluetoothConnectionStatus.CONNECTED

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

            # 控制台点了「重新连接」/ 改了输出设备 → 断开重来，用上新配置
            if _reconnect_request.is_set():
                _reconnect_request.clear()
                logger.info("♻️  手动重连请求 → 断开并按新配置重建")
                ran_ok = True
                break

            if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
                logger.warning("📡 Disconnected")
                break

            # ⚠ 原来的逻辑：ATVV 静默超过 watchdog_timeout 就断线重连。
            # 但遥控器的按键走的是 HID 通道，不产生 ATVV 通知 —— 所以「ATVV 静默」
            # 完全不等于「链路断了」。实测后果：每 180 秒无条件重连一次，
            # 每次重连约 4 秒内遥控器不可用（日志里 02:14:11 / 02:17:15 两次即是）。
            # 正确做法：静默只当作"疑似"，必须再做一次 GATT 读确认才断开。
            idle = time.time() - last_ble_activity
            if idle > cfg.watchdog_timeout:
                if not await check_health(force=True):
                    logger.warning(f"⏱️  Watchdog: ATVV 静默 {idle:.0f}s 且健康检查失败 → reconnect")
                    break
                last_ble_activity = time.time()
                logger.debug(f"💓 Watchdog: ATVV 静默 {idle:.0f}s，但链路正常，继续")

            if (time.time() - last_key_activity < cfg.key_check_window and
                    time.time() - last_health_check > cfg.heartbeat_cooldown):
                if not await check_health():
                    break

        ran_ok = True
    except KeyboardInterrupt:
        ran_ok = True
        logger.info("\n⏹️  Interrupted")
    finally:
        # 安全兜底：退出/断线时绝不能把快捷键按着不放 ——
        # 否则 Ctrl / Win 会一直处于按下状态，整台电脑的键盘都会不正常。
        try: hotkey_up()
        except Exception: pass
        state.reset()
        if sysmic is not None:
            try: sysmic.stop()
            except Exception: pass
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
