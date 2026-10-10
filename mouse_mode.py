"""
鼠标模式 —— 用遥控器方向键控制指针（v1.0.31）。

为什么要独立成一个模块
----------------------
移动引擎是**纯逻辑**：输入 = (方向, 按下/松开, 时刻)，输出 = 位移序列。
把它从 `main.py` 里抠出来，闸门（`tools/check_mouse_mode.py`）就能
**完全不碰硬件**地跑真值表 —— 这是它能进 CI 安全集的原因，也是这个项目
一贯的规矩（"闸门结论不许由跑闸那台机器决定"）。

为什么不用遥控器自己的"连发"
----------------------------
2026-10-10 真机实测：按住「方向右」1.28 秒，日志里只有**一对**按下/松开
（`15:48:40,916 按下` → `15:48:42,196 松开`）。原因是厂商页报告是**状态型**的
（`remote_hid.py:decode_report` 里 `payload[0]` = 当前按下的 usage，`0` = 没按），
而 `frida_tap.js` 只在内容**变化**时才上报 ⇒ 按住不动时一个事件都不会再来。

所以移动只能由我们自己的定时器产生。这反而是好事：
  · 节奏由我们定，不会一顿一顿；
  · 加速度曲线能做；
  · 按下/松开只是"开始/停止"两个沿，语义干净。

对角线为什么不做
----------------
同一次实测还发现：**报告一次只能带一个 usage**。同时按住「上」和「右」，
程序只会看到最后那一个（另有一条硬证据：`15:48:29` 的 `up` 和 `15:48:39` 的
`vol_down` 各丢了一次"松开"——因为报告被后一个键顶掉了）。
⇒ 四方向就是硬件上限。与其做一个"时灵时不灵"的对角线，不如不做，
并把这条写进文档（省得下次又有人去试）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("rvb.mouse")

# 方向 → 单位向量（屏幕坐标系：y 向下为正）
DIRECTIONS: dict[str, tuple[int, int]] = {
    "up":    (0, -1),
    "down":  (0,  1),
    "left":  (-1, 0),
    "right": (1,  0),
}

_TICK_HZ = 60.0
_TICK = 1.0 / _TICK_HZ

# 丢"松开"的兜底超时。厂商页报告一次只带一个键（见模块开头），所以
# "按住 A 再按 B"时 A 的松开永远到不了。`main.py` 那边会在"新键按下"时
# 立刻清掉旧方向（正常路径），这里的超时是**最后一道防线** ——
# 万一那条路也没兜住，指针不能一直往一个方向跑下去。
MAX_HOLD_SECONDS = 10.0

# 两个方向同时被认为按住时（只可能来自丢松开）把速度压回 1.0 倍，
# 否则斜着会快 √2 倍。正常路径走不到这里，但走到了也不能让它失控。
_DIAGONAL_DAMP = 0.7071067811865476


class MouseMover:
    """方向键 → 指针位移。

    线程安全：`press` / `release` 会被 BLE 回调线程调用，`tick` 跑在自己的
    60 Hz 线程上，`configure` 由控制台线程调用。
    """

    def __init__(
        self,
        move: Callable[[int, int], object] | None = None,
        base_speed: float = 5.0,
        max_speed: float = 20.0,
        accel_ms: int = 800,
        clock: Callable[[], float] | None = None,
        autostart: bool = True,
    ) -> None:
        """
        `move`    注入原语（默认 `keys.mouse_move`）。闸门传一个记录调用的桩。
        `clock`   时间源（默认 `time.monotonic`）。闸门传假时钟。
        `autostart=False` ⇒ `press()` **不**起后台线程，由调用方手动 `tick()`。
                   ⚠ 这是给 `tools/check_mouse_mode.py` 用的：真起 60Hz 线程的话，
                   它会拿**真**时钟和假时钟抢着调 `tick()`，真值表就没法确定了。
        """
        if move is None:
            # 延迟导入：闸门要能脱离 keys 单独测这个模块。
            import keys as _keys
            move = _keys.mouse_move
        self._move = move
        self._autostart = bool(autostart)
        self._base = max(0.1, float(base_speed))
        self._max = max(self._base, float(max_speed))
        self._accel_ms = max(0, int(accel_ms))
        self._clock = clock or time.monotonic

        self._lock = threading.RLock()
        self._dirs: set[str] = set()
        self._since = 0.0          # 这一批方向是从什么时候开始按住的
        self._last_event = 0.0     # 最后一次收到按下/松开的时刻
        self._carry_x = 0.0        # 亚像素余量
        self._carry_y = 0.0
        self._moved = 0            # 累计走了多少像素（诊断用）
        self._stuck = 0            # 兜底停了几次（诊断用）
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    # ── 配置（控制台的滑块热改，不用重连）──────────────────────────────────
    def configure(self, base_speed: float | None = None,
                  max_speed: float | None = None,
                  accel_ms: int | None = None) -> None:
        with self._lock:
            if base_speed is not None:
                self._base = max(0.1, float(base_speed))
            if max_speed is not None:
                self._max = max(self._base, float(max_speed))
            if accel_ms is not None:
                self._accel_ms = max(0, int(accel_ms))

    # ── 事件 ───────────────────────────────────────────────────────────────
    def press(self, direction: str) -> None:
        if direction not in DIRECTIONS:
            return
        now = self._clock()
        with self._lock:
            fresh = not self._dirs          # 从静止起步 ⇒ 加速度从头算、余量清零
            self._dirs.add(direction)
            self._last_event = now
            if fresh:
                self._since = now
                self._carry_x = 0.0
                self._carry_y = 0.0
        if self._autostart:
            self._ensure_thread()
            self._wake.set()

    def release(self, direction: str) -> None:
        with self._lock:
            self._dirs.discard(direction)
            self._last_event = self._clock()
            if not self._dirs:
                self._carry_x = 0.0
                self._carry_y = 0.0

    def release_all(self) -> None:
        """把所有"还认为按住"的方向一次清掉。

        `main.py` 在**任何新键按下**时都会先调它 —— 因为报告一次只带一个键，
        新键按下在逻辑上就蕴含"旧键已经不在按了"。不这么做的话，旧方向的
        松开事件永远等不到，指针会一直往那边跑。
        """
        with self._lock:
            self._dirs.clear()
            self._carry_x = 0.0
            self._carry_y = 0.0
            self._last_event = self._clock()

    # ── 查询（给 UI / 诊断）────────────────────────────────────────────────
    def active(self) -> bool:
        return bool(self._dirs)

    def directions(self) -> list[str]:
        return sorted(self._dirs)

    def speed_now(self) -> float:
        """当前每帧走多少像素（控制台显示用）。"""
        with self._lock:
            if not self._dirs:
                return 0.0
            return self._frame_step(self._clock() - self._since)

    def moved_px(self) -> int:
        return self._moved

    def stuck_stops(self) -> int:
        return self._stuck

    # ── 曲线 ───────────────────────────────────────────────────────────────
    def _frame_step(self, hold_s: float) -> float:
        """按住 hold_s 秒之后，这一帧该走多少像素。

        曲线是**二次**的（t²）：起步阶段很慢（几像素一帧 ⇒ 能精细地停在按钮上），
        按住 `accel_ms` 之后迅速加到上限（跨屏不用等）。
        线性曲线的手感是"一开始就太快、后面又不够快"，两头都不对。

        `accel_ms <= 0` ⇒ 退化成固定速度（闸门会两种都验）。
        """
        if self._accel_ms <= 0 or self._max <= self._base:
            return self._base
        t = max(0.0, min(1.0, hold_s * 1000.0 / self._accel_ms))
        return self._base + (self._max - self._base) * (t * t)

    # ── 每帧 ───────────────────────────────────────────────────────────────
    def tick(self, now: float | None = None) -> tuple[int, int]:
        """推进一帧，返回这一帧**实际发出**的位移（闸门靠它断言）。"""
        now = self._clock() if now is None else now
        with self._lock:
            if not self._dirs:
                return (0, 0)

            # 最后一道防线：松开丢了 ⇒ 别让指针一直跑
            if (now - self._last_event) > MAX_HOLD_SECONDS:
                names = "、".join(sorted(self._dirs))
                self._dirs.clear()
                self._carry_x = 0.0
                self._carry_y = 0.0
                self._stuck += 1
                logger.warning(
                    "🖱 鼠标模式：方向键「%s」按下超过 %.0f 秒都没等到松开 → "
                    "已强制停下。多半是按住它的时候又按了别的键"
                    "（遥控器的报告一次只带一个键，前一个键的松开就丢了）。",
                    names, MAX_HOLD_SECONDS)
                return (0, 0)

            step = self._frame_step(now - self._since)
            vx = sum(DIRECTIONS[d][0] for d in self._dirs)
            vy = sum(DIRECTIONS[d][1] for d in self._dirs)
            if vx and vy:
                vx *= _DIAGONAL_DAMP
                vy *= _DIAGONAL_DAMP
            # 亚像素余量：慢速时一帧可能连一个像素都不到，直接 int() 会把
            # 它永远抹成 0 ⇒ 表现为"慢慢推指针纹丝不动"。余量必须留着累加。
            self._carry_x += vx * step
            self._carry_y += vy * step
            ix = int(self._carry_x)         # 向零取整 ⇒ 余量保留正确的符号
            iy = int(self._carry_y)
            if ix == 0 and iy == 0:
                return (0, 0)               # 还不够一个像素 → 这一帧不发事件
            self._carry_x -= ix
            self._carry_y -= iy

        # ⚠ 注入必须在**锁外**做：`SendInput` 是系统调用，持锁调它会把
        #   `release()` 卡住 —— 而 release 是 BLE 回调线程在等。那就会变成
        #   "手都松了指针还在走"。宁可让两帧的顺序偶尔换一下。
        try:
            self._move(ix, iy)
        except Exception as e:                            # noqa: BLE001
            logger.debug("鼠标注入失败（已忽略）：%r", e)
        with self._lock:
            self._moved += abs(ix) + abs(iy)
        return (ix, iy)

    # ── 线程 ───────────────────────────────────────────────────────────────
    def _ensure_thread(self) -> None:
        t = self._thread
        if t is not None and t.is_alive():
            return
        with self._lock:
            t = self._thread
            if t is not None and t.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(target=self._run, name="rvb-mouse", daemon=True)
            self._thread = t
            t.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            # `bool(set)` 在 GIL 下是原子的，这里不加锁是安全的。
            if not self._dirs:
                # 空闲：睡着等 `press()` 叫醒。0.5 秒的兜底只是为了
                # `stop()` 之后线程能退出来，不是轮询。
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                self.tick()
            except Exception:                             # noqa: BLE001
                logger.exception("鼠标移动线程异常（已跳过这一帧）")
            time.sleep(_TICK)

    def stop(self) -> None:
        self.release_all()
        self._stop.set()
        self._wake.set()
