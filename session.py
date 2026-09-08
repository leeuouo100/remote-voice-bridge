"""
Session coordinator — mirrors vRemoter's X6SessionCoordinator.
Manages the voice session state machine: closed → opening → open → closing.
"""

from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger("rvb.session")


class Phase(str, Enum):
    CLOSED   = "closed"
    OPENING  = "opening"
    OPEN     = "open"
    CLOSING  = "closing"


@dataclass
class SessionState:
    phase: Phase        = Phase.CLOSED
    stream_id: int      = 0
    codec: int          = 0
    open_attempts: int  = 0


class SessionCoordinator:
    """
    Coordinates ATVV voice session lifecycle.

    Parameters
    ----------
    on_mic_open  : (stream_id: int) -> bytes | None
        Return MIC_OPEN command bytes, or None to skip.
    on_mic_close : (stream_id: int) -> None
    on_phase_change : (phase: Phase, reason: str) -> None
    """

    MAX_ATTEMPTS    = 3
    OPEN_TIMEOUT    = 1.0   # seconds to wait for AUDIO_START after MIC_OPEN
    CLOSE_TIMEOUT   = 1.2

    def __init__(
        self,
        on_mic_open:  Optional[Callable[[int], Optional[bytes]]] = None,
        on_mic_close: Optional[Callable[[int], None]]           = None,
        on_phase:     Optional[Callable[[Phase, str], None]]    = None,
    ):
        self._on_mic_open  = on_mic_open  or (lambda sid: None)
        self._on_mic_close = on_mic_close or (lambda sid: None)
        self._on_phase     = on_phase

        self.state  = SessionState()
        self._mode  = "toggle"   # "toggle" or "hold"
        self._held  = False
        self._open_timer:  Optional[asyncio.TimerHandle] = None
        self._close_timer: Optional[asyncio.TimerHandle] = None

    # ── Public API ─────────────────────────────────────────────────────────
    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def voice_key_down(self) -> None:
        if self._mode == "hold":
            self._try_open()
        else:
            if not self._held:
                self._held = True
                self._try_open()

    def voice_key_up(self) -> None:
        self._held = False
        if self._mode == "hold":
            self._try_close()
        # toggle: close on next audio_stop

    def on_audio_start(self, codec: int, stream_id: int) -> None:
        self.state.codec      = codec
        self.state.stream_id  = stream_id
        self.state.open_attempts = 0
        if self._open_timer:
            self._open_timer.cancel()
            self._open_timer = None
        if self.state.phase == Phase.OPENING:
            self._set(Phase.OPEN, "audio_start confirmed")
        elif self.state.phase == Phase.CLOSED:
            self._set(Phase.OPEN, "audio_start unsolicited")

    def on_audio_stop(self, reason: int = 0) -> None:
        if self.state.phase in (Phase.OPEN, Phase.OPENING, Phase.CLOSING):
            if self._close_timer:
                self._close_timer.cancel()
                self._close_timer = None
            # toggle 模式：必须清掉按住标记，否则第二次按语音键会被
            # voice_key_down() 的 `if not self._held` 挡掉，会话永久卡死。
            self._held = False
            self._set(Phase.CLOSED, f"audio_stop reason=0x{reason:02X}")

    def on_mic_open_result(self, code: int) -> None:
        if code == 0:
            loop = asyncio.get_event_loop()
            self._open_timer = loop.call_later(self.OPEN_TIMEOUT, self._open_timeout)
        else:
            logger.warning(f"MIC_OPEN failed: code=0x{code:04X}")
            self.state.open_attempts += 1
            if self.state.open_attempts < self.MAX_ATTEMPTS:
                loop = asyncio.get_event_loop()
                loop.call_later(1.0, self._retry_open)
            else:
                self._set(Phase.CLOSED, "mic_open max retries exceeded")

    # ── Internal ───────────────────────────────────────────────────────────
    def _try_open(self) -> None:
        if self.state.phase != Phase.CLOSED:
            return
        self._set(Phase.OPENING, "voice_key_down")
        cmd = self._on_mic_open(self.state.stream_id)
        if cmd:
            logger.info(f"Sent MIC_OPEN (attempt {self.state.open_attempts+1})")
            self.state.open_attempts += 1
            loop = asyncio.get_event_loop()
            self._open_timer = loop.call_later(self.OPEN_TIMEOUT, self._open_timeout)
        else:
            self._set(Phase.CLOSED, "mic_open command returned None")

    def _try_close(self) -> None:
        if self.state.phase != Phase.OPEN:
            return
        self._set(Phase.CLOSING, "voice_key_up")
        self._on_mic_close(self.state.stream_id)
        loop = asyncio.get_event_loop()
        self._close_timer = loop.call_later(self.CLOSE_TIMEOUT, self._close_timeout)

    def _set(self, phase: Phase, reason: str) -> None:
        self.state.phase = phase
        logger.debug(f"Phase → {phase.value}  ({reason})")
        if self._on_phase:
            self._on_phase(phase, reason)

    def _open_timeout(self) -> None:
        if self.state.phase == Phase.OPENING:
            logger.warning("Audio start timeout → closed")
            self._set(Phase.CLOSED, "open timeout")

    def _close_timeout(self) -> None:
        if self.state.phase == Phase.CLOSING:
            logger.warning("Close verify timeout → closed")
            self._set(Phase.CLOSED, "close timeout")

    def _retry_open(self) -> None:
        if self.state.phase == Phase.CLOSED:
            self._try_open()
