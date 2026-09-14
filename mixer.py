"""
mixer.py — 三路音频：电脑麦克风 + 遥控器麦克风 → 混合输出。

为什么需要这个模块
------------------
之前只把遥控器那一路播给 `CABLE Input`，结果就是微信/豆包**完全听不到房间里的
声音**：想一边用电脑麦克风说话、一边拿遥控器语音输入，做不到。

vRemoter 在 macOS 上用一个自研 2ch 虚拟声卡解决这件事。Windows 上不需要额外驱动 ——
直接在软件层把两路 PCM 相加再写进 `CABLE Input` 即可，而且能做得更多：
每路独立增益、静音、独奏、参与混音开关，互不影响。

数据流
------
    电脑麦克风（任意采样率）──重采样──┐
                                      ├──相加（各自增益/静音/独奏）──▶ CABLE Input
    遥控器麦克风（ATVV 16kHz）────────┘

线程模型
--------
电脑麦克风的采集回调跑在 PortAudio 自己的线程里，遥控器的解码在 asyncio 线程里，
混合发生在输出流的回调线程里 —— 三者通过 `queue.Queue` 交接，不共享可变状态。
"""

from __future__ import annotations

import logging
import queue

logger = logging.getLogger("rvb.mixer")

# 队列上限：约 2 秒的音频。超过说明采集比播放快（时钟漂移），
# 直接丢老数据保持"实时"，否则延迟会越积越大，最后听起来像回声。
_QUEUE_HEADROOM_SEC = 2.0


def resample_linear(x, src_rate: int, dst_rate: int):
    """线性插值重采样。

    语音场景够用：8k↔16k↔48k 之间插值，对 ASR 的准确率没有可感知影响，
    而它没有依赖、没有状态、不会在长会话里累积漂移（每块独立换算）。

    参数 x 是 numpy 一维数组。
    """
    if src_rate == dst_rate or len(x) == 0:
        return x
    import numpy as np

    n_out = int(len(x) * dst_rate / src_rate)
    if n_out <= 0:
        return x[:0]
    idx = np.arange(n_out, dtype=np.float64) * (src_rate / dst_rate)
    i0 = np.floor(idx).astype(np.int64)
    np.clip(i0, 0, len(x) - 1, out=i0)
    i1 = np.minimum(i0 + 1, len(x) - 1)
    frac = (idx - i0).astype(np.float32)
    return (x[i0] * (1.0 - frac) + x[i1] * frac).astype(np.float32)


def _downsample_points(mono, n: int = 4) -> list[int]:
    """把一块麦克风数据压成 n 个代表点（取绝对值最大者，保住轮廓）。"""
    if len(mono) == 0:
        return [0] * n
    step = max(1, len(mono) // n)
    out: list[int] = []
    for i in range(0, len(mono), step):
        seg = mono[i:i + step]
        if len(seg):
            out.append(int(max(seg, key=abs)))
    return (out + [0] * n)[:n]


# ── 设备枚举 ──────────────────────────────────────────────────────────────────
def list_input_devices() -> list[str]:
    """所有带输入通道的设备名（去重，剔除纯输出的虚拟声卡）。

    剔除 `CABLE Input` 是必须的：它是本程序的**输出**目标，如果被选成"电脑麦克风"
    就会形成一个闭环 —— 把混合结果又采回来再混进去，直接啸叫。
    """
    names: list[str] = []
    try:
        import sounddevice as sd
        raw = []
        for d in sd.query_devices():
            if d.get("max_input_channels", 0) <= 0:
                continue
            n = (d.get("name") or "").strip()
            if not n:
                continue
            low = n.lower()
            if "cable input" in low or "vb-audio" in low or "voicemeeter" in low:
                continue
            raw.append(n)
        names = _dedupe_names(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"枚举输入设备失败：{e}")
        return []
    names.sort(key=lambda n: (0 if any(k in n.lower() for k in ("麦克风", "microphone", "mic")) else 1,
                              n.lower()))
    return names


# Windows 上的设备名去重 ──────────────────────────────────────────────────────
# 同一只设备会在 MME / DirectSound / WASAPI / WDM-KS 四套驱动下各出现一次，
# 而 MME 还会把名字**截断到 31 个字符**。不去重的话，设置页的下拉框里会并排放着
# 4 个长得几乎一样的 `CABLE Input`，用户根本不知道该选哪个 —— 实际它们是同一只声卡。
_MIN_PREFIX_MERGE = 12


def _dedupe_names(raw: list[str]) -> list[str]:
    """去掉同名 / 截断重复的设备名，保留最完整的那个（原始大小写）。"""
    out: list[str] = []
    lowered: list[str] = []
    # 长的先来，这样被截断的短名会被完整名吸收掉
    for n in sorted(set(raw), key=lambda s: (-len(s), s.lower())):
        low = n.lower()
        dup = False
        for k in lowered:
            if low == k:
                dup = True
                break
            # 前缀合并只在名字够长时生效，避免"麦克风"这种短名把无关设备全吞掉
            if len(low) >= _MIN_PREFIX_MERGE and (k.startswith(low) or low.startswith(k)):
                dup = True
                break
        if not dup:
            out.append(n)
            lowered.append(low)
    return out


def list_output_devices() -> list[str]:
    """所有带输出通道的设备名（去重，CABLE 排最前）。"""
    try:
        import sounddevice as sd
        raw = []
        for d in sd.query_devices():
            if d.get("max_output_channels", 0) <= 0:
                continue
            n = (d.get("name") or "").strip()
            if n:
                raw.append(n)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"枚举输出设备失败：{e}")
        return []

    names = _dedupe_names(raw)
    names.sort(key=lambda n: (0 if "cable" in n.lower() else 1, n.lower()))
    return names


def find_input_device(name: str) -> int | None:
    """按名字找输入设备索引；name 为空则返回系统默认输入设备。"""
    import sounddevice as sd
    if not name:
        try:
            dev = sd.default.device
            idx = dev[0] if isinstance(dev, (list, tuple)) else dev
            if idx is None or idx < 0:
                idx = None
            d = sd.query_devices(idx) if idx is not None else sd.query_devices(kind="input")
            return int(d["index"]) if "index" in d else idx
        except Exception:  # noqa: BLE001
            try:
                d = sd.query_devices(kind="input")
                return int(d["index"])
            except Exception:  # noqa: BLE001
                return None
    target = name.strip().lower()
    best = None
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) <= 0:
            continue
        dn = (d.get("name") or "").lower()
        if dn == target:
            return i
        if target in dn and best is None:
            best = i
    return best


# ── 电脑麦克风采集 ────────────────────────────────────────────────────────────
class SystemMic:
    """持续采集电脑麦克风，重采样到输出采样率，供混音回调取用。

    采集与播放是两个独立的时钟域，所以中间必须有一个队列做缓冲：
    播放回调要多少就给多少，采到的多余部分留到下一块，缺了就补静音。
    """

    def __init__(self, device_name: str, out_rate: int):
        self.device_name = device_name or ""
        self.out_rate = int(out_rate)
        self.queue: "queue.Queue" = queue.Queue(
            maxsize=int(out_rate * _QUEUE_HEADROOM_SEC))
        self._stream = None
        self.in_rate = self.out_rate
        self.device_label = ""
        self._peak_hold = 0.0

    # ── 生命周期 ──
    def start(self) -> bool:
        import sounddevice as sd

        dev = find_input_device(self.device_name)
        if dev is None:
            logger.warning("⚠ 找不到可用的电脑麦克风，混音只保留遥控器一路")
            return False
        try:
            info = sd.query_devices(dev)
            # 尽量用设备自己的默认采样率：硬开 16k 在不少 Windows 驱动上是失败的
            self.in_rate = int(info.get("default_samplerate") or 48000)
            self.device_label = info.get("name", str(dev))
            self._stream = sd.InputStream(
                device=dev, channels=1, dtype="float32",
                samplerate=self.in_rate, blocksize=1024,
                callback=self._cb,
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠ 电脑麦克风打开失败（{e}），混音只保留遥控器一路")
            self._stream = None
            return False

        logger.info(f"✅ 电脑麦克风：{self.device_label}  {self.in_rate}Hz → {self.out_rate}Hz")
        return True

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

    @property
    def ok(self) -> bool:
        return self._stream is not None

    # ── 采集回调（PortAudio 线程）──
    def _cb(self, indata, frames, time_info, status):
        import state
        from state import db_from_peak
        if status:
            logger.debug(f"mic status: {status}")
        try:
            mono = indata[:, 0]
            # float32(-1..1) → int16 量纲，和遥控器那一路对齐，相加才有意义
            scaled = mono * 32768.0
            # 电平用**峰值保持**：说话时电平条一闪一闪看着很累，
            # 这里让峰值缓慢衰减，读数稳定得多。
            peak = float(max(abs(scaled.min()), abs(scaled.max()))) if len(scaled) else 0.0
            self._peak_hold = max(peak, self._peak_hold * 0.75)
            state.push_levels(sys_db=db_from_peak(self._peak_hold))
            state.push_sys_audio(_downsample_points(scaled))

            out = resample_linear(scaled, self.in_rate, self.out_rate)
            try:
                self.queue.put_nowait(out)
            except queue.Full:
                # 采集比播放快 → 丢掉最老的一块，保持实时
                try:
                    self.queue.get_nowait()
                    self.queue.put_nowait(out)
                except queue.Empty:
                    pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"麦克风采集回调异常：{e}")

    # ── 播放侧取数 ──
    def read(self, n: int):
        """取 n 个采样；不足则返回现有的（调用方自行补零）。"""
        import numpy as np
        chunks = []
        got = 0
        while got < n:
            try:
                blk = self.queue.get_nowait()
            except queue.Empty:
                break
            chunks.append(blk)
            got += len(blk)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
