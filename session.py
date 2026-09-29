"""
Session coordinator — mirrors vRemoter's X6SessionCoordinator.
Manages the voice session state machine: closed → opening → open → closing.

⚠ 线程模型（2026-09-29 审查报告 P2-3 之后必须知道）
--------------------------------------------------
本类的公开方法来自**两个**不同的线程：

  ① BLE 通知回调线程（WinRT/COM 线程池，线程名 `Dummy-XXXX`）——
     `on_control` 里的 `on_audio_start` / `on_audio_stop` /
     `on_mic_open_result` / `ensure_mic_open` / `close` 全在这儿；
  ② `threading.Timer` 的定时器线程 —— `_open_timeout` / `_close_timeout` /
     `_retry_open`。

两边会**同时**读写 `state.phase` / `state.mic_open_sent`。没有锁的话，
一次"超时收尾"和一次"audio_start 确认"交错就可能留下
`phase=CLOSED` 而 `mic_open_sent=True` 这种自相矛盾的状态 ——
表现是"下一次按语音键没反应"（静默失效，最难查的那一类）。
⇒ 所有状态迁移都在 `self._lock`（RLock）里做。

另外：**所有**定时器都必须登记进 `self._timers`，并由 `shutdown()` 统一取消。
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

        # ⚠ 一把**可重入**锁保护所有状态迁移（P2-3）。
        #   RLock 而不是 Lock：公开方法之间会互相调用
        #   （voice_key_down → _try_open → _set → on_phase 回调里又可能回调进来），
        #   用 Lock 会自己把自己锁死。
        self._lock = threading.RLock()

        # ⚠ 定时器必须用 threading.Timer，不能用 asyncio.call_later。
        # 这个类的所有公开方法都从 **BLE 通知回调线程**（WinRT/COM 线程池，
        # 线程名 Dummy-XXXX）调用，那里没有事件循环，
        # asyncio.get_event_loop() 会抛
        # "There is no current event loop in thread 'Dummy-XXXX'"
        # —— v1.0.3 真机事故：MIC_OPEN/MIC_CLOSE 一次都没真正发出，
        # 根子就是这些调度全在回调线程上炸掉了。
        self._open_timer:  Optional[threading.Timer] = None
        self._close_timer: Optional[threading.Timer] = None
        self._retry_timer: Optional[threading.Timer] = None

        # ⚠ **所有**在跑的定时器都要在这里登记（P2-3）。
        #   原先 `_retry_open` 的定时器根本没被持有
        #   （`self._schedule(1.0, self._retry_open)` 的返回值直接丢了），
        #   于是 `close()` 取消不到它：会话已经结束、甚至链路已经重连，
        #   它到点照样把相位推回 OPENING 并**再发一次 MIC_OPEN**。
        #   这就是审查报告说的"旧定时器可能跨重连执行"。
        self._timers: set[threading.Timer] = set()

        # shutdown 之后本对象即作废 —— 所有入口先看这个标记（见 shutdown）。
        self._closed = False

    # ── Public API ─────────────────────────────────────────────────────────
    @property
    def is_closed(self) -> bool:
        """本对象是否已 shutdown（作废）。"""
        return self._closed

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode

    def voice_key_down(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._mode == "hold":
                self._try_open()
            else:
                if not self._held:
                    self._held = True
                    self._try_open()

    def voice_key_up(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._held = False
            if self._mode == "hold":
                self._try_close()
            # toggle: close on next audio_stop

    def close(self, reason: str = "session end") -> bool:
        """**无条件**收尾：发 MIC_CLOSE + 相位归 CLOSED（幂等）。返回是否真发了命令。

        ⚠⚠ 为什么不能拿 `voice_key_up()` / `_try_close()` 当收尾入口 ——
        这是 2026-09-29 审查报告 P0-1 的核心，也是真机上「会话结束了、
        遥控器还在推流」的直接原因。那两条路各有一道门槛：

            voice_key_up()  → 只在 `_mode == "hold"` 时才调 `_try_close()`
                              （toggle 模式干脆什么都不做）
            _try_close()    → 开头就 `if self.state.phase != Phase.OPEN: return`

        而会话收尾有 **6 条**路径（再按语音键 / 键盘确认键 / 厂商页确认键 /
        超时 / 断连 / 退出），其中"确认键""超时""断连""退出"这几条走到这里时，
        相位往往**已经不是 OPEN** 了（audio_stop 早就把它打回 CLOSED）。
        于是这两道门槛一起把它挡在外面 → MIC_CLOSE 一次都没发出去 →
        遥控器那头以为会话还在，**继续推流**。
        真机实证（2026-09-29 11:26:34 结束）：日志里再没有任何 MIC_CLOSE，
        音频帧从 600 一路涨到 3761，多推了 52 秒。

        ⇒ 收尾必须有一条**不看模式、不看相位、必定发命令**的路，就是这里。
        """
        with self._lock:
            if self._closed:
                # 已作废 ⇒ 不再往链路里发东西（但相位归位还是要做，
                # 免得调用方看到一个永远停在 OPEN 的对象）。
                self._held = False
                self._set(Phase.CLOSED, reason)
                return False
            self._held = False
            self._cancel_all_timers()

            sent = False
            try:
                sent = bool(self._on_mic_close(self.state.stream_id))
            except Exception as e:                      # noqa: BLE001
                logger.error(f"MIC_CLOSE 发送失败：{e}")
            # 相位归 CLOSED —— 顺带把 `mic_open_sent` 复位（见 _set），
            # 否则下一段语音的 `ensure_mic_open()` 会以为已经发过而直接返回。
            self._set(Phase.CLOSED, reason)
            return sent

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
        with self._lock:
            if self._closed:
                return False
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

    def mark_mic_open_failed(self) -> bool:
        """MIC_OPEN **真实写入失败** → 撤销「本次已开麦」标记（P2-2）。

        返回 True 表示确实撤销了（原来是 True）。由 main.py 的
        `_on_mic_open_done(False)` 调用。

        ⚠ 为什么要走方法而不是让 main.py 直接改 `state.mic_open_sent`：
          这个字段是**状态机的一部分**，必须和相位迁移在同一把锁里改。
          绕过去改的话，一次并发的 `_set(CLOSED)` 或 `ensure_mic_open()`
          就可能读到中间态。
        """
        with self._lock:
            if self.state.mic_open_sent:
                self.state.mic_open_sent = False
                return True
            return False

    def on_audio_start(self, codec: int, stream_id: int) -> None:
        with self._lock:
            if self._closed:
                return
            self.state.codec      = codec
            self.state.stream_id  = stream_id
            self.state.open_attempts = 0
            if self._open_timer:
                self._open_timer.cancel()
                self._timers.discard(self._open_timer)
                self._open_timer = None
            if self.state.phase == Phase.OPENING:
                self._set(Phase.OPEN, "audio_start confirmed")
            elif self.state.phase == Phase.CLOSED:
                self._set(Phase.OPEN, "audio_start unsolicited")

    def on_audio_stop(self, reason: int = 0) -> None:
        with self._lock:
            if self._closed:
                return
            if self.state.phase in (Phase.OPEN, Phase.OPENING, Phase.CLOSING):
                if self._close_timer:
                    self._close_timer.cancel()
                    self._timers.discard(self._close_timer)
                    self._close_timer = None
                # toggle 模式：必须清掉按住标记，否则第二次按语音键会被
                # voice_key_down() 的 `if not self._held` 挡掉，会话永久卡死。
                self._held = False
                self._set(Phase.CLOSED, f"audio_stop reason=0x{reason:02X}")

    def on_mic_open_result(self, code: int) -> None:
        with self._lock:
            if self._closed:
                return
            if code == 0:
                if self._open_timer:            # 先撤旧的再挂新的，别让旧定时器到点空响
                    self._open_timer.cancel()
                    self._timers.discard(self._open_timer)
                self._open_timer = self._schedule(self.OPEN_TIMEOUT, self._open_timeout)
            else:
                logger.warning(f"MIC_OPEN failed: code=0x{code:04X}")
                self.state.open_attempts += 1
                if self.state.open_attempts < self.MAX_ATTEMPTS:
                    # ⚠ 这个定时器**必须**记下来（P2-3）：原先它的返回值直接丢了，
                    #   `close()` / `shutdown()` 都取消不到它 —— 会话关了、链路重连了，
                    #   它到点还会把相位推回 OPENING 并再发一次 MIC_OPEN。
                    if self._retry_timer:
                        self._retry_timer.cancel()
                        self._timers.discard(self._retry_timer)
                    self._retry_timer = self._schedule(1.0, self._retry_open)
                else:
                    self._set(Phase.CLOSED, "mic_open max retries exceeded")

    def shutdown(self) -> None:
        """彻底停掉这个协调器：取消所有定时器，之后所有入口变 no-op。

        ⚠ 为什么 `close()` 不够（2026-09-29 审查报告 P2-3）：
          `close()` 是"**收尾一次会话**"—— 它取消定时器，但对象还是活的，
          下一条语音照样能用。真正的场景是**重连 / 退出**：旧的
          SessionCoordinator 被丢掉，而它排出去的定时器还挂在 threading 里，
          到点就 `_retry_open()` —— 把相位推回 OPENING、**再发一次 MIC_OPEN**。
          此时链路可能已经换了一条，这条命令要么白发（噪声），要么把
          新会话的状态搅乱。

          所以需要一个"**这个对象从此作废**"的硬标记 `_closed`：
          定时器到点先看它，是 True 就直接返回 —— 不依赖"取消得够快"
          （`Timer.cancel()` 对已经开始执行的定时器是无效的）。
        """
        with self._lock:
            self._closed = True
            self._cancel_all_timers()
            self._held = False
        logger.debug("SessionCoordinator 已 shutdown（定时器全取消，之后入口一律 no-op）")

    # ── Internal ───────────────────────────────────────────────────────────
    def _cancel_all_timers(self) -> None:
        """取消并清空所有在跑的定时器。**只在持锁时调用。**"""
        for t in list(self._timers):
            try:
                t.cancel()
            except Exception:                           # noqa: BLE001
                pass
        self._timers.clear()
        self._open_timer = None
        self._close_timer = None
        self._retry_timer = None

    def _schedule(self, delay: float, fn: Callable[[], None]) -> threading.Timer:
        """线程安全的延时调度（BLE 回调线程里没有 asyncio 事件循环可用）。

        ⚠ 每个定时器都登记进 `_timers`，并在**触发时把自己摘掉** ——
          不登记的话 `close()` / `shutdown()` 取消不到它。
        ⚠ `_closed` 这道门放在**定时器回调里**（而不是只靠 `cancel()`）：
          `Timer.cancel()` 对"已经进入回调"的定时器无效，只能靠标记挡住。
        """
        def _run() -> None:
            self._timers.discard(t)
            if self._closed:
                logger.debug("定时器在 shutdown 之后到点 → 忽略")
                return
            try:
                fn()
            except Exception as e:                      # noqa: BLE001
                logger.error("定时器回调失败：%s", e)

        t = threading.Timer(delay, _run)
        t.daemon = True
        self._timers.add(t)
        t.start()
        return t

    def _try_open(self) -> None:
        # ⚠ 调用方已经持锁（voice_key_down / _retry_open）—— RLock 可重入。
        if self._closed:
            return
        if self.state.phase != Phase.CLOSED:
            return
        self._set(Phase.OPENING, "voice_key_down")
        cmd = self._on_mic_open(self.state.stream_id)
        if cmd:
            logger.info(f"📤 Sent MIC_OPEN (attempt {self.state.open_attempts+1})")
            self.state.mic_open_sent = True
            self.state.open_attempts += 1
            if self._open_timer:
                self._open_timer.cancel()
                self._timers.discard(self._open_timer)
            self._open_timer = self._schedule(self.OPEN_TIMEOUT, self._open_timeout)
        else:
            self._set(Phase.CLOSED, "mic_open command returned None")

    def _try_close(self) -> None:
        # ⚠ 调用方已经持锁（voice_key_up）—— RLock 可重入。
        if self._closed:
            return
        if self.state.phase != Phase.OPEN:
            return
        self._set(Phase.CLOSING, "voice_key_up")
        self._on_mic_close(self.state.stream_id)
        if self._close_timer:
            self._close_timer.cancel()
            self._timers.discard(self._close_timer)
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
        with self._lock:
            if self._closed:
                return
            if self.state.phase == Phase.OPENING:
                logger.warning("Audio start timeout → closed")
                self._set(Phase.CLOSED, "open timeout")

    def _close_timeout(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self.state.phase == Phase.CLOSING:
                logger.warning("Close verify timeout → closed")
                self._set(Phase.CLOSED, "close timeout")

    def _retry_open(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self.state.phase == Phase.CLOSED:
                self._try_open()
