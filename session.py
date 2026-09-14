"""
Session coordinator — mirrors vRemoter's X6SessionCoordinator.
Manages the voice session state machine: closed → opening → open → closing.
"""

from __future__ import annotations
import logging
import threading
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
    # 本次语音会话里 MIC_OPEN 是否已经真正发出去了。
    # 用它而不是 open_attempts 做判断：attempts 在失败重试时也会自增。
    mic_open_sent: bool = False


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
        # ⚠ 定时器必须用 threading.Timer，不能用 asyncio.call_later。
        # 这个类的所有公开方法都从 **BLE 通知回调线程**（WinRT/COM 线程池，
        # 线程名 Dummy-XXXX）调用，那里没有事件循环，
        # asyncio.get_event_loop() 会抛
        # "There is no current event loop in thread 'Dummy-XXXX'"
        # —— v1.0.3 真机事故：MIC_OPEN/MIC_CLOSE 一次都没真正发出，
        # 根子就是这些调度全在回调线程上炸掉了。
        self._open_timer:  Optional[threading.Timer] = None
        self._close_timer: Optional[threading.Timer] = None

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

    def ensure_mic_open(self) -> bool:
        """确保本次语音会话已经发出过 MIC_OPEN（幂等，已发过就跳过）。

        为什么单独加这个入口：真机实测发现 Chromecast 遥控器按语音键时，
        ATVV 控制通道只会上报 AUDIO_START(0x04)，**从来没有 START_SEARCH(0x08)**。
        而 openMicrophone() 原本只挂在 start_search 分支上 ——
        于是 MIC_OPEN 一次都没发出去，遥控器不推流，
        日志表现就是「Audio STOP（本次共收到 0 个音频帧）」连续 0 帧。

        遥控器必须先收到 MIC_OPEN 才会真正上传麦克风数据，所以在 AUDIO_START
        到达时补发一次是安全的：已发过则直接返回，不会重复开麦。

        ⚠ 这里**刻意不走 `_try_open()`**：调用点就在 AUDIO_START 分支里，
        而 `on_audio_start()` 已经把相位推到 OPEN，`_try_open()` 开头的
        `if self.state.phase != Phase.CLOSED: return` 会把整件事吞掉，
        mic_open_sent 永远是 False —— 那这个补发就等于没写。
        本方法只认 `mic_open_sent` 这一个标记，与相位解耦。

        返回 True 表示本次调用真的发出了 MIC_OPEN。
        """
        if self.state.mic_open_sent:
            return False
        try:
            cmd = self._on_mic_open(self.state.stream_id)
        except Exception as e:                      # noqa: BLE001
            logger.error(f"ensure_mic_open failed: {e}")
            return False
        if not cmd:
            return False
        self.state.mic_open_sent = True
        logger.info("📤 MIC_OPEN 补发（AUDIO_START 触发，遥控器此前从未收到开麦命令）")
        return True

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
            if self._open_timer:            # 先撤旧的再挂新的，别让旧定时器到点空响
                self._open_timer.cancel()
            self._open_timer = self._schedule(self.OPEN_TIMEOUT, self._open_timeout)
        else:
            logger.warning(f"MIC_OPEN failed: code=0x{code:04X}")
            self.state.open_attempts += 1
            if self.state.open_attempts < self.MAX_ATTEMPTS:
                self._schedule(1.0, self._retry_open)
            else:
                self._set(Phase.CLOSED, "mic_open max retries exceeded")

    # ── Internal ───────────────────────────────────────────────────────────
    @staticmethod
    def _schedule(delay: float, fn: Callable[[], None]) -> threading.Timer:
        """线程安全的延时调度（BLE 回调线程里没有 asyncio 事件循环可用）。"""
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        return t

    def _try_open(self) -> None:
        if self.state.phase != Phase.CLOSED:
            return
        self._set(Phase.OPENING, "voice_key_down")
        cmd = self._on_mic_open(self.state.stream_id)
        if cmd:
            logger.info(f"📤 Sent MIC_OPEN (attempt {self.state.open_attempts+1})")
            self.state.mic_open_sent = True
            self.state.open_attempts += 1
            self._open_timer = self._schedule(self.OPEN_TIMEOUT, self._open_timeout)
        else:
            self._set(Phase.CLOSED, "mic_open command returned None")

    def _try_close(self) -> None:
        if self.state.phase != Phase.OPEN:
            return
        self._set(Phase.CLOSING, "voice_key_up")
        self._on_mic_close(self.state.stream_id)
        self._close_timer = self._schedule(self.CLOSE_TIMEOUT, self._close_timeout)

    def _set(self, phase: Phase, reason: str) -> None:
        self.state.phase = phase
        # 会话结束就清掉「本次已开麦」标记，否则第二次按语音键时
        # ensure_mic_open() 会以为已经发过而直接返回 —— 第一次能用、
        # 之后每次都 0 帧，是最难排查的那类"偶发"故障。
        if phase == Phase.CLOSED:
            self.state.mic_open_sent = False
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
