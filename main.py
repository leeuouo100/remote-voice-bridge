"""
remote-voice-bridge — Windows ATVV bridge for Bluetooth voice remotes.
融合 vRemoter (完整 ATVV 协议栈 + 会话协调) 和 leufon/remote-voice-bridge
(VB-CABLE 音频管道 + 按键触发重连) 的技术。
"""

from __future__ import annotations
import asyncio
import logging
import math
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
from keys import voice_hotkey_down, voice_hotkey_up, hotkey_up, tap_key
from buttons import resolve_button
import mixer

# 语音会话最长持续时间（秒）。超过就由程序自动收尾 ——
# 用户按了「开始」却忘了按第二下（或遥控器没电/走开了）时，
# 不能让输入法一直挂着听。这是本程序"管好语音功能"的一部分，不能指望用户记得。
VOICE_MAX_SECONDS = 600

# 我们每发一次 MIC_OPEN，遥控器就会回一个 audio_start（"我开始推流了"）。
# 那一声不是用户按的，绝不能被当成"第二次按下"去结束会话 ——
# 否则用户按下语音键的瞬间就会被自己关掉（"按一次它掉"）。
#
# ⚠⚠ 关键：回声是**遥控器收到 MIC_OPEN 之后**才发的，而"发出去了"与
#   "遥控器真的收到了"之间能差 1 秒 —— `_send_tx` 返回只代表**把写入排进了
#   主事件循环**，真正的 GATT 写入可能慢到 1 秒才落地。
#   ⇒ 记账分两步（P2-2 之后是这样）：发出时记一条"欠一声回声"，
#     写入**真正落地**时再把窗口锚点挪过去（`_on_mic_open_done(True)`）；
#     写入**失败**则把账销掉并复位"已开麦"标记（`_on_mic_open_done(False)`）。
#   v1.0.14 就是在这儿翻的车：
#   它把窗口从 1.5 收到 0.8，而真机实测"排进队列 → 回声"最慢 1070ms
#   → 那 8 次回声漏出窗口 → 被当成第二次按下 → **按下就掉**。
#   真机日志（2026-09-23 00:18:35）：
#     Audio START → TAP(开) → 补发 MIC_OPEN → [+969ms] Audio START(回声)
#     → 又被判成真按键 → TAP(关) → 会话结束 → **用户一个字都没说上**
#   对比同一晚成功的那次（00:18:39）：补发 → 写入完成只花 20ms → 回声落在窗口内。
#
# ⇒ 所以判据不能只看"过了多久"，还要有一条**该来还没来的回声**没到：
#     pending_mic_echo > 0  且  距离那次 MIC_OPEN 不到 ECHO_MAX_AGE
#   回声一到就把这条账销掉 —— 于是"回声之后 1.3 秒用户真按键"不会被误吞
#   （旧写法只看时间窗，1.5s 内的真按键会被吞掉）。
#
# 真机实测（2026-09-23，239 次 MIC_OPEN，239 次都配上回声）：
#   中位 20ms、最大 1070ms；>0.8s 有 8 次、>1.5s **0 次**
# ⇒ ECHO_MAX_AGE = 1.5s：比实测最大值留 40% 余量。
#
# ⚠ 窗口宽度仍是**双向**代价，不许随手调：
#   · 太窄 → 回声漏过去 → 会话被自己关掉（v1.0.12 的病）
#   · 太宽 → 真按键被当成回声吞掉 → **用户当场用不了**（v1.0.13 的病）
ECHO_MAX_AGE = 1.5

# ── Logging ────────────────────────────────────────────────────────────────────
# 必须写到用户目录而不是程序目录：打包成 exe 后程序目录是 PyInstaller 的
# 临时解压路径（_MEIxxxx），退出即被清理，日志会全部丢失。
#
# 三件事交给 logsetup（2026-09-29 审查报告 P2-6）：
#   ① **轮转** 5 MB × 3 份 —— 以前是 FileHandler，只涨不换，真机上到过十几 MB；
#   ② **脱敏** 蓝牙地址只留前 2 / 后 2 字节 —— 用户排错第一件事就是把日志发出来；
#   ③ **raw HID 默认不记** —— `raw=02 42 00` 只对开发有用，且能反推按键。
from config import CONFIG_DIR
import logsetup

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
logsetup.install(CONFIG_DIR / "bridge.log")
# ⚠ 配置要在**打第一行日志之前**读出来 —— 否则启动那几行（正好含适配器地址）
#   会以明文落盘，而"脱敏开着"这件事就只是心理安慰。
#   读失败不算致命：用 logsetup 的默认值（脱敏开、raw 关），别让日志配置把程序拦住。
try:
    _boot_cfg = Config.load()
    logsetup.set_redact(getattr(_boot_cfg, "log_redact", True))
    logsetup.set_raw_hid(getattr(_boot_cfg, "log_raw_hid", False))
except Exception:                                   # noqa: BLE001
    pass
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

# 被当成"自动重开麦回响"而吞掉的 audio_start 次数。
#
# 为什么要有这个数：这类"吞掉"以前只有一行 logger.debug，正式版日志级别是 INFO
# —— 等于**一声不响地把遥控器的事件扔了**（本项目"静默丢弃"的老毛病）。
# 2026-09-22 排查「按一下、刚开口就说不成」时，最关键的那两个 audio_start
# 在日志里根本不存在，只能靠时序反推。吞掉多少、什么时候吞，必须看得见。
_echo_swallowed = 0

# 输出流（喂给 CABLE Input 那一路）的存活证据。
#
# 为什么非要这两个数：输出流是"建一次、start 一次"的，之后**没有任何监护** ——
# 一旦 PortAudio 那一路死了（WASAPI 抖动、设备被别的程序抢、睡眠唤醒），
# 回调就再也不被调用，于是 CABLE Input 永远收到静音。
# 而遥控器那一路的波形照旧在动（它走 on_audio，和输出流无关），
# 用户看到的就是「有波形、但输入法收不到声音，重启软件才好」。
# 2026-09-17 武哥报的正是这一条。
_cb_last_at  = 0.0     # 输出回调最后一次被调用的时刻
_cb_calls    = 0       # 回调总次数（隔一段时间应该涨）
_drop_frames = 0       # 因队列满而**丢掉**的音频帧数

# 遥控器**最后一次推来音频帧**的时刻。
#
# 与 `_audio_frames` 的区别很关键：那个只在 `atvv.state.stream_active` 为真时
# 才自增（`on_audio` 的早退门在它前面），所以它回答的是"这一路**我们认不认**"；
# 而这个在门**之前**就写，回答的是"遥控器**客观上**还在不在推流"。
# 主循环的"残留推流自愈"（见 ③）必须用后者 —— 要抓的正是
# "我们已经不认这段会话了、遥控器却还在推"的情形，前者那时根本不涨。
_remote_frame_last_at = 0.0

# 手动重连请求。控制台点「重新连接」时置位，主循环看到就断开重来。
# 为什么不在控制台里 Popen 一个新进程：那样会出现两个实例同时抢同一个 BLE
# 连接和同一个托盘图标，谁赢不确定，表现为"点了重连就时好时坏"。
_reconnect_request = threading.Event()

# ── 优雅停止（2026-09-29 审查报告 P1-4）──────────────────────────────────────
# 托盘点「退出」时原先只设了托盘自己的 `_stop`，而 run_bridge 的主循环
# **根本不看它** —— 桥线程是 daemon，主进程一结束就被直接掐掉，
# `finally` 里的清理（关 GattSession / 停 Frida 注入 / 关音频流）**不保证执行**。
# 后果是：重开程序时上一轮的 BLE 会话/注入可能还残着，表现为"退出再开就连不上"。
#
# 所以给主循环一个它能看见的信号，并把「连接阶段的等待」也一起叫醒。
_stop_request = threading.Event()


def request_stop() -> None:
    """请求桥线程尽快收尾退出（托盘「退出」用）。"""
    _stop_request.set()
    # 连接阶段在 `_hold_ble_connection` 里睡 0.5s 一轮，重连请求能把它叫醒
    _reconnect_request.set()


# ── 语音结束后的「自动发送」（⚠ 默认关，是可选项）────────────────────────
#
# 🔴 默认**必须是关的**（2026-09-17 武哥否掉了"默认开"）：
#   自动发送会把"还没想好的话"替用户发出去。真实场景：说到一半去上厕所，
#   回来想接着改、改完再发 —— 程序早把半截话发进真人对话里了。
#   这个代价远大于"要自己动手发一下"。**宁可不发，也不能替用户说出他还没
#   决定要说的话。**
#
# 默认流程（当前行为）：
#     按语音键（开始听）→ 说完 → 再按一次语音键（结束）→ 文字进输入框
#     → **什么时候发由你决定**：改完按键盘回车即可（手本来就在键盘上；
#       原来真正别扭的是"得去够鼠标点一下"，那一下才打断思路）。
#
# 打开这个开关之后才轮得到下面这套逻辑（四条收尾路径都在管，详见各自调用点）：
#   · 结束 → 等一小会儿（让输入法把字落进输入框）→ 替用户按发送键
#   · 开始新一段 → 取消上一条待发送
#   · 这一段一帧音频都没有 → 不发（误触；按回车会把输入框里原有内容发出去）
#   · 超时收尾（挂着忘了关）→ **永不发送**（那段时间很可能录到环境音）
#
# 为什么不像最初设想的那样"绑一个遥控器按键来发送"：遥控器除语音键以外的按键
# 走 HID 厂商页，在 Windows 上至今收不到报告（见 remote_hid.py 文件头）。
# 哪天那条路通了，把它绑成「发送」键 —— 那才是既省事、又由用户决定。
#
# 为什么状态放模块级、而不是 on_control 的闭包里：
#   on_control 可能在 _get_cfg 定义之前就被 BLE 回调线程调到（订阅是先于
#   后面那些闭包定义的），闭包会 UnboundLocalError。放模块级 + 在主循环里
#   读配置，时序上就不存在这个窗口。
_pending_send_at: float = 0.0     # 「待发送」的登记时刻（0 = 没有待发送）
_pending_send_frames: int = 0     # 结束那一次会话共收到多少音频帧


def request_voice_send(frames: int) -> None:
    """语音会话刚结束 → 登记一次「待发送」。

    真正的发送动作在主循环里做：那里读配置方便，也不在 BLE 回调线程上
    （回调线程没有 asyncio 循环，见 v1.0.3 那个坑）。
    """
    global _pending_send_at, _pending_send_frames
    _pending_send_at = time.time()
    _pending_send_frames = int(frames or 0)


def cancel_voice_send(reason: str = "") -> None:
    """取消待发送（用户又开始说下一段了 → 上一条显然不该发出去）。"""
    global _pending_send_at
    if _pending_send_at:
        _pending_send_at = 0.0
        if reason:
            logger.info("↩ 取消自动发送（%s）", reason)


def maybe_send_after_voice(cached: dict) -> None:
    """主循环每轮调用一次：到点了就替用户按下发送键。

    为什么收成一个模块级函数、而不是把逻辑摊在主循环里：
    主循环里出现 `global _pending_send_at` 会直接 SyntaxError
    （`name is used prior to global declaration` —— 循环条件先读了它）。
    把"改状态"和"读状态"都关进模块级函数，这个坑就不存在了。

    ⚠ 一帧音频都没有时不发送。那种情况多半是误触，而按回车会把输入框里
      **原有的内容**发出去 —— 比不发送更糟（等于替用户发了一条错消息）。
    """
    global _pending_send_at, _pending_send_frames
    if not _pending_send_at:
        return

    if not cached.get("send_after_voice", False):
        # ⚠ 兜底必须是 False（不发），不能是 True。
        #   "配置里读不到这一项"时的安全方向是**不发**：替用户发出一条他还没
        #   决定要发的消息是不可逆的，少发一次只是多按一下回车。
        #   （_get_cfg 正常都会填上这一项，这里防的是配置残缺/别处自定义调用。）
        _pending_send_at = 0.0                          # 开关关掉 → 直接作废
        return

    delay = max(0.05, float(cached.get("send_after_voice_delay_ms") or 800) / 1000.0)
    if time.time() - _pending_send_at < delay:
        return                                          # 还没到点，下一轮再看

    frames = _pending_send_frames
    _pending_send_at = 0.0
    _pending_send_frames = 0

    if frames <= 0:
        logger.info("📨 本次语音没有任何音频帧 → 判定为误触，不发送")
        return

    key = cached.get("send_after_voice_key") or "enter"
    if tap_key(key):
        logger.info("📨 已发送（替用户按了 %s）", key)
    else:
        logger.error(
            "❌ 发送失败：%r 这个键名发不出去（看 config.json 的 send_after_voice_key）",
            key,
        )


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


# ── 混音软限幅 ────────────────────────────────────────────────────────────────
# 混音是 `remote×增益 + sys×增益` **直接相加**，加完硬削顶到 int16 的 [-32768, 32767]。
#
# 🔴 2026-09-29 真机实测：遥控器**解码后的原始峰值就已经是 32768（满量程）**，
#    而用户配置里 `gain = 10.0` —— 放大 10 倍再削顶，正常说话基本被切成方波。
#    削顶会产生大量高次谐波，语音识别在这种波形上明显退化，用户看到的就是
#    「语音输入时好时坏、有时候一个字都识别不对」。
#
# 软限幅：拐点 `_LIMIT_KNEE` 以下**严格线性**（小信号一点不动），以上渐进压向满量程，
# 数学上永远到不了 32767，因此不再有硬削顶。代价只是大音量时轻微压缩 —— 比削顶好得多。
_LIMIT_KNEE = 0.70       # 归一化拐点：|x| ≤ 0.70 原样通过
_FULL_SCALE = 32767.0


def _soft_limit(x: float) -> float:
    """把归一化样本软限幅到 (-1, 1)。拐点以下严格线性，小信号不被染色。"""
    a = -x if x < 0 else x
    if a <= _LIMIT_KNEE:
        return x
    over = (a - _LIMIT_KNEE) / (1.0 - _LIMIT_KNEE)
    y = _LIMIT_KNEE + (1.0 - _LIMIT_KNEE) * (1.0 - math.exp(-over))
    return -y if x < 0 else y


# 限幅占比统计窗口：每约 1 秒结算一次并写进 state 给控制台看。
# 这是"增益是不是开太大"的**唯一客观依据** —— 以前只能靠耳朵猜。
_limit_win = {"n": 0, "hit": 0, "at": 0.0, "warn_at": 0.0}


# ── Audio stream ───────────────────────────────────────────────────────────────
def _create_stream(out_dev: int, sample_rate: int, sysmic=None):
    """输出流：把「遥控器麦克风」和「电脑麦克风」按各自的增益/静音/独奏相加后写出去。

    为什么输出流要**一直开着**（而不是只在遥控器推流时开）：
    电脑麦克风那一路是持续的，关掉输出流等于把房间里的声音也一起掐了 ——
    微信就完全听不见你说话了，那还不如不装这个软件。
    """
    import sounddevice as sd

    def cb(outdata, frames, timeinfo, status):
        global _cb_last_at, _cb_calls
        # 存活心跳：主循环靠它判断"这路流还活着吗"，见 _supervise_audio。
        # 必须放在最前面 —— 就算下面混合逻辑抛异常，心跳也得先记上，
        # 否则一个 bug 会让看门狗误判成"流死了"而去反复重建。
        _cb_last_at = time.time()
        _cb_calls += 1
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
        if sysmic is not None:
            # ⚠⚠ 无论这一路**是否发声**，都要按一比一的比例把队列消费掉 ——
            #   与下面遥控器那一路是同一条规矩（见 `if not _pending_samples:` 的注释）。
            #   老写法是"增益为 0 就**不取数**"（`s_gain > 0.0` 写在 if 里），
            #   于是静音 / 被别人独奏压掉期间队列原地积压，一路涨到 maxsize
            #   （2 秒）为止，`put_nowait` 开始抛 queue.Full（静默丢弃）；
            #   解除静音的那一刻，先播出来的是**2 秒前的旧音频** —— 听感是
            #   "回声/串音"，而语音识别拿到的是错位的音轨。
            #   现在不发声时用 skip() 把数据丢掉：队列永不积压，
            #   重新出声时听到的就是当下的声音。
            #   （2026-09-29 审查报告 P0-2 的后半条。）
            try:
                if s_gain > 0.0:
                    sys_blk = sysmic.read(frames)
                else:
                    sysmic.skip(frames)
            except Exception:                      # noqa: BLE001
                sys_blk = None
        sys_len = len(sys_blk) if sys_blk is not None else 0

        peak = 0
        lim_hit = 0
        for i in range(frames):
            v = 0.0
            # ⚠⚠ 无论这一路**是否发声**，都要按一比一的比例把队列消费掉。
            #
            # 老写法是"只在 r_gain > 0 时才取"，于是遥控器那一路静音/未参与混音的
            # 期间（source_audible 为假、或增益滑到 0）队列**一点都不消费**：
            #   · 涨到 maxsize=65536 之后，on_audio 的 put_nowait 抛 queue.Full，
            #     后面**每一帧都被丢掉**，而日志里一个字都没有（"静默丢弃"老坑）；
            #   · 重新打开声音时，先播出来的是几分钟前积压的旧音频。
            # 症状正好是武哥 2026-09-17 报的「波形在动、输入法收不到声音，重启才好」。
            # 现在改成每帧必取一个：队列永远不会积压，重新出声时也是当下的声音。
            if not _pending_samples:
                try:
                    _pending_samples.extend(_sample_queue.get_nowait())
                except queue.Empty:
                    pass
            if _pending_samples:
                s = int(_pending_samples.popleft())
                if r_gain > 0.0:
                    v = s * r_gain
            if i < sys_len:
                v += float(sys_blk[i]) * s_gain

            # ⚠ 这里**不要**再写 `int(v)` + 手写 if 削顶。
            #   那是硬削顶：超过满量程就把波形切平，产生大量高次谐波。
            #   真机上遥控器解码峰值本来就能到 32768，乘上 gain=10 之后
            #   整段都被切平 —— 语音识别时好时坏就是这么来的。
            #   改成软限幅后永远到不了满量程，小信号（拐点以下）一点没变。
            x = v / 32768.0
            if x > _LIMIT_KNEE or x < -_LIMIT_KNEE:
                lim_hit += 1
            iv = int(_soft_limit(x) * _FULL_SCALE)
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

        # ── 限幅占比：每约 1 秒结算一次 ──────────────────────────────────
        # 这是"增益是不是开太大"的**唯一客观依据**。以前只能靠耳朵猜，
        # 用户报"时好时坏"时也没有任何数字可看。
        _limit_win["hit"] += lim_hit
        _limit_win["n"] += frames
        now_cb = time.time()
        if now_cb - _limit_win["at"] >= 1.0 and _limit_win["n"] > 0:
            pct = 100.0 * _limit_win["hit"] / _limit_win["n"]
            state.update(mix_limit_pct=round(pct, 1))
            # 只在真的压得厉害时提醒，且 30 秒最多一次 —— 别刷屏
            if pct >= 20.0 and now_cb - _limit_win["warn_at"] >= 30.0:
                _limit_win["warn_at"] = now_cb
                logger.warning(
                    "🔺 混音里有 %.0f%% 的采样在被限幅（拐点 %.2f）—— 增益开太大了。\n"
                    "   遥控器解码峰值本来就能顶到满量程，再乘大增益会整段被压平，"
                    "语音识别在这种波形上会明显变差。\n"
                    "   处置：控制台 → 音频 → 把「麦克风增益」调小（先试 2~3，"
                    "看着「混合输出」的电平不顶格即可）。", pct, _LIMIT_KNEE)
            _limit_win["n"] = 0
            _limit_win["hit"] = 0
            _limit_win["at"] = now_cb

    return sd.OutputStream(
        device=out_dev, channels=1, dtype="int16",
        samplerate=sample_rate, blocksize=240, callback=cb,
    )


# ── BLE 链路建立 ───────────────────────────────────────────────────────────────
async def _hold_ble_connection(ble, timeout: float = 20.0, stop=None):
    """主动把 BLE 链路拉起来，并尽量维持住。

    为什么不能只"看一眼 connection_status"：
    WinRT 的 `BluetoothLEDevice.from_id_async()` **只取回设备对象，不发起连接**。
    链路要等下面三件事之一发生才会真的建立：
      ① 遥控器自己醒来广播（用户按键）；
      ② 我们访问一次 GATT（get_gatt_services_async）；
      ③ 建一个 GattSession。

    老代码只依赖 ①，而且只等 10×0.5s = **5 秒**。2026-09-28 实测：这台机器上
    从"设备对象就绪"到 connection_status 变成 1 需要约 **10 秒** —— 5 秒窗口
    必然判失败，于是每轮重连都在半路放弃、下一轮又重新 from_id_async 把进度清零，
    日志里只剩「Connection failed」，看着像"遥控器没醒"，其实是**等得不够久**。

    返回 (是否已连接, GattSession 或 None)。
    ⚠ 调用方**必须持有**返回的 GattSession —— 它被 GC 回收时链路会跟着断。
    """
    from winrt.windows.devices.bluetooth import BluetoothConnectionStatus

    sess = None

    # ① 先建 GattSession 并声明「我要维持连接」。
    #    这是 WinRT 里唯一能让 Windows 主动保持 BLE 链路的手段：
    #    没有它，系统空闲时会把链路断掉，遥控器随即进入睡眠
    #    —— 也正是老代码注释里那个「ATVV 静默 ≠ 链路断了」的老问题。
    try:
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattSession
        bdid = getattr(ble, "bluetooth_device_id", None)
        if bdid is not None:
            sess = await GattSession.from_device_id_async(bdid)
            if sess is not None and getattr(sess, "can_maintain_connection", False):
                sess.maintain_connection = True
                logger.info("🔗 已请求维持 BLE 连接（GattSession.maintain_connection）")
    except Exception as e:                       # noqa: BLE001
        # 拿不到 GattSession 不影响后面 ②③ —— 降级即可，不要因此中断连接。
        logger.warning("⚠️  建立 GattSession 失败（降级继续）：%r", e)

    if ble.connection_status == BluetoothConnectionStatus.CONNECTED:
        return True, sess

    # ② 触发一次 GATT 访问 —— 只读 connection_status 不会让系统去连。
    try:
        await ble.get_gatt_services_async()
    except Exception as e:                       # noqa: BLE001
        logger.debug("触发连接时 get_gatt_services_async 抛异常（可忽略）：%r", e)

    # ③ 等链路真正建起来。实测约 10 秒，这里给 20 秒余量。
    #    ⚠ 每轮都要看 stop：用户点了「退出」不该还要在这儿干等 20 秒
    #      （托盘 join 有超时，等超了就会把清理直接掐掉 —— 那正是 P1-4 要修的）。
    waited = 0.0
    while waited < timeout:
        if stop is not None and stop.is_set():
            logger.info("🛑 连接等待期间收到退出请求 → 提前结束")
            break
        if ble.connection_status == BluetoothConnectionStatus.CONNECTED:
            return True, sess
        await asyncio.sleep(0.5)
        waited += 0.5

    return ble.connection_status == BluetoothConnectionStatus.CONNECTED, sess


# ── Main bridge ────────────────────────────────────────────────────────────────
class _BridgeResources:
    """一次 run_bridge 里所有「需要收尾」的句柄 —— 2026-09-29 审查报告 P1-3。

    ⚠ 为什么要有这个类
    ------------------
    原先的 `try/finally` 只包住**主循环**，而**建立阶段**（连 BLE → 找 ATVV
    服务 → 找三个特征 → 订阅通知 → 起音频流 → 装键盘钩子 → 注入 Frida）中间
    有十来条 `return False`。那些路**直接绕过 finally**：

      · `GattSession.maintain_connection` 还举着 —— Windows 会一直拽着这条链路；
      · `BluetoothLEDevice` 没 close；
      · Frida 注入的会话还挂在 WUDFHost 里；
      · 音频流没停。

    全靠 GC 回收，而 GC 什么时候跑、跑不跑得到都不确定。现象是"重连之后
    第一次总是失败"，日志里还看不出原因。

    现在改成：**从拿到第一个句柄起就进入同一个 finally**。`run_bridge` 只剩
    一层薄壳，真正的活儿在 `_run_bridge_inner`；不管它是正常返回、中途
    `return False`、还是抛异常，收尾都走同一个 `teardown()`。
    """

    def __init__(self) -> None:
        self.ble = None              # BluetoothLEDevice
        self.gatt_session = None     # GattSession（维持连接那个，不能被 GC 回收）
        self.atvv = None             # ATVVProtocol
        self.coord = None            # SessionCoordinator
        self.tx_char = None
        self.audio_char = None
        self.ctl_char = None
        self.aud_token = None
        self.ctl_token = None
        self.stream = None           # sd.OutputStream
        self.sysmic = None           # mixer.SystemMic
        self.hid_buttons = None      # remote_hid.RemoteHidButtons
        self.hid_tap = None          # frida_hid.RemoteHidTap
        self.kb = None               # keyboard 模块（装了钩子才非 None）
        self.voice_active = False
        self.ran_ok = False
        # ⚠ 「统一收尾」那个入口（`_run_bridge_inner` 里的局部函数 `end_voice_session`）。
        #    **必须**由建立阶段显式登记进来 —— teardown 在**模块级类**里，
        #    那里根本没有这个名字（局部函数不是全局名），直接写裸名会被解析成
        #    全局查找 → 运行期 NameError → 被 teardown 的 `except Exception`
        #    吞成一行 warning ⇒ **退出/断连时那条收尾路径整条没跑**
        #    （现象：只发了一行"退出前收尾异常"的告警，而相位、UI 状态、
        #     待发送登记全都没归位）。2026-09-29 审查报告 P2 排查时发现。
        self.end_voice_session = None

    async def teardown(self) -> None:
        """收尾。**每一步都自己 try 住** —— 某一步失败不能让后面的清理不跑。"""
        # 安全兜底：退出/断线时绝不能把快捷键按着不放 ——
        # 否则 Ctrl / Win 会一直处于按下状态，整台电脑的键盘都会不正常。
        try:
            hotkey_up()
        except Exception:
            pass

        # ── 第 5、6 条收尾路径：断连 / 退出 ────────────────────────────────
        # ⚠ 退出前**必须**命令遥控器关麦。不发的后果：程序没了，遥控器还以为
        #   会话开着，继续推流 —— 用户看到的是"软件都关了，遥控器还在收音"，
        #   而且它推的流没人接，只能等遥控器自己超时。
        #
        # ⚠ 这里**不能只调 end_voice_session()**：它的 `_send_tx` 只是把协程
        #   投递到事件循环，而我们此刻**就在**这个循环的收尾里 —— run_bridge
        #   一返回、asyncio.run 一收尾，那条还没跑到的写入就被取消了：
        #   **MIC_CLOSE 静默丢失**，日志上还写着"已发出"。
        #   所以这里自己 await 一次写入，确认它真的落到 BLE 上（最多等 1 秒，
        #   超时就放弃 —— 退出不能被一条蓝牙写入卡住）。
        was_streaming = bool(
            (self.atvv is not None and self.atvv.state.stream_active)
            or self.voice_active
        )
        try:
            if was_streaming:
                logger.info("🎙️ 语音会话【结束】（断开/退出）")
            if self.atvv is not None and self.coord is not None:
                # ⚠ 走登记进来的那个入口，**不要**写裸名 —— 见 __init__ 里的注释：
                #   裸名在这里解析成全局查找，模块顶层没有这个名字，运行期 NameError，
                #   被下面那个 except 吞掉 ⇒ 收尾静默失效。
                if self.end_voice_session is not None:
                    self.end_voice_session("断开或退出", send=False)
                else:
                    logger.warning(
                        "退出前收尾入口未登记（res.end_voice_session 为空）—— "
                        "跳过统一收尾，只发 MIC_CLOSE"
                    )
        except Exception as e:                          # noqa: BLE001
            logger.warning(f"退出前收尾异常（继续清理）：{e}")
        if was_streaming and self.tx_char is not None and self.coord is not None:
            try:
                from winrt.windows.storage.streams import DataWriter
                _w = DataWriter()
                _w.write_bytes(self.atvv.mic_close_cmd(self.coord.state.stream_id))
                await asyncio.wait_for(
                    self.tx_char.write_value_with_result_async(_w.detach_buffer()),
                    timeout=1.0,
                )
                logger.info("📤 MIC_CLOSE（退出前）已发出 —— 遥控器不会继续推流")
            except Exception as e:                      # noqa: BLE001
                logger.warning(f"退出前补发 MIC_CLOSE 失败（遥控器可能还会推一会儿）：{e}")

        # ⚠ 会话协调器**作废**：取消它排出去的所有定时器，之后入口一律 no-op。
        #   放在这里（MIC_CLOSE 已经补发完）而不是更早：早于补发的话，
        #   `end_voice_session` 里的 `session.close()` 会被挡掉。
        #   为什么必须有这一步（P2-3）：`close()` 只收尾"这一次会话"，
        #   对象还活着；而重连/退出时它会被丢掉，排出去的定时器却还挂在
        #   threading 里 —— 到点 `_retry_open()` 会把相位推回 OPENING 并
        #   **再发一次 MIC_OPEN**，此时链路可能已经换了一条。
        if self.coord is not None:
            try:
                self.coord.shutdown()
            except Exception:                           # noqa: BLE001
                pass

        state.reset()
        # 松开「维持连接」的请求，让 Windows 可以正常休眠这条链路；
        # 不显式清掉的话，托盘退出后遥控器会被系统一直拽着不放。
        if self.gatt_session is not None:
            try: self.gatt_session.maintain_connection = False
            except Exception: pass
            try: self.gatt_session.close()
            except Exception: pass
        if self.sysmic is not None:
            try: self.sysmic.stop()
            except Exception: pass
        if self.stream is not None:
            try: self.stream.stop(); self.stream.close()
            except Exception: pass
        if self.ctl_char is not None and self.ctl_token is not None:
            try: self.ctl_char.remove_value_changed(self.ctl_token)
            except Exception: pass
        if self.audio_char is not None and self.aud_token is not None:
            try: self.audio_char.remove_value_changed(self.aud_token)
            except Exception: pass
        if self.kb is not None:
            try: self.kb.unhook_all()
            except Exception: pass
        if self.hid_buttons is not None:
            try: self.hid_buttons.stop()
            except Exception: pass
        if self.hid_tap is not None:
            try: self.hid_tap.stop()
            except Exception: pass
        if self.ble is not None:
            try: self.ble.close()
            except Exception: pass
        logger.info("🧹 Cleanup done")


async def run_bridge(device_type: str | None = None, name_hint: str | None = None,
                     stop_event: "threading.Event | None" = None):
    """跑一轮桥。返回 True 表示"这一轮正常结束"（含收到退出请求）。

    ⚠ 这里只是一层**壳**：真正干活的是 `_run_bridge_inner`。这样不管它是
    正常返回、中途 `return False`、还是抛异常，收尾都走同一个 `teardown()`
    （2026-09-29 审查报告 P1-3）。

    `stop_event`：外部（托盘）要求退出的信号。**必须能被主循环看见** ——
    否则托盘点「退出」时桥线程是被直接掐掉的，清理不保证跑（P1-4）。
    """
    res = _BridgeResources()
    try:
        res.ran_ok = await _run_bridge_inner(device_type, name_hint, stop_event, res)
        return res.ran_ok
    finally:
        await res.teardown()


async def _run_bridge_inner(device_type: str | None = None,
                            name_hint: str | None = None,
                            stop_event: "threading.Event | None" = None,
                            res: "_BridgeResources | None" = None):
    """真正干活的那一层。收尾**不在这里** —— 见 `_BridgeResources.teardown`。"""
    if res is None:                       # 允许单独调用（诊断/测试）
        res = _BridgeResources()
    stop = stop_event if stop_event is not None else _stop_request
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
    # 远端 MAC —— 后面 Frida 旁路挑驱动宿主时用它做"就是这台"的硬证据
    # （同型号的第二只遥控器 VID/PID 完全一样，只有 MAC 分得开）。
    _, _remote_addr = _addr_from_ble_id(dev_info.id)

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
    res.ble = ble                       # P1-3：立刻登记，失败路径也能被收尾

    # ⚠ 必须把 GattSession 存成局部变量：run_bridge 的栈一直活着，
    #   它就不会被 GC 回收，链路也就能一直维持住。
    ble_session = None
    if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
        logger.warning("⚠️  Not connected yet — remote may be sleeping. Press any button to wake.")
    connected, ble_session = await _hold_ble_connection(ble, stop=stop)
    res.gatt_session = ble_session      # P1-3：同上，它是"举着链路"的那个
    if not connected:
        if stop.is_set():
            logger.info("🛑 收到退出请求 → 不再重试连接")
            return True
        logger.error(
            "❌ Connection failed —— 20 秒内没能建立 BLE 链路。\n"
            "   处置：按一下遥控器上任意一个键把它唤醒（别只按一次就等），\n"
            "   然后点托盘菜单里的「重新连接」。"
        )
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
    # P1-3：逐个登记（不写成元组赋值 —— 闸门按 `res.<名字> =` 逐个核，
    # 元组形式会让它数不到，而"数不到"就等于"以后漏了也发现不了"）
    res.tx_char = tx_char
    res.audio_char = audio_char
    res.ctl_char = ctl_char
    logger.info("✅ ATVV characteristics ready")

    # ── Protocol + Session ──
    atvv = ATVVProtocol()
    res.atvv = atvv

    def _send_tx(cmd: bytes, tag: str = "TX", on_done=None) -> bool:
        """Write raw bytes to the ATVV TX characteristic (fire-and-forget).

        返回 True = **已成功投递到主事件循环**（写入本身异步进行）。

        ⚠⚠ 返回值**不是**"写成功了" —— 2026-09-29 审查报告 P2-2 说的就是这件事。
        要知道真实结果，必须给 `on_done`：

        `on_done(ok)`：GATT 写入**有结果**时回调一次（成功 True / 失败 False），
        跑在**主事件循环线程**上。`cmd` 非空时**保证恰好调用一次** ——
        三条路都会调到：异步写入拿到结果、写入抛异常、以及"连排队都没排上"
        （最后这条是**同步**调用，就在调用方的线程上）。

        为什么"投递成功"不能当"写入成功"用：
        `run_coroutine_threadsafe` 只是把协程排进队列，真正的 GATT 写可能
        几百毫秒后才失败（链路抖动、遥控器走远、特征被注销）。老代码把
        "排上了"当成功返回，于是**状态机按"已经开麦"记账**：
          · `session.state.mic_open_sent = True` ⇒ 后面的 `ensure_mic_open()`
            一律直接 return，**再也不重发**；
          · `pending_mic_echo` 也记了一笔"欠一声回声"，而回声永远不会来
            ⇒ 窗口一直挂着，下一条真按键有可能被误吞。
        现象是"按了没反应"，日志上还写着"补发成功"。
        现在把真实结果交给调用方回滚（见 `_on_mic_open_done`）。

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

        def _fire(ok: bool) -> None:
            """回调失败不能把写入的结果吞掉 —— 分开 try。"""
            if on_done is None:
                return
            try:
                on_done(ok)
            except Exception as e:                    # noqa: BLE001
                logger.error(f"{tag} on_done 回调失败: {e}")

        async def _write():
            try:
                w = DataWriter()
                w.write_bytes(cmd)
                r = await tx_char.write_value_with_result_async(w.detach_buffer())
                logger.info(f"📤 {tag} [{cmd.hex(' ')}] status={r.status}")
                ok = r.status == GattCommunicationStatus.SUCCESS
                if not ok:
                    logger.warning(f"⚠ {tag} 写入未成功：status={r.status}")
                _fire(ok)
                return ok
            except Exception as e:
                logger.error(f"{tag} write failed: {e}")
                _fire(False)
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
            _fire(False)          # 保证 on_done 恰好被调用一次
            return False

    # ── 回声窗口的记账（见 ECHO_MAX_AGE 的注释）────────────────────────────
    def _mark_mic_open_scheduled():
        """我们**发了一次** MIC_OPEN —— 从现在起该来一声回声了。"""
        nonlocal pending_mic_echo, mic_echo_since
        pending_mic_echo += 1
        mic_echo_since = time.time()

    def _undo_mic_open_scheduled(reason: str):
        """把"欠一声回声"那笔账销掉 —— 前提是这声回声**确实不会来了**。

        ⚠ 销账的时机有两种，都在 `_on_mic_open_done` 那条路上：
          · 写入失败（含"连排队都没排上"）；
          · 其余情况一律**不销** —— 宁可多留一笔，也别把真按键的回声窗口
            提前关掉（那会让回声被当成真按键、把会话关掉）。
        """
        nonlocal pending_mic_echo
        if pending_mic_echo > 0:
            pending_mic_echo -= 1
        logger.warning("⚠ %s → 销掉一笔「欠一声回声」的账（当前还欠 %d 声）",
                       reason, pending_mic_echo)

    def _on_mic_open_done(ok: bool):
        """MIC_OPEN 的**真实结果**（2026-09-29 审查报告 P2-2）。

        跑在主事件循环线程上（"连排队都没排上"那条是同步调用）。

        ok=True  → 写入真的落到 BLE 了：把回声窗口锚点挪到这一刻。
                   "排队"与"落地"能差 1 秒，回声跟着差 1 秒（v1.0.14
                   就是拿排队时刻当锚点，窗口才老是漏）。
        ok=False → 这一声回声**永远不会来**：
                   ① 销掉那笔账（不销的话窗口一直挂着，下一条真按键可能被吞）；
                   ② 把 `mic_open_sent` 复位 —— **这条最要紧**：不复位的话
                      `ensure_mic_open()` 会一直以为已经开过麦而直接 return，
                      遥控器**一帧音频都不会推**，而日志上只有一行"补发成功"。
        """
        nonlocal mic_echo_since
        if ok:
            # 只挪时刻、**不加计数**：这是同一次 MIC_OPEN 的第二个事实，
            # 不是新的一次。写成 `+= 1` 会让账永远销不完。
            # 只在"还欠着回声"时挪：否则一次迟到的回调会把已销完账的窗口重新拉开。
            if pending_mic_echo > 0:
                mic_echo_since = time.time()
            return
        _undo_mic_open_scheduled("MIC_OPEN 未真正写入")
        # ⚠ 走协调器的方法，不直接改 `session.state.mic_open_sent`：
        #   那个字段是状态机的一部分，必须和相位迁移在同一把锁里改（P2-3）。
        if session.mark_mic_open_failed():
            logger.warning(
                "⚠ MIC_OPEN 真实写入失败 → 已撤销「本次已开麦」标记，"
                "下次按下 / 松手会重发（否则遥控器一帧音频都不会推）"
            )

    def _on_mic_open(sid: int):
        """Host 主动开麦 — 必须真正写入 BLE，否则遥控器不会推流。

        返回 None = 没排上（构造失败或投递失败），调用方据此不置 mic_open_sent。

        ⚠ MIC_OPEN 是**唯一**会引来回声的动作，所以"该来一声回声"这笔账
          在这里记，而且只在真的排上之后记 —— 没排上就不会有回声，
          记了账反而会让下一条真按键被吞（v1.0.13 就是这么全废的）。

        ⚠⚠ 顺序（P2-2）：**先记账、再排队**。反过来的话有个竞态 ——
          `_send_tx` 一返回，主循环那边可能**立刻**就写完并回调
          `_on_mic_open_done(False)`（销账），而这一边还没来得及 `+= 1`：
          销账销在了记账之前，账上就永远挂着 1 笔"欠一声回声"，
          下一条真按键可能被误吞。先记账再排队，回调最早也只能在记账之后发生。

        ⚠ 这里**不**在 `_send_tx` 返回 False 时自己销账 —— `on_done` 保证
          恰好被调用一次（含"连排队都没排上"那条同步路），销账只放在
          `_on_mic_open_done` 一处，免得两条路各销一次、账被销成负数。
        """
        try:
            cmd = atvv.mic_open_cmd()
        except Exception as e:
            logger.error(f"mic_open_cmd failed: {e}")
            return None
        if not cmd:
            return None
        _mark_mic_open_scheduled()
        if not _send_tx(cmd, "MIC_OPEN", on_done=_on_mic_open_done):
            return None
        return cmd

    def _on_mic_close_done(ok: bool):
        """MIC_CLOSE 的**真实结果**（P2-2）。

        失败时**必须如实说**：`end_voice_session` 那行日志写的是"已发出"，
        而它其实是"排上了"。遥控器没收到 MIC_CLOSE 就会**继续推流** ——
        用户看到的是"程序里已经结束了，遥控器还在收音"。
        这里把它点破，主循环的「残留推流自愈」（③）才有据可依。
        """
        if not ok:
            logger.warning(
                "⚠ MIC_CLOSE 真实写入失败 —— 遥控器可能还会继续推一会儿；"
                "主循环的「残留推流自愈」会在 5 秒内补发一次"
            )

    def _on_mic_close(sid: int):
        try:
            cmd = atvv.mic_close_cmd(sid)
        except Exception as e:
            logger.error(f"mic_close_cmd failed: {e}")
            return None
        if not cmd:
            return None
        return cmd if _send_tx(cmd, "MIC_CLOSE", on_done=_on_mic_close_done) else None

    session = SessionCoordinator(
        on_mic_open=_on_mic_open,
        on_mic_close=_on_mic_close,
        on_phase=lambda phase, reason: logger.debug(f"Phase: {phase.value}  ({reason})"),
    )
    session.set_mode(cfg.voice_mode)
    res.coord = session

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
    res.voice_active = False
    voice_started_at = 0.0     # 本次会话开始时刻（超时兜底用）
    # 「残留推流自愈」上一次补发 MIC_CLOSE 的时刻（冷却用，见主循环 ③）。
    _residual_close_at = 0.0
    # 「我们发过 MIC_OPEN、但还没等到它那一声回声」的条数与时刻（防自激用）。
    # 判据见 ECHO_MAX_AGE 那段注释 —— 不能只看时间，还要看这笔账还没销。
    pending_mic_echo = 0
    mic_echo_since   = 0.0
    ctl_token = aud_token = None
    stream    = None
    sysmic    = None
    # 认不出来的控制指令 / 键名，各自只报一次（去重集合）。
    # 见 on_control 与 _on_key 里的注释：这两个洞让"按键没反应"永远查不出结论。
    _unknown_ops: set[int] = set()

    # ── 能力响应（CAPS）到达信号 —— 2026-09-29 审查报告 P2-1 ─────────────────
    #
    # 为什么需要它：采样率是**遥控器**在 CAPS 里告诉我们的（ADPCM 8k / 16k），
    # 而 `atvv.state.sample_rate` 在响应到达之前是**默认值 16000**。
    # 老代码发完 GET_CAPS 就往下走、立刻按 16000 建音频流 ——
    # 协商出来是 8k 的遥控器就会**按错速率**建流（音频变调/变速），
    # 而这件事不会报错，只会"听起来不对"。
    #
    # 用 threading.Event 而不是 asyncio.Event：它由 **BLE 通知回调线程**
    # 里的 on_control 置位，asyncio.Event.set() 不是线程安全的。
    # 等待放在 executor 里跑（见下面 `caps_ready.wait(...)`），不占事件循环。
    caps_ready = threading.Event()

    # ── 语音会话收尾：**唯一**入口 ────────────────────────────────────────────
    def end_voice_session(reason: str, *, send: bool = False) -> bool:
        """结束语音会话，把"会话已结束"这件事**在每一层**同时落实。

        ⚠⚠ 为什么必须收成一个函数（2026-09-29 审查报告 P0-1）

        收尾有 **6 条**路径：
            ① 再按一次语音键（toggle 翻转）        ② 键盘确认键（Enter）
            ③ 厂商页/Frida 确认键                  ④ 600 秒超时兜底
            ⑤ BLE 断连                            ⑥ 程序退出
        原先这 6 处各写各的，做的事**还都不一样**：有的只撤 UI、
        有的连 UI 都不撤。而"让遥控器停止推流"这件事（发 MIC_CLOSE +
        撤 `atvv.state.stream_active`）**一处都没做** —— 于是：

            真机实证 2026-09-29 11:26:34,435 「语音会话【结束】（确认键）」
            之后，日志里再无任何 MIC_CLOSE，音频帧从 600 一路涨到 3761
            （11:27:25 仍在涨）—— **多推了 52 秒**。
            用户感受：明明结束了，遥控器还在收音；紧接着再按语音键，
            新会话的帧数统计和"零帧误触保护"全被上一段的残流污染。

        这正是本项目反复踩的同一个坑：**同一套机制在多条路径都要落地，
        漏一条等于没有**。所以收尾只留这一个入口，6 条路径全部改走它。

        参数
        ----
        reason : 写进日志与 UI 的结束原因（如"确认键"）。
        send   : 是否登记「自动发送」。**默认 False** —— 安全方向是不发，
                 只有"用户明确表示说完了"的那两条路径（再按语音键 / 确认键）
                 才传 True；超时、断连、退出传 False（那几段音频不该替用户发出去）。

        返回 True 表示"调用之前会话确实开着"。
        """
        nonlocal voice_active, voice_started_at, pending_mic_echo, mic_echo_since
        was_active = voice_active

        # ① 释放输入法热键（幂等，重复调用没有副作用）。
        #    放在最前面：万一下面任何一步抛异常，也绝不能把 Ctrl/Win 按着不放 ——
        #    那会让整台电脑的键盘都不正常。
        voice_hotkey_up()

        # ② 命令遥控器关麦。**必须在清状态之前**，而且要**不看相位**地发。
        #    这是让遥控器真正停止推流的唯一手段。
        closed = session.close(f"会话收尾：{reason}")

        # ③ 立刻撤掉 ATVV 的推流许可。
        #    ⚠ 只发 MIC_CLOSE 不够：`on_audio()` 与 `decode_audio()` 都先看
        #      `stream_active`。不置 False，遥控器在"真正停下来"之前推的帧
        #      照样被喂进 UI 和混音队列（波形继续动、「遥控器麦克风」卡片继续
        #      显示有声音），`_audio_frames` 也会继续涨 —— 污染下一段的统计。
        atvv.state.stream_active = False

        # ④ 撤 UI 状态：streaming / remote_level_db / 遥控器波形，一次清干净。
        #    ⚠ 顺序铁律：这一句必须在 `voice_active = False` **之前**。
        #      反过来会留一个"voice_active=False 而 streaming 还是 True"的窗口，
        #      主循环那一轮的"状态归位"检查正好撞上 → 多打一条归位日志，
        #      结果没错、但会把下一个查日志的人带偏。
        state.end_session(f"语音结束（{reason}）")

        # ⑤ 清会话记账。
        #    `pending_mic_echo` 必须一起清 —— 留着它，下一段语音的第一条真按键
        #    会被判成"回给 MIC_OPEN 的回声"当场吞掉：不崩、不报错、UI 波形还在跳，
        #    就是"按一下没反应"（v1.0.13 那个坑的变体）。
        voice_active     = False
        res.voice_active = False      # P1-3：收尾时要能判断"是不是还在推流"
        voice_started_at = 0.0
        pending_mic_echo = 0
        mic_echo_since   = 0.0

        # ⑥ 自动发送：按策略登记或取消（真正的发送动作在主循环里做）。
        if send:
            request_voice_send(_audio_frames)
        else:
            cancel_voice_send(f"会话收尾（{reason}）")

        # ⚠ 这里只报「机械部分」（命令发没发出去、状态撤没撤），
        #    "是哪条路径收的尾"由**调用点**自己那行 `🎙️ 语音会话【结束】（…）` 负责。
        #    分两层的原因：收尾机制只有一份（改一处就全对），而"谁触发的"
        #    天然属于调用点。tools/check_send_after_voice.py 也靠调用点那行锚点
        #    逐个核对 6 条路径有没有接上。
        logger.info(
            "🔚 会话收尾（%s）：MIC_CLOSE %s · stream_active=False · 状态已归位",
            reason,
            "已发出" if closed else "⚠ 未发出（遥控器可能还会推一会儿）",
        )
        return was_active

    # ⚠ 登记给收尾用：teardown 是**模块级类**的方法，看不见这里的局部函数。
    #   不登记的话那行裸名会变成运行期 NameError，被 except 吞掉（P2 排查时发现）。
    res.end_voice_session = end_voice_session

    def on_control(sender, args):
        nonlocal last_ble_activity, last_start_search, voice_active, voice_started_at
        nonlocal pending_mic_echo, mic_echo_since
        global _audio_frames, _audio_peak, _echo_swallowed
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
                    # 唤醒"等能力响应再建音频流"那条路（P2-1）。
                    # 放在 if caps 里面：解析失败（caps=None）时不能放行 ——
                    # 否则会拿着默认 16000 去建流，跟"没等"是一个效果。
                    caps_ready.set()
            elif event["type"] == "audio_start":
                session.on_audio_start(event["codec"], event["stream_id"])
                atvv.state.decoder.reset()
                _pending_samples.clear()
                logger.info("▶ Audio START")
                state.clear_audio()          # 清掉上一次的波形，UI 从空开始画
                # ⚠⚠ 这里**不许**写 state.update(streaming=True)！
                #   `audio_start` 有两种含义（用户按下语音键 / 遥控器回给 MIC_OPEN 的
                #   回声），而"是哪种"要到下面才算得出来。写在这一行 = **回声也会把
                #   「语音中」点亮**：会话刚结束又来一条回声 → 状态被重新点亮，而唯一
                #   能清掉它的那条 audio_stop 可能永远不来 → 控制台/托盘一直显示
                #   "还在收音"，直到断开重连。
                #   （2026-09-29 报的正是这个；日志里 2026-09-24 10:33:59 有一次实证：
                #    `【结束】（确认键）` 之后 95ms 一条被吞掉的回声把状态点亮了。）
                #   ⇒ streaming 只许由**真正开了会话**的那个分支点亮（见下面
                #     `elif not voice_active:` 里的那句），并且必须与 voice_active 一致。
                # ── 先判「这一下是不是我们自己引来的回声」，**再**决定补不补开麦 ──
                # ⚠⚠ 顺序绝对不能反 —— v1.0.13 就是在这儿翻的车：
                #   那一版把这句补发**无条件**挂在 audio_start 分支的顶上，而
                #   `ensure_mic_open()` 真的发出 MIC_OPEN 时，调用方会顺手记
                #   `mic_reopen_at = 现在` → 紧接着的判定拿这个**刚写下的时刻**去比
                #   → **这一次（真按键）自己把自己判成了回声**、当场吞掉，热键不注入。
                #   光吞一次还不至于全废，致命的是它每次都会重来：
                #   松手(audio_stop)会把相位打回 CLOSED、`mic_open_sent` 复位
                #   → **下一次按键**又补发一次、又刷新一次窗口
                #   → 每一次按键都落在窗口内 → **一次都启动不了**。
                #   真机证据（2026-09-22 23:34:36~47，武哥连按 10 次）：
                #     `Audio START → 补发 MIC_OPEN → Audio START(回声+17ms)`
                #     `→ 17 帧 → Audio STOP`  原地循环 1.3s 一轮，
                #   **一行 `🎤 voice hotkey TAP` 都没有** —— 热键一次都没注入，
                #   输入法从头到尾没被叫起来。用户感受＝"语音输入完全用不了"。
                #   （这是**静默失效**：不崩、不报错，UI 波形还在跳那 17 帧，
                #     所以只能靠日志里"少了哪一行"来发现，不能靠崩溃提示。）
                #
                # 判据 = 「我们欠遥控器一声回声，而这一下大概率就是它」：
                #   · pending_mic_echo > 0            —— 发过 MIC_OPEN、回声还没到
                #   · now - mic_echo_since < ECHO_MAX_AGE —— 那一声就在这一带
                # 真机实测（2026-09-23，239 次 MIC_OPEN，239 次都配上了回声）：
                #   延迟中位 20ms、最大 1070ms；>0.8s 有 8 次、>1.5s **0 次**。
                #   ⚠ v1.0.14 把窗口设成 0.8s，依据是"回声最长 ~350ms"——
                #     那个 350ms 是拿**排队时刻**量的**假数**：真正的 GATT 写入
                #     可能慢到 1 秒，回声就跟着晚 1 秒，于是漏出窗口、被当成
                #     真按键 → **按下就掉**（武哥 2026-09-23 报的正是这个）。
                now = time.time()
                is_echo = (pending_mic_echo > 0
                           and (now - mic_echo_since) < ECHO_MAX_AGE)
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
                if is_echo:
                    # 不是用户按的 → 吞掉，并把这笔账销掉（回声到了）。
                    # ⚠ 「销账」不等于「刷新窗口」，这里**只减计数、
                    #   绝不动 mic_echo_since**：
                    #   · 不动时刻 = 窗口只能靠时间过期，一条回声不可能把窗口续下去
                    #     （v1.0.13 就是刷新时刻 → 窗口永不过期 → 按键全被吞）
                    #   · 减去计数 = 回声之后再来真按键不会被误吞
                    #     （旧写法只看时间窗：1.5s 内用户的真按键会被一起吞掉）
                    pending_mic_echo = max(0, pending_mic_echo - 1)
                    _echo_swallowed += 1
                    if _echo_swallowed <= 3:
                        logger.info(
                            f"↩ 忽略遥控器回给 MIC_OPEN 的回声 audio_start"
                            f"（距我们发 MIC_OPEN {now - mic_echo_since:.2f}s"
                            f" < {ECHO_MAX_AGE}s，本段第 {_echo_swallowed} 次，"
                            f"还欠 {pending_mic_echo} 声）"
                        )
                    else:
                        logger.debug("↩ 又是 MIC_OPEN 引来的回声，已忽略")
                elif not voice_active:
                    # 用户又开始说下一段了 → 上一条「待发送」显然不该再发出去。
                    # 例：说完一句觉得不对，紧接着按一下重说 —— 若不等这一下就
                    # 把上一条发出去，聊天框里就会多出一条残缺消息。
                    cancel_voice_send("又开始了一段新的语音")
                    # ⚠ 帧计数**只在这里**清零 —— 不能放在上面 audio_start 的顶上。
                    #   放在顶上：第 2 次按下（＝收尾）也会先走到 audio_start，
                    #   于是「先清零、再回头读它去登记发送」→ 传进去永远是 0
                    #   → 零帧误触保护每次都被触发 → **自动发送永远不触发**，
                    #   而且日志还会理直气壮地报「误触」，把排查方向指歪。
                    #   （这是本项目的老病：0 帧和误触长得一模一样，这次是程序
                    #   自己把计数器清零造成的。）
                    _audio_frames = 0
                    _audio_peak   = 0
                    # 回响计数按会话归零：这样每一段的前 3 次"吞掉"都会以 INFO
                    # 出现在日志里，一眼能看出这一段有没有被回响干扰。
                    _echo_swallowed = 0
                    voice_hotkey_down()      # tap=点按开始 / hold=按下并保持
                    voice_active     = True
                    res.voice_active = True
                    voice_started_at = time.time()
                    state.update(streaming=True, last_event="语音中…")
                    logger.info("🎙️ 语音会话【开始】—— 可以松手了，会一直听着")
                    # ── 补发 MIC_OPEN：**必须在判完回声、确认是真按键之后** ──────
                    # 遥控器按语音键只上报 AUDIO_START(0x04)，**从不发
                    # START_SEARCH(0x08)**，而开麦命令原先只挂在 start_search 分支
                    # → 遥控器永远收不到 MIC_OPEN → 一帧音频都不推，
                    # 日志表现就是连续「本次共收到 0 个音频帧」。
                    #
                    # ⚠⚠ 位置铁律：这一段**只能**待在 is_echo 判定**之后**。
                    #   v1.0.13 把它放在判定之前（无条件、每次 audio_start 都发），
                    #   而 ensure_mic_open() 成功时顺手记 mic_reopen_at=现在 →
                    #   **这一次（真按键）自己就落进了回声窗口**、当场被吞掉。
                    #   于是热键一次都没注入，输入法从头到尾没被叫起来。
                    #
                    # ⚠ 这里**不再自己记时刻** —— 回声窗口的记账已经收进
                    #   `_on_mic_open`（那个唯一会引来回声的地方）：
                    #     排队成功 → 记一条"欠一声回声"
                    #     写入成功 → 把窗口锚点挪到"遥控器真的收到了"那一刻
                    #   旧写法把时刻记在这儿，记的是"排队成功"而不是"写入成功"
                    #   —— 两者能差 1 秒，回声因此漏出窗口 → 按下就掉。
                    #   顺序铁律仍然成立：判定在**前**、补开麦在**后**。
                    session.ensure_mic_open()
                else:
                    # tap=再点按结束 / hold=松开结束。
                    # ⚠ 收尾**只走 end_voice_session 这一个入口**（见它的注释）：
                    #   它会一并发 MIC_CLOSE（让遥控器真的停流）、撤
                    #   `atvv.state.stream_active`（让后续帧不再喂进 UI/混音）、
                    #   清 `pending_mic_echo`（否则下一段的第一下会被当回声吞掉）。
                    logger.info("🎙️ 语音会话【结束】（第二次按下语音键）")
                    # send=True：用户明确按了结束，这一段该走「自动发送」策略。
                    # 传这一段的帧数：一帧都没收到 = 这次其实是误触，不该发。
                    end_voice_session("第二次按下语音键", send=True)
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
                    # 会话本来就没开（例如"按下→松手"这种一次性的短按）——
                    # 但状态可能被别处点亮过，这里一并归位。
                    # ⚠ 这条**不算会话收尾**：遥控器已经自己停了流，再发 MIC_CLOSE
                    #   是多余的。但 atvv 的推流许可必须撤掉 —— 不撤的话
                    #   `on_audio()` 会把后续帧继续喂进 UI 和混音队列
                    #   （"遥控器麦克风"卡片波形一直在动 = 用户说的"还在收音"）。
                    atvv.state.stream_active = False
                    state.end_session("语音结束")
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
        global _audio_frames, _audio_peak, _drop_frames, _remote_frame_last_at
        last_ble_activity = time.time()
        # ⚠⚠ 这一行必须在 `stream_active` 那道门**之前**。
        #
        # 它是「遥控器现在到底还在不在推流」的唯一证据，而主循环的
        # "残留推流自愈"（③）正是靠它判断的。写在门之后就永远只在
        # "我们认为在开会话"时更新 —— 而那条自愈要抓的恰恰是
        # **我们认为没有会话、遥控器却还在推**的情形，那时门已经把它挡掉了，
        # 证据永远不会刷新 → 自愈条件永远不成立 → 代码在、永远跑不到。
        _remote_frame_last_at = time.time()
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
                # ⚠ 以前这里是 `logger.debug("Queue full, dropping frame")` ——
                #   正式版日志级别是 INFO，等于**一声不响地把音频扔掉**。
                #   用户那边只看到"没声音"，日志里没有任何证据，只能靠猜。
                #   现在按"第一次 + 每 200 次"报到 WARNING。
                _drop_frames += 1
                if _drop_frames == 1 or _drop_frames % 200 == 0:
                    logger.warning(
                        "⚠ 音频队列已满，正在丢帧（累计 %d 帧）—— "
                        "输出流多半停摆了，看门狗会重建它", _drop_frames)
        except Exception as e:
            logger.error(f"AUD handler error: {e}")

    # ── Subscribe BEFORE sending CAPS ──
    logger.info("📡 Subscribing to notifications...")
    ctl_token  = ctl_char.add_value_changed(on_control)
    res.ctl_token = ctl_token
    try:
        await ctl_char.write_client_characteristic_configuration_descriptor_async(
            GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
        logger.info("✅ CTL subscribed")
    except Exception as e:
        logger.error(f"CTL subscribe failed: {e}")
        return False

    aud_token  = audio_char.add_value_changed(on_audio)
    res.aud_token = aud_token
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

    # ── 等能力响应再建音频链（2026-09-29 审查报告 P2-1）──────────────────────
    #
    # 采样率是**遥控器**在 CAPS 里给我们的（ADPCM 8k / 16k），而响应到达之前
    # `atvv.state.sample_rate` 只是 ATVVState 的默认值 16000。
    # 老代码发完 GET_CAPS 就一路往下建流 ⇒ 协商出来是 8k 的遥控器会被
    # **按 16k 建流**：不报错、不崩，只是声音变调变速（听感不对但没人知道为什么）。
    #
    # 这里先等它一下（最多 _CAPS_WAIT 秒）。**超时不能当失败** —— 一条慢响应
    # 不该把整个桥卡住，所以超时就用默认值继续，并在下面用 `_audio_rate`
    # 盯着它变（迟到且速率不同 ⇒ 重建整条链）。
    _CAPS_WAIT = 3.0
    got_caps = await loop.run_in_executor(None, caps_ready.wait, _CAPS_WAIT)
    if got_caps and atvv.state.caps is not None:
        _c = atvv.state.caps
        logger.info(
            "🎚 采样率按遥控器协商值建流：%d Hz"
            "（CAPS v%d.%d  codec=0x%02X  frame=%d）",
            atvv.state.sample_rate, _c.version[0], _c.version[1],
            _c.codecs, _c.frame_size)
    else:
        logger.warning(
            "⚠ 发出 CAPS 请求后 %.1f 秒内没等到能力响应 —— 先按默认 %d Hz 建流。\n"
            "   若响应迟到且协商速率不同，会自动按新速率重建整条音频链；"
            "这条日志留着是为了让「声音变调」这种**不报错**的故障有据可查。",
            _CAPS_WAIT, atvv.state.sample_rate or 16000)

    # 建流时用的速率 —— 能力响应迟到时靠它发现"这条流是按错的速率开的"（P2-1）
    _audio_rate = {"sr": int(atvv.state.sample_rate or 16000)}

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
    #
    # ⚠ 抽成函数是因为**采样率变了要能重建**（P2-1）：`SystemMic` 是按
    #   `out_rate` 做重采样的（mixer.py: resample_linear(in_rate → out_rate)），
    #   输出流换了速率而它不换 ⇒ 喂给新流的是**旧速率**的样本，听感又是变调。
    async def _make_sysmic(sr: int):
        if not cfg.system_mic_enabled:
            logger.info("ℹ️  电脑麦克风已在设置里关闭，本次只桥接遥控器一路")
            return None
        sm = mixer.SystemMic(cfg.system_mic_device, sr)
        ok = await loop.run_in_executor(None, sm.start)
        if ok:
            state.update(sys_mic_ready=True, sys_mic_name=sm.device_label)
            return sm
        state.update(sys_mic_ready=False, sys_mic_name="")
        return None

    sysmic = await _make_sysmic(_audio_rate["sr"])
    res.sysmic = sysmic

    def _start_stream():
        # ⚠ 读 `_audio_rate` 而不是 `atvv.state.sample_rate`：
        #   这条流必须和 `sysmic` 用**同一个**速率，否则两路混出来的东西是错的。
        #   采样率变化时由 `_rebuild_for_rate` 把这两个一起换掉。
        return _create_stream(out_dev, _audio_rate["sr"], sysmic)

    stream = await loop.run_in_executor(None, _start_stream)
    res.stream = stream
    await loop.run_in_executor(None, stream.start)
    logger.info("✅ Audio stream started（%d Hz）", _audio_rate["sr"])

    # 起跑线：先给心跳一个初值，否则监护第一次检查时 _cb_last_at 还是 0，
    # 会被当成"已静默很久"而白重建一次。
    global _cb_last_at
    _cb_last_at = time.time()

    # ── 输出流监护（"波形在动、输入法却没声音"的根因就在这）────────────────
    #
    # 为什么必须有个监护：这条流是**建一次、start 一次**的，之后没人管。
    # PortAudio/WASAPI 那一路只要出一次事（设备被别的程序独占、睡眠唤醒、
    # 驱动抖动），回调就不再被调用 —— CABLE Input 永远收到静音，而遥控器
    # 那一路的波形照旧在动（on_audio 与输出流无关），看起来"程序明明是好的"。
    # 用户唯一的出路是重启软件，这正是 2026-09-17 武哥报的那条。
    #
    # 判据只有一个、且非常硬：**输出回调的心跳**。
    # 这条流是一直开着的，所以正常时 _cb_last_at 每几十毫秒就刷新一次；
    # 静默超过 _AUDIO_SILENT_LIMIT 秒 = 这一路已经死了，直接重建。
    _AUDIO_SILENT_LIMIT = 3.0        # 秒。正常回调间隔 ~5ms（240 帧 @16k）
    _AUDIO_REBUILD_GAP  = 20.0       # 两次重建之间至少隔这么久，防止死循环重建
    _audio_sup = {"last_rebuild": 0.0, "rebuilt": 0}

    def _drain_audio_queues() -> int:
        """丢掉积压的旧采样 —— 重建后绝不能把"过去的声音"播出去。"""
        global _drop_frames
        _pending_samples.clear()
        n = 0
        while True:
            try:
                _sample_queue.get_nowait()
                n += 1
            except queue.Empty:
                break
        _drop_frames = 0
        return n

    async def _rebuild_audio(reason: str) -> None:
        nonlocal stream
        old = stream
        _audio_sup["last_rebuild"] = time.time()
        logger.warning(
            "🔇 音频输出流已停摆（%s）→ 重建这条管道。\n"
            "   为什么会出现：PortAudio/WASAPI 那一路出过一次事就再也不回来了，"
            "而遥控器波形照旧在动，所以看起来『程序没坏、就是没声音』。", reason)
        state.update(last_event="音频输出流停摆 → 已自动重建")

        def _stop_old():
            for fn in ("stop", "close"):
                try:
                    getattr(old, fn)()
                except Exception:                       # noqa: BLE001
                    pass

        await loop.run_in_executor(None, _stop_old)
        dropped = _drain_audio_queues()
        if dropped:
            logger.info("🧹 重建前清掉 %d 块积压音频（避免播出旧声音）", dropped)
        try:
            new = await loop.run_in_executor(None, _start_stream)
            await loop.run_in_executor(None, new.start)
            stream = new
            res.stream = new      # P1-3：监护重建之后也要收得到
            _audio_sup["rebuilt"] += 1
            logger.info("✅ 音频输出流已重建（累计 %d 次），声音应立刻恢复",
                        _audio_sup["rebuilt"])
        except Exception as e:                          # noqa: BLE001
            logger.error("❌ 重建音频输出流失败：%s —— 将稍后重试", e)

    async def _supervise_audio() -> None:
        now = time.time()
        if not _cb_last_at:
            return
        silent = now - _cb_last_at
        if silent <= _AUDIO_SILENT_LIMIT:
            return
        if now - _audio_sup["last_rebuild"] < _AUDIO_REBUILD_GAP:
            return
        await _rebuild_audio(f"输出回调已静默 {silent:.1f} 秒")

    async def _rebuild_for_rate(new_sr: int) -> None:
        """协商采样率与建流速率不同 → 整条音频链按新速率重建（P2-1）。

        什么时候会走到：CAPS 响应比 `_CAPS_WAIT` 还晚（蓝牙慢 / 遥控器刚被唤醒），
        于是我们先用默认 16000 建了流，之后才收到"其实是 8k"。
        不重建的后果是**不报错但听感不对** —— 8k 的样本按 16k 播 = 变调变速，
        用户只会说"声音怪怪的"，日志里一个字都没有。

        ⚠ `_audio_rate` 在开头就改成新值，**失败也不回退**：不回退是为了
          避免主循环每 200ms 重试一次、把日志刷爆（重建失败多半是设备被独占，
          重试也不会好）。代价是"这条链可能还停在旧速率"—— 所以失败那条
          日志把话说全了，别让后来的人以为已经修好了。
        """
        nonlocal stream, sysmic
        old_stream, old_sysmic = stream, sysmic
        _audio_rate["sr"] = new_sr
        logger.warning(
            "🎚 协商采样率与建流时不同 → 按 %d Hz 重建整条音频链。\n"
            "   为什么会出现：CAPS 响应比我们等待的时间还晚（蓝牙慢 / 遥控器刚醒）。\n"
            "   为什么必须重建：8k 的样本按 16k 播是变调变速，**不报错**、只听着不对；\n"
            "   而且电脑麦克风那一路是按旧速率做重采样的，只换输出流同样不对。",
            new_sr)
        state.update(last_event=f"采样率改为 {new_sr} Hz → 已重建音频链")

        def _stop_old():
            for obj in (old_stream, old_sysmic):
                if obj is None:
                    continue
                for fn in ("stop", "close"):
                    try:
                        getattr(obj, fn)()
                    except Exception:                   # noqa: BLE001
                        pass

        await loop.run_in_executor(None, _stop_old)
        dropped = _drain_audio_queues()
        if dropped:
            logger.info("🧹 重建前清掉 %d 块积压音频（避免按新速率播出旧声音）", dropped)
        try:
            sysmic = await _make_sysmic(new_sr)
            res.sysmic = sysmic
            new = await loop.run_in_executor(None, _start_stream)
            await loop.run_in_executor(None, new.start)
            stream = new
            res.stream = new
            logger.info("✅ 音频链已按 %d Hz 重建", new_sr)
        except Exception as e:                          # noqa: BLE001
            logger.error(
                "❌ 按新采样率重建音频链失败：%s\n"
                "   ⚠ 这条链可能**还停在旧速率**（声音会变调），且不会再自动重试"
                "（重试多半也没用，设备被独占）。处置：控制台点「重新连接」。", e)

    # ── Keyboard hooks ──
    import keyboard as _kb
    res.kb = _kb              # P1-3：unhook 用
    from keys import was_self_injected

    # 遥控器按键（HID）→ 内部 button_id。
    #
    # ⚠⚠ 这里的键名必须是 keyboard 库 **normalize_name() 之后**的样子。
    #    钩子回调里的 `e.name` 在 `KeyboardEvent.__init__` 里被归一化过
    #    （keyboard/_keyboard_event.py:32），它会：整体小写（长度>1 时）、
    #    `_`→空格、再查一遍规范名表（keyboard/_canonical_names.py）。
    #    那张表把 'escape'→'esc'、'return'→'enter'、' '→'space'、
    #    'spacebar'→'space'、'applications'→'menu'、'left menu'→'left alt'、
    #    'play/pause'→'play/pause media'。
    #
    # 🔴 2026-09-23 实测整改（别再写回去）：
    #    旧表里有 4 个**永远不会命中**的死键名，其中 `escape` 是致命的 ——
    #    库只会报 `esc`，于是"遥控器返回键"这条映射从一开始就是死的。
    #    这次不是照文档猜，而是把库的 `to_name` 表整个跑了一遍
    #    （见 CHANGELOG v1.0.15），拿到"钩子究竟可能报哪些名字"的**完整清单**。
    #
    # 🔴 三条不许违反的规矩：
    #  ① 只写库真会报的名字。写错的代价是静默失效 —— 按了没反应，日志一片空白。
    #  ② **绝不写单个字母**。本机实测（同一份清单）：库用
    #     `MapVirtualKeyW(vk, VK_TO_CHAR)` 兜底给多媒体键起了字母名 ——
    #     `mute`→'D'、`vol_down`→'C'、`vol_up`→'B'、`next track`→'P'、
    #     `previous track`→'Q'、`stop media`→'J'、`play/pause media`→'G'、
    #     `browser start and home`→'M'。
    #     把 'B' 映射成 vol_up 就等于**劫持物理键盘的 B 键**（打字变调音量）。
    #     所以这 8 个键**一律不进这张表**。
    #  ③ 音量 / 静音 / 播放这些键本来也不该靠钩子：Windows 会把手电（消费类）
    #     集合的按键翻成 WM_APPCOMMAND 而不是键盘事件，钩子根本看不到。
    #     它们走的是**厂商页**那条路（`remote_hid.py` 直接读 0x2A4D 报告），
    #     解出来的 button_id 直接进同一个 `resolve_button`，不经过这里。
    # 🔴🔴 2026-09-29 重大整改：这张表**只许放"物理键盘不会产生"的键名**。
    #
    # 为什么（真机事故，用户报的原话是"按空格就把消息发出去了"）：
    #   低级键盘钩子**分不出遥控器和物理键盘** —— 它只看键名。所以只要表里
    #   出现 `space` / `enter` / `esc` / 方向键 这类**物理键盘也有**的名字，
    #   用户打字时就会被当成遥控器按键：
    #       按物理空格 → 命中 "space" → btn_id = "ok" → 注入 Enter
    #       （config 里 ok 默认就是 enter）→ 微信里**直接把手打的消息发出去**。
    #   真机日志实证（2026-09-29 09:58:32）：
    #       🔘 HID 按键 'space'（scan=57） → 按钮「ok」→ 动作 'enter'
    #   `scan=57` 正是**物理键盘**的空格键码。同一个毛病还有：
    #       物理回车 → 一次变两次回车；物理 Esc / 方向键 → 每个都多按一下。
    #
    # 那遥控器的按键靠什么？**不靠这张表。** 本机遥控器走的是
    #   · `frida_hid.RemoteHidTap`（v1.0.20：注入 WUDFHost 抄 IOCTL 报告）
    #   · `remote_hid.RemoteHidButtons`（厂商页 0x2A4D）
    # 两条路都**直接给出 button_id**、直接进 `resolve_button`，与本表无关。
    # 本表只是"某些遥控器把按键报在键盘页"时的兜底 —— 而本项目的实测结论是
    # Chromecast 遥控器的按键**从来不走键盘页**（在 WUDFHost 内部就被消费了）。
    #
    # ⚠ 规矩（与下面那三条并列，别再违反）：
    #  ④ **凡是物理键盘也有的键名，一律进 `KEY_MAP_SHARED`**（默认不生效），
    #     不许写进这张常开表。`tools/check_keymap.py` 会拦。
    KEY_MAP = {
        # 消费类键：物理键盘基本不会产生，可以常开。
        "browser back":               "back",     # 0xA6 遥控器返回键常报这个
    }

    # ⚠⚠ 这张表里的名字**物理键盘也会产生**，所以**默认不生效**。
    #   要打开得设 config.json 的 `keyboard_page_keys: true`（见 config.py 里的长注释）。
    #   打开之后的代价：打字时这些键会被当成遥控器按键 ——
    #   按一下空格会多发一个回车（在聊天软件里就是"手打的消息被发出去"）。
    #
    # 保留它是因为：万一哪天遇到一台**只**走键盘页的遥控器（本机这台不是），
    # 这条兜底还有用；但默认必须是关的 —— 安全方向是"打字正常"，
    # 而不是"多认一个遥控器按键"。
    KEY_MAP_SHARED = {
        # 主页
        "home":                       "home",     # 0x24
        # 确认
        "enter":                      "ok",       # 0x0D
        "space":                      "ok",       # 0x20（确认键走键盘页时可能是空格）
        # 返回
        "esc":                        "back",     # 0x1B ⚠ 曾经写成 "escape"（死键名）
        # 方向
        "up": "up", "down": "down", "left": "left", "right": "right",
    }

    # 这些名字是**真的**会被库报出来（实测清单里有），只是没有语义明确的内置动作。
    # 用途只有一个：让日志把话说清楚 —— 出现它们说明按键
    # **已经到 Windows 了**（这是好消息，问题不在蓝牙那一层），
    # 而不是笼统一句"没有对应按钮"。
    # ⚠ 刻意不硬凑映射：把 `browser refresh` 硬指向某个动作，
    #   用户按物理键盘上的浏览器键也会中招。
    REMOTE_LIKE_KEYS = {
        "browser forward", "browser refresh", "browser stop",
        "browser search key", "browser favorites",
        "menu", "select", "select media", "start mail",
        "start application 1", "start application 2",
        "play", "page up", "page down",
    }

    def _library_key_names() -> set:
        """keyboard 库**可能报出来**的全部键名（归一化之后的）。

        做法：把库的 `to_name` 表整张跑一遍，取每条的 `names[0]` ——
        那正是钩子回调里 `e.name` 的来源（`_winkeyboard.process_key` 里
        `name = names[0]`），再过一遍 `normalize_name` 与
        `KeyboardEvent.__init__` 完全一致。所以这就是权威清单，
        不用我们照着文档猜（照文档猜正是 `escape` 那个死键名的来源）。

        拿不到库（没装 / 换了实现）时返回空集合，此时**不报结论** ——
        这一点很重要：拿不到清单不等于"键名有问题"，
        不许把"没测成"说成"测出来是坏的"。
        """
        try:
            from keyboard import _winkeyboard as _w
            from keyboard._canonical_names import normalize_name as _nn
            _w._setup_name_tables()
            out = set()
            for names in _w.to_name.values():
                if names:
                    out.add(_nn(names[0]))
            return out
        except Exception as e:                          # noqa: BLE001
            logger.debug(f"取 keyboard 键名清单失败（跳过这项自检）：{e}")
            return set()

    def _audit_key_map_names() -> None:
        """开机自检：KEY_MAP 里的键名，库真的会报出来吗？

        为什么值得单项检查：写错键名的失效是**绝对静默**的 ——
        那个按键永远匹配不上，而日志一个字都不说（"认不出"分支只在
        **真收到事件**时才走）。遥控器按键本来就不来，两件事叠在一起
        就再也查不出来了 —— `escape` vs `esc` 就是这么躺了整整一版的。
        """
        known = _library_key_names()
        if not known:
            return                                   # 拿不到清单 → 不出结论
        dead = [k for k in KEY_MAP if k not in known]
        if dead:
            logger.error(
                "❌ 按键映射表里有 %d 个**永远不会命中**的键名 %s —— "
                "对应按键按下去不会有任何反应，而且日志不会留痕。"
                "（键名必须是 keyboard 库归一化之后的写法，"
                "例如库报 `esc` 而不是 `escape`）",
                len(dead), dead,
            )
        else:
            logger.info(
                "✅ 按键映射自检：%d 个键名都能被 keyboard 库报出来",
                len(KEY_MAP),
            )

    _audit_key_map_names()

    # keymap 缓存：按键事件可能一秒来好几个，不能每次都读一遍 config.json。
    # 用 mtime 判断是否被控制台/手改过 —— 改了立即生效，不用重启。
    _cfg_cache: dict = {}
    _cfg_mtime: float = -1.0
    _keymap_warned: set[tuple[str, str]] = set()
    # 首次见到的键名只报一次，见 _on_key 里那两处。
    _seen_keys: set[str] = set()

    # 按键旁路（Frida）的句柄。⚠ 必须**在这里**先声明成 None：
    # 下面的 `_get_cfg()` 会在旁路起来之前就被调用（本函数第 1997 行那次预热），
    # 而"配置一变就下发屏蔽表"要读它 —— 晚一步声明就会撞
    # `NameError: unbound local`，把整个桥启动搞挂。
    _hid_tap = None
    _last_pushed_mapping: dict | None = None

    def _effective_keymap(cached: dict) -> dict:
        """归一后、**真正要下发给钩子**的那张表。

        ⚠ 总开关关掉时要下发的是**空表**，不是"不下发"。
        不下发 = JS 侧继续按上一次的表把 usage 原地写 0 ⇒ 界面写着
        「已停用/原样直通」，实际那些键彻底失效，直到重连。
        """
        if not cached.get("mapping_enabled", True):
            return {}
        return dict(cached.get("keymap") or {})

    def _push_mapping_if_changed(cached: dict) -> None:
        """keymap / mapping_enabled 一变就把新屏蔽表下发给 Frida 钩子（P1-6）。

        原先只在启动时下发一次，于是运行中关掉映射、或把某键改成 native 之后
        Python 不再映射、JS 仍按旧表清零 —— 现象是"改了没用，得重连"。
        """
        nonlocal _last_pushed_mapping
        km = _effective_keymap(cached)
        if km == _last_pushed_mapping:
            return
        _last_pushed_mapping = km
        if _hid_tap is None:
            # 旁路还没起来（或本来就没启用）。起来那一刻会自己下发一次，
            # 所以这里只记账、不报错。
            return
        try:
            _hid_tap.set_mapping(km)
        except Exception as e:                      # noqa: BLE001
            logger.warning(f"⚠ 下发按键屏蔽表失败（按键映射可能滞后到重连）：{e}")

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
                    # 语音结束后自动发送（见 config 里的长注释）。这三个字段
                    # 必须进这个白名单，否则用户改了 config.json 也不会生效 ——
                    # 而且会**静默**不生效（缓存里读不到就当默认值用）。
                    "send_after_voice": bool(getattr(new, "send_after_voice", False)),
                    "send_after_voice_delay_ms": int(
                        getattr(new, "send_after_voice_delay_ms", 800) or 800),
                    "send_after_voice_key": str(
                        getattr(new, "send_after_voice_key", "enter") or "enter"),
                    # 键盘页兜底映射（⚠ 默认关，见 config 里的长注释：
                    # 开着 = 按物理空格会被当成遥控器确认键、再注入一个回车）。
                    "keyboard_page_keys": bool(
                        getattr(new, "keyboard_page_keys", False)),
                }
                _warn_bad_keymap(_cfg_cache["keymap"])
                # 映射表变了就立刻下发（P1-6）：控制台改完 / 手改 config.json
                # 都能马上生效，不用重连。
                _push_mapping_if_changed(_cfg_cache)
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
        #
        # ⚠⚠ 默认**不查** `KEY_MAP_SHARED` —— 那张表里是 space / enter / esc /
        #    方向键，**物理键盘也会产生这些名字**，查了就等于劫持打字：
        #    按一下物理空格 → 命中 "space" → 按钮「ok」→ 注入一个回车
        #    （config 里 ok 默认就是 enter）→ 聊天软件里手打的消息被直接发出去。
        #    真机实证 2026-09-29 09:58:32：`HID 按键 'space'（scan=57） → 按钮「ok」`。
        #    要用得显式打开 config.json 的 `keyboard_page_keys`（默认 false）。
        cached = _get_cfg()
        kmap = KEY_MAP
        if cached.get("keyboard_page_keys", False):
            kmap = {**KEY_MAP, **KEY_MAP_SHARED}
        btn_id = kmap.get(e.name, "")
        if not btn_id and e.name:
            # ⚠ 兜底只跑**多词复合键**（键名里带空格），单词键一律不动。
            # 单词键（up/down/left/right/back/home/enter…）已由上面的
            # `kmap.get` 精确匹配兜住，绝不能进兜底循环：
            # 词边界下 "up" 会误中物理键盘的 "page up"、"left" 误中 "left windows"，
            # 导致按方向/翻页键时顺手注入一个方向键。
            # 复合键（"browser back" / "browser start and home" / "volume up" 等）
            # 即便未来 keyboard 库换种写法、精确匹配失手，也能兜底接住。
            for k, v in kmap.items():
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
            # ⚠ 收尾只走 end_voice_session（见它的注释）：6 条路径共用一份机制。
            #   send=swallow 的理由：按确认＝"我确定说完了"，本该登记自动发送；
            #   但 swallow=False 时这一下 OK **自己就会变成 Enter** 发出去，
            #   再登记一次就是连发两下（多发一条空消息）→ 只在吞掉这一下时才登记。
            logger.info(
                "🎙️ 语音会话【结束】（确认键）"
                + ("；这一下不再当 Enter 发出" if swallow else "；Enter 照常放行")
            )
            end_voice_session("确认键", send=swallow)
            return not swallow               # 吞掉这一下，不让它再当 Enter 发出去

        if btn_id and btn_id != "voice":
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
            if e.name in REMOTE_LIKE_KEYS:
                # ⚠ 这一条要说清楚"这是好消息"。
                #   本机实测里出现过一种最容易被误读的情形：遥控器按键
                #   **确实到了 Windows**（说明蓝牙/HID 那层是通的！），
                #   但这个键名不在映射表里，于是日志只留一句"没有对应按钮"，
                #   看起来和"按键根本没来"一模一样 —— 排查方向会整个走反。
                logger.info(
                    f"✅ HID 按键 {e.name!r}（scan={getattr(e, 'scan_code', None)}）"
                    f" → **已到 Windows**（这是遥控器/多媒体键盘上的功能键），"
                    f"但目前没有对应动作，已忽略。"
                    f" 它没进 KEY_MAP 是**刻意的**：这类键名要么语义不明确、"
                    f"要么会被误当成普通字母，硬凑映射会劫持物理键盘。"
                    f" 需要用它的话请把这一行发出来。"
                )
            else:
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

    # ── 遥控器按键（HID 厂商自定义页）────────────────────────────────────
    #
    # ⚠⚠ 这一块是 v1.0.11 修「方向/返回/Home/YouTube…一个都没反应」的关键，
    #    别再只在键盘钩子里找原因。
    #
    # 遥控器（VID 18D1 / PID 9450）暴露 5 路 HID 集合：键盘 / 消费类 / 鼠标 /
    # 厂商页 0xFF01 / 厂商页 0xFF80。实测它把**所有**按键都发在两个厂商页上，
    # 而 Windows 只处理前三种 —— 厂商页 Windows 完全不理，既不产生键盘事件
    # 也不产生媒体键事件。于是：
    #   · 键盘钩子**永远**收不到（日志里连一行 🔘 都没有，不是"没映射")
    #   · 改映射表改到天亮也没用 —— 事件根本没进 Windows
    # 唯一出路就是自己 CreateFile 打开这两路集合、按参考实现
    # （VincentKingHsu/vRemoter 的 ChromecastRemoteHIDBridge.swift）的格式解码。
    #
    # 解码走的是模块 remote_hid.py；解出来的 button_id 直接喂给现有的
    # resolve_button，映射表 / 控制台 / config.json 全都不用改。
    #
    # ⚠ 语音键不在这条路上：它走 ATVV 的 BLE 数据通道（见 on_control）。
    #   CHROMECAST_BUTTONS 里 voice 的 usage 是字符串 "voice"，
    #   所以 remote_hid 的 USAGE_TO_BUTTON 天然不含它，两条路不会打架。
    _hid_buttons = None
    # ⚠ `_hid_tap` 不在这里声明 —— 它已经在上面 `_get_cfg` 定义之前声明过了
    #   （那段"配置一变就下发屏蔽表"要读它）。这里再写一次会把它**重新置成 None**，
    #   虽然此刻还没赋值、后果一样，但两处声明会让下一个改代码的人以为
    #   "这里才是权威"，改错地方。保持唯一声明点。
    # 两条按键来源（frida 旁路 / 厂商页自读）共用这一个派发口，按 (键, 沿)
    # 去重，防止同一按键被两路各派发一次（两路都能用时才会碰到）。
    _hid_recent: dict[tuple[str, bool], float] = {}

    def _on_hid_button(btn_id: str, is_down: bool) -> None:
        """HID 解出来的按键 → 复用现有映射表派发（与 _on_key 同一套规则）。"""
        nonlocal last_key_activity, last_ble_activity, voice_active, voice_started_at
        now = time.time()
        _k = (btn_id, is_down)
        if now - _hid_recent.get(_k, 0.0) < 0.06:
            return                                  # 两路重复上报 / 抖动
        _hid_recent[_k] = now
        # 遥控器还活着的证据：watchdog 是"ATVV 静默 180 秒就重连"，
        # 而按键走厂商页、不产生 ATVV 通知 —— 不记进来就会出现
        # "一直在按遥控器却被判失联、给重连了"。
        last_ble_activity = now
        if is_down:
            last_key_activity = now

        cached = _get_cfg()
        if not cached.get("mapping_enabled", True):
            return                                  # 映射总开关关掉 → 什么都不发

        # 语音会话中按「确认键」= 收尾。
        # 这条本来只在 _on_key（键盘钩子）里有，而厂商页的确认键走不到那儿 ——
        # 不在这里补上，就是"按 OK 关不掉语音"。
        if is_down and voice_active and btn_id == "ok":
            # ⚠ 收尾只走 end_voice_session（见它的注释）：6 条路径共用一份机制。
            logger.info("🎙️ 语音会话【结束】（厂商页确认键）")
            # send=True：下面直接 return，这一下按键被我们**吞掉了**、不会走到
            # resolve_button 变成 Enter → 无条件登记，不存在重复发送。
            end_voice_session("厂商页确认键", send=True)
            return

        resolve_button(
            btn_id,
            event_type="down" if is_down else "up",
            keymap=cached.get("keymap") or {},
            on_voice=None,
        )

    if getattr(cfg, "hid_vendor_keys", True):
        try:
            import remote_hid
            # bypass_probe：让 HID 通道审计在「旁路已就绪」时别再把
            # 「厂商页 0 条」说成「按键没到本程序」。闭包读的是调用时刻的
            # `_hid_tap`（它在本函数后面才被赋值），所以这里写 lambda 是安全的。
            _hid_buttons = remote_hid.RemoteHidButtons(
                _on_hid_button,
                bypass_probe=lambda: (_hid_tap is not None
                                      and bool(getattr(_hid_tap, "ready", False))),
            )
            res.hid_buttons = _hid_buttons
            n_col = _hid_buttons.start()
            if n_col:
                logger.info(f"✅ 遥控器厂商页按键已接管（{n_col} 路集合）→ 按键映射生效")
            else:
                # ⚠ 必须报出来。"开不了"和"开了但没按键"在用户眼里都是
                # "按键没反应"，不写这一行就等于让下一个排查的人重新走一遍。
                logger.warning(
                    "⚠ 没找到遥控器的厂商页 HID 集合 —— 除语音键外的按键将不会生效。"
                    " 常见原因：① 遥控器没连上/没配对；② 插的是别的蓝牙棒、"
                    "设备 VID 不是 18D1；③ 设备被别的程序独占。"
                    " 用 RemoteVoiceBridgeDiag.exe --hid 看设备在不在。"
                )
                _hid_buttons = None
        except Exception as e:                      # noqa: BLE001
            # 这一路挂了不该拖垮语音 —— 它是"读不到"，不是"桥起不来"。
            logger.exception(f"⚠ 启动厂商页按键读取失败（语音功能不受影响）：{e}")
            _hid_buttons = None
    else:
        logger.info("ℹ️  厂商页按键读取已在设置里关闭（hid_vendor_keys=false）")

    # ── 遥控器按键（Frida 旁路，注入 WUDFHost）────────────────────────────
    #
    # ⚠⚠ 这是 v1.0.20 修「除语音键外所有按键都没反应」的关键，也是真机上
    #    **唯一**能拿到遥控器按键的那一路。
    #
    # 为什么：遥控器的 HID 报告在 WUDFHost.exe（承载 HOGP 的 UMDF 驱动宿主）
    # 内部就被消费掉了 —— 用户态 HID 接口 / Raw Input / 键盘钩子**全都看不到**。
    # 上面 `hid_vendor_keys` 那条「自开厂商页」的路真机实测一直是 0 条。
    # 唯一出路是用 Frida 注入 WUDFHost，在它读 GATT 特征的那次
    # IOCTL（0x80018483）输出缓冲区上抄一份。
    #
    # 解出来的 button_id 与厂商页那条路**完全一致**，一起喂给上面的
    # `_on_hid_button` → 映射表 / 控制台 / config.json 全都不用改。
    # 两条路并行互补：任一路拿到按键都能用。
    #
    # ⚠ 语音键仍不在这里：它走 ATVV 的 BLE 数据通道（见 on_control）。
    if getattr(cfg, "hid_frida_tap", True):
        try:
            import frida_hid
            # 远端 MAC（12 位小写 hex）—— 比 VID/PID 硬的"就是这台"证据：
            # 同型号的第二只遥控器 VID/PID 完全一样，只有 MAC 分得开。
            _mac = f"{_remote_addr:012x}" if _remote_addr is not None else None
            _hid_tap = frida_hid.RemoteHidTap(
                _on_hid_button,
                # ⚠ 默认**不许**回退到"任意 BLE HID 的第一项"（P1-8）：
                #   同一个 WUDFHost 里可能还服务别的蓝牙键鼠。兼容款要显式打开。
                allow_any_hid=bool(getattr(cfg, "hid_frida_any_hid", False)),
                expect_mac=_mac,
            )
            res.hid_tap = _hid_tap
            # 下发的是**归一后**的表（mapping_enabled=false → 空表，见
            # _effective_keymap 的注释）。记进 _last_pushed_mapping，免得
            # 下一次 _get_cfg() 又原样推一遍。
            _initial = _effective_keymap(_get_cfg())
            _last_pushed_mapping = _initial
            try:
                _hid_tap.set_mapping(_initial)
            except Exception:                       # noqa: BLE001
                pass
            _hid_tap.start()
            logger.info("🔓 遥控器按键旁路已启动（注入 WUDFHost 读 HID 报告）"
                        "—— 若日志随后出现「按键旁路：就绪」，除语音键外的按键即可用")
        except Exception as e:                      # noqa: BLE001
            # 同厂商页那条：这一路挂了不该拖垮语音 —— 它是"读不到"，不是"桥起不来"。
            logger.exception(f"⚠ 启动按键旁路失败（语音功能不受影响）：{e}")
            _hid_tap = None
    else:
        logger.info("ℹ️  按键旁路已在设置里关闭（hid_frida_tap=false）")

    # ── Health check ──
    async def check_health(force: bool = False) -> bool:
        # 只改 last_health_check —— 这里已经不碰语音会话的两个变量了
        # （超时兜底与状态归位都搬去主循环，见上面那段注释）。
        nonlocal last_health_check
        now = time.time()

        # ⚠ 语音会话的「超时兜底」和「状态归位」**已经搬去主循环**（见下面
        #   `_supervise_audio()` 之后那一段）。别把它们挪回这里 ——
        #   `check_health()` 只在"最近 cfg.key_check_window（默认 3 秒）内按过键"
        #   时才会被主循环调用，而"按了开始就走开"这种最需要兜底的场景
        #   **一个按键都没有** ⇒ 兜底代码一次都不会执行。
        #   （这是"代码在、但永远不会跑到"的静默失效，和 v1.0.5 那个
        #     "诊断工具没进安装包"是同一类：看起来做了，其实没做。）

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
            # ⚠ 退出请求必须在**每一轮的最前面**看：托盘点「退出」时，
            #   桥线程要能自己走到 finally 去关 GattSession / Frida / 音频流，
            #   而不是等主进程结束时被直接掐掉（2026-09-29 审查报告 P1-4）。
            if stop.is_set():
                logger.info("🛑 收到退出请求 → 结束桥循环，开始清理")
                ran_ok = True
                break

            await asyncio.sleep(0.2)

            # 音频输出流监护：回调静默超时就重建（见 _supervise_audio 注释）。
            # 放在每一轮的最前面 —— 这是"没声音"里唯一能自愈的一条，越早发现越好。
            try:
                await _supervise_audio()
            except Exception as e:                      # noqa: BLE001
                logger.error("音频监护异常：%s", e)

            # 协商采样率变了 → 整条音频链按新速率重建（P2-1）。
            # 只在"CAPS 响应迟到"时才会响一次（正常情况下上面已经等到了），
            # 所以放在这里不占常规开销。
            _want_sr = int(atvv.state.sample_rate or 16000)
            if _want_sr != _audio_rate["sr"]:
                try:
                    await _rebuild_for_rate(_want_sr)
                except Exception as e:                  # noqa: BLE001
                    logger.error("按采样率重建音频链异常：%s", e)

            # ── 语音会话的收尾与「状态归位」（每一轮都核一遍）──────────────
            #
            # ⚠ 这两件事**必须待在主循环里**，不能塞进 check_health()：
            #   check_health() 只在"最近 cfg.key_check_window（默认 3 秒）内按过键"
            #   时才会被调用，而"用户按了开始就走开"恰恰一个按键都没有 ⇒
            #   放在那儿的兜底代码**一次都不会执行**（代码在、永远跑不到）。
            #   下面两条都是"状态与事实不一致"，属于每一轮都该核的事。
            _now_v = time.time()

            # ① 超时兜底：用户按了「开始」却忘了收尾，程序自己收场。
            #    「管理好语音输入功能」包含这一条 —— 不能指望用户永远记得按第二下。
            if voice_active and voice_started_at and (_now_v - voice_started_at) > VOICE_MAX_SECONDS:
                logger.warning(
                    f"⏰ 语音会话已持续 {int(_now_v - voice_started_at)} 秒，超过上限 "
                    f"{VOICE_MAX_SECONDS} 秒 → 自动结束，避免一直挂着听"
                )
                logger.info("🎙️ 语音会话【结束】（超时自动收尾）")
                # ⚠ 超时收尾**故意不自动发送**（send=False，也是默认值）——
                #   别顺手改成"那也发一下吧"：麦克风已经开着最多 10 分钟，
                #   里面很可能全是环境音、旁人的话（用户人可能早走开了）。
                #   把这种内容自动发进聊天框，比"少发一次"严重得多 ——
                #   宁可不发，让用户自己看一眼再决定。
                #   send=False 会顺手**取消**待发送，防别处残留的登记在这一刻被触发。
                end_voice_session("超时自动收尾", send=False)

            # ② 状态归位：`streaming` 是 UI 上「有没有在收音」的**唯一真源**
            #    （托盘图标变橙红、控制台顶部那行「语音中」都直接读它），
            #    所以它必须与 `voice_active` 一致。
            #    历史上它被 `audio_start` 分支顶部一句无条件 `update(streaming=True)`
            #    点亮过，而**回声** audio_start 也会走到那里 —— 会话刚结束又来一条
            #    回声，状态就被重新点亮，唯一能清掉它的那条 audio_stop 却可能永远
            #    不来 ⇒ 用户看到的就是"已经关了还在显示收音中"（2026-09-29 报的）。
            #    根因那处已经拆掉（见 audio_start 分支的注释），这里再兜一层：
            #    **宁可多撤一次** —— 撤错了下一帧音频就会把它点亮回来，
            #    而漏撤的代价是 UI 一直说谎。
            if state.get().streaming != voice_active:
                if voice_active:
                    state.update(streaming=True, last_event="语音中…")
                else:
                    logger.info("🧹 语音会话没在跑，但状态还亮着「语音中」→ 已归位")
                    # ⚠ 顺手把推流许可也撤掉：不撤的话 `on_audio()` 会继续
                    #   把帧喂进「遥控器麦克风」那张卡的波形 —— 用户看到的就是
                    #   "已经关了，波形还在动、还显示在收音"（2026-09-29 报的）。
                    atvv.state.stream_active = False
                    state.end_session("语音结束（状态归位）")

            # ③ 残留推流自愈：程序这边**没有会话**，遥控器却**还在推流**。
            #
            # 这一条是给"遥控器没收到 MIC_CLOSE"兜底的。用户能看到的症状是：
            #   输入法那边早关了、程序也认为会话结束了，**遥控器却还在收音** ——
            #   控制台「遥控器麦克风」那张卡的波形一直在动，看着像程序坏了。
            # 真机实证（2026-09-29）：会话 11:26:34 结束，遥控器一路推到 11:27:25
            #   （多推 52 秒 / 3161 帧），全程没有任何 MIC_CLOSE。
            # 现在收尾会主动发 MIC_CLOSE（见 end_voice_session），这里再兜一层：
            #   只要"我们没在开会话"却"还在收帧"，就再命令它停一次。
            #   ⚠ 冷却时间是必须的 —— 主循环 200ms 一轮，没有冷却会瞬间刷爆 BLE。
            if (not voice_active
                    and _remote_frame_last_at
                    and (_now_v - _remote_frame_last_at) < 2.0
                    and (_now_v - _residual_close_at) > 5.0):
                _residual_close_at = _now_v
                logger.warning(
                    "⚠ 遥控器还在推流，但程序这边没有会话 → 补发 MIC_CLOSE 让它停"
                    "（正常情况下不该出现，出现了说明某条收尾路径漏发了命令）"
                )
                session.close("残留推流自愈")
                atvv.state.stream_active = False
                state.end_session("语音结束（残留推流已停）")

            # ── 语音结束后的自动发送（见 request_voice_send 的注释）──────────
            # 放在主循环而不是 BLE 回调线程里，有三个好处：
            #   ① 回调线程没有 asyncio 循环，那边只能干同步活
            #   ② 这里能安全地读配置（用户在控制台改了开关下一轮就生效）
            #   ③ 到点发送这件事本身是"延迟动作"，天然属于循环
            try:
                maybe_send_after_voice(_get_cfg())
            except Exception as e:                      # noqa: BLE001
                logger.error("自动发送异常：%s", e)

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
