"""
Shared bridge state — thread-safe handoff between the asyncio bridge
thread and the tray/UI thread.

The bridge runs in its own thread (asyncio loop); the tray icon and the
console window run in others. This module is the single source of truth
both sides read from, so no locks are held across UI calls.
"""

from __future__ import annotations
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Callable

_lock = threading.RLock()
_listeners: list[Callable[[], None]] = []

# 波形环形缓冲：每路音频都在自己的采集线程/回调里往里推代表点。
# 放在 BridgeState 之外，因为它是每帧高频写入的原始数据，
# 不该跟着 state.get() 一起被 asdict 拷贝。
#
# 三路各一条：电脑麦克风 / 遥控器麦克风 / 混合输出。
# 三条都画出来，用户才能一眼区分「没声音」到底是哪一段断的 ——
# 是麦克风没采到，还是遥控器没推流，还是混音把某一路压掉了。
_WAVE_POINTS = 256
_WAVE_WIRE = 128          # 发给前端只取最近 128 点，够画满一条波形了

_wave: deque[int] = deque(maxlen=_WAVE_POINTS)      # 遥控器麦克风
_wave_sys: deque[int] = deque(maxlen=_WAVE_POINTS)  # 电脑麦克风
_wave_mix: deque[int] = deque(maxlen=_WAVE_POINTS)  # 混合输出（最终送给微信的）


def db_from_peak(peak: float, full: float = 32768.0) -> float:
    """线性峰值 → dBFS（-96 视为静音）。

    UI 上显示的是 dB 而不是原始峰值：人耳是对数感知的，"有声音但很小"在图上看
    该是 -50dB 还是 -20dB，一眼就能判断；给它一个 0–32768 的整数毫无直观意义。
    """
    p = abs(float(peak))
    if p < 1.0:
        return -96.0
    return max(-96.0, 20.0 * math.log10(p / full))


@dataclass
class BridgeState:
    connected: bool = False
    streaming: bool = False
    device:    str  = ""
    level:     int  = 0          # 0-100, audio level meter
    last_event: str = ""
    updated_at: float = 0.0
    # ── 音频诊断（"输入法没反应"时用来区分：没收到音频 vs 收到了没触发输入法）──
    audio_frames: int   = 0      # 本次会话累计收到的音频帧数
    audio_peak:   int   = 0      # 本次会话峰值（int16 满量程 32768）
    audio_last_at: float = 0.0   # 最后一次收到音频的时间戳
    sample_rate:  int   = 0
    frame_bytes:  int   = 0

    # ── 三路电平（dBFS，UI 直接显示）────────────────────────────────────
    sys_level_db:    float = -96.0   # 电脑麦克风
    remote_level_db: float = -96.0   # 遥控器麦克风
    mix_level_db:    float = -96.0   # 混合输出
    # ── 设备名（给"状态清单"用）─────────────────────────────────────────
    sys_mic_name: str = ""
    out_dev_name: str = ""
    sys_mic_ready: bool = False      # 系统麦克风采集是否真的开起来了

    def as_dict(self) -> dict:
        return asdict(self)


_state = BridgeState()


# ── 实时麦克风增益 ────────────────────────────────────────────────────────────
# 刻意放在 BridgeState 之外、且**不加锁**：
# 它是音频播放回调（每 ~15ms 一次、每次 240 个采样）要读的热路径变量，
# 加锁会在播放线程上引入抖动；而 float 赋值/读取在 GIL 下本身就是原子的，
# 读到"上一帧的值"完全无所谓。
# 控制台的滑块改这里 → 下一个音频块立即生效，不用重连、不用重启。
_live_gain: float = 10.0


def get_gain() -> float:
    return _live_gain


def set_gain(value: float) -> float:
    """设置实时增益，返回夹取后的值（1x–30x）。"""
    global _live_gain
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _live_gain
    _live_gain = max(1.0, min(30.0, v))
    return _live_gain


# ── 混音参数 ──────────────────────────────────────────────────────────────────
# 同样刻意不加锁：音频播放回调每个块（~15ms）都要读一遍。
# 这就是 UI 上的「静音 / 独奏 / 参与混音 / 增益」四个开关的真源，
# 面板上一点，下一个音频块立即生效。
_mix: dict = {
    "sys_enabled":    True,    # 参与混音
    "remote_enabled": True,
    "sys_muted":      False,   # 静音
    "remote_muted":   False,
    "sys_solo":       False,   # 独奏（只要有独奏，其它路一律静音）
    "remote_solo":    False,
    "sys_gain":       1.0,
    "remote_gain":    10.0,
}


def mix_params() -> dict:
    return _mix


def set_mix(**kw) -> dict:
    """更新混音参数（夹取数值范围），返回更新后的快照。"""
    for k, v in kw.items():
        if k not in _mix:
            continue
        if k in ("sys_gain", "remote_gain"):
            try:
                _mix[k] = max(0.0, min(10.0 if k == "sys_gain" else 30.0, float(v)))
            except (TypeError, ValueError):
                pass
        else:
            _mix[k] = bool(v)
    return dict(_mix)


def source_audible(which: str) -> bool:
    """该路音频此刻是否应该被听到（综合 参与混音 / 静音 / 独奏）。

    独奏语义：**只要有任意一路处于独奏，其它路就一律静音** ——
    这是调音台的标准行为，用户想单独听某一路时不用去手动静音另一路。
    """
    solo_any = _mix["sys_solo"] or _mix["remote_solo"]
    if which == "sys":
        if _mix["sys_solo"]:
            return True
        if solo_any or _mix["sys_muted"] or not _mix["sys_enabled"]:
            return False
        return True
    if which == "remote":
        if _mix["remote_solo"]:
            return True
        if solo_any or _mix["remote_muted"] or not _mix["remote_enabled"]:
            return False
        return True
    return False


def get() -> BridgeState:
    """Return a snapshot copy (safe to read from any thread)."""
    with _lock:
        return BridgeState(**asdict(_state))


def update(**kw) -> None:
    """Patch one or more fields and notify listeners."""
    with _lock:
        for k, v in kw.items():
            if hasattr(_state, k):
                setattr(_state, k, v)
        _state.updated_at = time.time()
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass


def push_audio(points: list[int], level: int, frames: int, peak: int,
               sample_rate: int = 0, frame_bytes: int = 0) -> None:
    """高频写入路径：推送波形点 + 刷新诊断指标。

    刻意不走 update() —— 这里是每帧（约每 20-30ms）调用一次，
    不该每次都对所有 listener 做一遍回调。
    """
    with _lock:
        _wave.extend(points)
        _state.level         = level
        _state.audio_frames  = frames
        _state.audio_peak    = peak
        _state.audio_last_at = time.time()
        if sample_rate:
            _state.sample_rate = sample_rate
        if frame_bytes:
            _state.frame_bytes = frame_bytes
        _state.updated_at    = time.time()


def wave_snapshot() -> list[int]:
    """取一份当前波形（拷贝，安全）。"""
    with _lock:
        return list(_wave)


def wave_snapshot_sys() -> list[int]:
    """电脑麦克风的波形（拷贝，安全）。"""
    with _lock:
        return list(_wave_sys)


def push_sys_audio(points: list[int]) -> None:
    """电脑麦克风的高频写入路径（只更新波形，电平走 push_levels）。"""
    with _lock:
        _wave_sys.extend(points)


def push_mix_audio(points: list[int]) -> None:
    """混合输出的高频写入路径。

    在音频播放回调里调用 —— 那是端口音频自己的实时线程，
    所以这里只做一次 deque.extend，不做任何格式化/分配。
    """
    with _lock:
        _wave_mix.extend(points)


def waves_payload(n: int = _WAVE_WIRE) -> dict:
    """三路波形，各取最近 n 点，给前端画图用。

    统一在这里裁长度，前端就不必关心环形缓冲有多大。
    """
    with _lock:
        return {
            "sys":    list(_wave_sys)[-n:],
            "remote": list(_wave)[-n:],
            "mix":    list(_wave_mix)[-n:],
        }


def push_levels(sys_db: float | None = None,
                remote_db: float | None = None,
                mix_db: float | None = None) -> None:
    """更新三路电平（dBFS）。

    用 None 表示"这次不更新这一路" —— 遥控器没在推流时它的电平就该停在原位，
    不能因为混音器还在跑就把遥控器那一格刷成静音。
    """
    with _lock:
        if sys_db is not None:
            _state.sys_level_db = sys_db
        if remote_db is not None:
            _state.remote_level_db = remote_db
        if mix_db is not None:
            _state.mix_level_db = mix_db


def clear_audio() -> None:
    """开一次新的语音会话时清空波形与计数。"""
    with _lock:
        _wave.clear()
        _state.audio_frames  = 0
        _state.audio_peak    = 0
        _state.audio_last_at = 0.0
        _state.level         = 0
        # 遥控器电平也归零：新一次会话从"还没声音"开始画，
        # 否则上一段留下的读数会让「遥控器麦克风」一开始就是"有声音"。
        _state.remote_level_db = -96.0


def reset() -> None:
    """Clear transient runtime state (on disconnect / shutdown)."""
    update(connected=False, streaming=False, level=0, device="")
    clear_audio()
    with _lock:
        _wave_sys.clear()
        _wave_mix.clear()
        _state.sys_level_db    = -96.0
        _state.remote_level_db = -96.0
        _state.mix_level_db    = -96.0
        _state.sys_mic_ready   = False


def on_change(fn: Callable[[], None]) -> None:
    """Register a listener invoked (on a background thread) after updates."""
    _listeners.append(fn)
