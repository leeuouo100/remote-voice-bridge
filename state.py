"""
Shared bridge state — thread-safe handoff between the asyncio bridge
thread and the tray/UI thread.

The bridge runs in its own thread (asyncio loop); the tray icon and the
console window run in others. This module is the single source of truth
both sides read from, so no locks are held across UI calls.
"""

from __future__ import annotations
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Callable

_lock = threading.RLock()
_listeners: list[Callable[[], None]] = []

# 波形环形缓冲：on_audio 每来一帧就往里推几个代表点。
# 放在 BridgeState 之外，因为它是每帧高频写入的原始数据，
# 不该跟着 state.get() 一起被 asdict 拷贝。
_WAVE_POINTS = 256
_wave: deque[int] = deque(maxlen=_WAVE_POINTS)


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

    def as_dict(self) -> dict:
        return asdict(self)


_state = BridgeState()


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


def clear_audio() -> None:
    """开一次新的语音会话时清空波形与计数。"""
    with _lock:
        _wave.clear()
        _state.audio_frames  = 0
        _state.audio_peak    = 0
        _state.audio_last_at = 0.0
        _state.level         = 0


def reset() -> None:
    """Clear transient runtime state (on disconnect / shutdown)."""
    update(connected=False, streaming=False, level=0, device="")
    clear_audio()


def on_change(fn: Callable[[], None]) -> None:
    """Register a listener invoked (on a background thread) after updates."""
    _listeners.append(fn)
