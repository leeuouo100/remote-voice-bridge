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


def _empty():
    """空的 float32 采样数组（统一的"没有数据"表示）。"""
    import numpy as np
    return np.zeros(0, dtype=np.float32)


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


# ── 回环设备 / 别名设备 ───────────────────────────────────────────────────────
# 本程序把混音结果**写进** `audio_output`（默认 `CABLE Input`），而 VB-CABLE 会把
# `CABLE Input` 上的东西原样送到 `CABLE Output`。如果"电脑麦克风"解析到
# `CABLE Output`，就等于**把自己的输出采回来再混进去** —— 一个闭环。
#
# 🔴 2026-09-29 真机实测（不是推测）：
#     程序实际打开的就是 `CABLE Output (VB-Audio Virtual )`，于是
#         levels.sys = -10.4 dBFS    而    levels.remote = -34.3 dBFS
#     系统麦克风那一路比遥控器**响 24 dB**，送到输入法的信号里绝大部分是
#     延迟的自听自。用户报的正是「语音输入时好时坏、识别不出字」。
#
# 为什么原来没挡住：`list_input_devices()` 里**有**这个过滤，但它只喂给设置页的
# 下拉框；真正决定"开哪只设备"的 `find_input_device("")` 走的是
# `sd.query_devices(kind="input")` 这条**默认设备**分支，完全没经过过滤。
# 守卫写在了"选择器"上而不是"解析器"上 —— 本项目反复踩的"代码在、跑不到"。
_LOOPBACK_MARKERS = (
    "cable input", "cable output", "cable 2", "vb-audio", "vb-cable",
    "virtual cable", "voicemeeter",
)

# Windows 的"别名设备"：它们不指向某一只具体的麦克风，而是指向**当前系统默认**，
# 而系统默认完全可能就是 `CABLE Output`（用户按本程序文档给输入法设的正是它）。
# 留在下拉框里等于给用户第二个踩同一个坑的入口，所以一并剔除。
_ALIAS_MARKERS = ("sound mapper", "主声音捕获驱动程序", "主声音驱动程序")


def is_loopback_name(name: str) -> bool:
    """这只设备会不会把本程序自己的输出喂回来（＝闭环）。"""
    low = (name or "").strip().lower()
    return bool(low) and any(m in low for m in _LOOPBACK_MARKERS)


def is_alias_name(name: str) -> bool:
    """这只设备是不是"指向当前默认"的 Windows 别名（而不是某只真实麦克风）。"""
    low = (name or "").strip().lower()
    return bool(low) and any(m in low for m in _ALIAS_MARKERS)


def _device_name(idx) -> str:
    """索引 → 设备名；查不到返回空串（绝不抛）。"""
    try:
        import sounddevice as sd
        return (sd.query_devices(idx).get("name") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def default_input_index():
    """系统默认输入设备的索引；拿不到返回 None。

    ⚠⚠ 不要用 `sd.default.device[0]` 去取。

      `sd.default.device` 的类型是 `_InputOutputPair`，**不是 list/tuple**，
      `isinstance(dev, (list, tuple))` 判出来是 **False**。本项目历史上两处都这么写，
      于是各自以不同方式静默跑偏：
        · `find_input_device` 把整个对象拿去和 0 比大小 → `TypeError` → 被
          `except Exception` 吞掉 → 退回 `query_devices(kind="input")`，
          **恰好绕过了回环过滤**，把 `CABLE Output` 当成了电脑麦克风；
        · `console_server._resolve_in_device` 静默掉到 `in_list[0]`，报了一只
          **根本不是实际设备的**名字，还打了绿勾（"检查项全绿但识别不出字"）。

      `sd.query_devices(kind="input")` 是唯一稳的写法：它返回的 dict 带 "index"。
    """
    try:
        import sounddevice as sd
        d = sd.query_devices(kind="input")
        i = d.get("index")
        return int(i) if i is not None else None
    except Exception:  # noqa: BLE001
        return None


def _is_usable_mic(idx) -> bool:
    """这个索引能不能当"电脑麦克风"用（既非回环、也非别名）。"""
    n = _device_name(idx)
    return bool(n) and not is_loopback_name(n) and not is_alias_name(n)


def _first_real_input():
    """第一只**真实**麦克风的索引（跳过回环与别名设备）。没有则 None。"""
    try:
        import sounddevice as sd
        cands = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0:
                continue
            n = (d.get("name") or "").strip()
            if not n or is_loopback_name(n) or is_alias_name(n):
                continue
            # 名字里带"麦克风 / mic"的排前面（本机就是 Realtek 麦克风阵列）
            low = n.lower()
            cands.append((0 if ("麦克风" in n or "microphone" in low) else 1, len(n), i))
        if not cands:
            return None
        return min(cands)[2]
    except Exception:  # noqa: BLE001
        return None


# 解析结果的原因码
INPUT_OK = "ok"              # 正常
INPUT_LOOPBACK = "loopback"  # 会把本程序自己的输出喂回来 → 必须拒绝
INPUT_ALIAS = "alias"        # 指向"当前默认"的别名设备 → 拒绝
INPUT_MISSING = "missing"    # 配置里写死的设备在系统里找不到
INPUT_NONE = "none"          # 系统里没有任何可用的真实麦克风


def resolve_input_device(name: str):
    """**唯一**的"电脑麦克风用哪只设备"解析入口。返回 `(索引|None, 设备名, 原因码)`。

    为什么要做成唯一入口：这段逻辑以前在 `find_input_device`（运行时真正开设备）
    和 `console_server._resolve_in_device`（控制台检查项）**各写了一遍**，两边结论
    不一致 —— 一个说 `CABLE Output`、另一个说 `Microsoft Sound Mapper` 并打绿勾，
    用户看到的是"检查项全绿、却一个字都识别不出来"。同一份判断只能有一个出处。

    `索引` 为 None 时调用方必须**不要**退回系统默认 —— 那正是要躲的闭环；
    应当明确禁用"电脑麦克风"这一路并告警。
    """
    want = (name or "").strip()

    if want:
        idx = find_input_device_by_name(want)
        if idx is None:
            return None, want, INPUT_MISSING
        nm = _device_name(idx) or want
        if is_loopback_name(nm):
            return None, nm, INPUT_LOOPBACK
        if is_alias_name(nm):
            return None, nm, INPUT_ALIAS
        return idx, nm, INPUT_OK

    # 配置为空 = 用系统默认。默认设备**必须**过一遍过滤：
    # 本机的系统默认正是 `CABLE Output`，直接用就是闭环。
    idx = default_input_index()
    bad, reason = "", INPUT_NONE
    if idx is not None:
        nm = _device_name(idx)
        if is_loopback_name(nm):
            bad, reason = nm, INPUT_LOOPBACK
        elif is_alias_name(nm):
            bad, reason = nm, INPUT_ALIAS
        else:
            return idx, nm, INPUT_OK

    # 默认设备不可用 → 退到第一只真实麦克风；一只都没有就明确返回 None。
    alt = _first_real_input()
    if alt is not None:
        return alt, _device_name(alt), INPUT_OK
    return None, bad, reason


def find_input_device_by_name(name: str):
    """按名字找输入设备索引（三级匹配：全名相等 → 前缀 → 子串）。找不到返回 None。"""
    target = (name or "").strip().lower()
    if not target:
        return None
    try:
        import sounddevice as sd
    except Exception:  # noqa: BLE001
        return None
    # 同一级里取**最短名**。
    #
    # ⚠ 别改成"谁先枚举到就用谁"：同一只声卡会在 MME / DirectSound / WASAPI /
    #   WDM-KS 四套驱动下各出现一次（MME 还会把名字截断到 31 字符），
    #   枚举顺序由驱动决定、不保证稳定。取最短名天然偏向没被截断、
    #   后缀修饰最少的那一条，也就是用户在下拉框里看到的那个名字。
    exact, prefix, contains = [], [], []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0:
                continue
            dn = (d.get("name") or "").lower()
            if dn == target:
                exact.append((len(dn), i))
            elif dn.startswith(target):
                prefix.append((len(dn), i))
            elif target in dn:
                contains.append((len(dn), i))
    except Exception:  # noqa: BLE001
        return None
    for bucket in (exact, prefix, contains):
        if bucket:
            return min(bucket)[1]
    return None


# ── 设备枚举 ──────────────────────────────────────────────────────────────────
def list_input_devices() -> list[str]:
    """所有带输入通道的设备名（去重，剔除回环设备与 Windows 别名设备）。

    剔除的理由见上面 `_LOOPBACK_MARKERS` / `_ALIAS_MARKERS` 那段注释：
    这两类设备被选成"电脑麦克风"都会形成闭环或等价于闭环。
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
            if is_loopback_name(n) or is_alias_name(n):
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
    """按名字/默认找输入设备索引；**保证不是回环设备**。找不到返回 None。

    保留这个名字只是为了不破坏既有调用点；真正的判断全在 `resolve_input_device`，
    这里只是取它的第一项。需要知道"为什么没有"（回环？别名？找不到？）时，
    请直接调 `resolve_input_device`。
    """
    return resolve_input_device(name)[0]


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
        # 上一块取不完的**尾部余量**。
        # ⚠ 没有它就会丢样本，见 read() 的注释 —— 这是 2026-09-29 审查报告 P0-2。
        self._remainder = _empty()

    # ── 生命周期 ──
    def start(self) -> bool:
        import sounddevice as sd

        dev, label, reason = resolve_input_device(self.device_name)
        if dev is None:
            # ⚠⚠ 这里**绝不能**退回"系统默认设备" —— 系统默认可能正是本程序自己的
            #     输出（`CABLE Output`），退回去就是闭环。宁可不混音。
            #
            #     这几种"没有可用麦克风"必须**分别说清**：以前统一报
            #     「找不到可用的电脑麦克风」，而真机上的情况其实是"找到了、
            #     但找到的是自己的输出"，日志一个字没提，只能靠猜。
            if reason == INPUT_LOOPBACK:
                logger.warning(
                    "⚠ 电脑麦克风解析到「%s」—— 那是本程序**自己的输出**，"
                    "采回来会形成自听自的闭环（真机实测：这一路比遥控器响 24 dB，"
                    "送到输入法的信号里绝大部分是延迟回声，语音识别时好时坏）。\n"
                    "   ⇒ 已**拒绝打开**，混音只保留遥控器一路。\n"
                    "   处置：控制台 → 音频 → 把「电脑麦克风」显式选成**真实的**麦克风"
                    "（别选 CABLE Output / Sound Mapper 这类虚拟或别名设备）。", label)
            elif reason == INPUT_ALIAS:
                logger.warning(
                    "⚠ 电脑麦克风解析到「%s」—— 它是 Windows 的**别名设备**，"
                    "指向「当前系统默认」，而默认可能就是 CABLE Output"
                    "（＝本程序自己的输出）。已拒绝打开，混音只保留遥控器一路。\n"
                    "   处置：控制台 → 音频 → 把「电脑麦克风」显式选成真实麦克风。", label)
            elif reason == INPUT_MISSING:
                logger.warning(
                    "⚠ 配置里指定的电脑麦克风「%s」在系统里找不到，混音只保留遥控器一路。"
                    "（控制台 → 音频 → 重新选一只）", label)
            else:
                logger.warning("⚠ 系统里没有可用的真实麦克风，混音只保留遥控器一路")
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
        """取**恰好** n 个采样（不够就少给，由调用方补零）；多出来的尾部留到下次。

        ⚠⚠ 必须留余量，不能整块返回 —— 这是 2026-09-29 审查报告 P0-2 的核心。

        老写法是"攒够 n 就 `np.concatenate(chunks)` 整块返回"，而两个时钟域
        的块大小差着一个数量级：
            采集块（PortAudio 回调）  = 1024 个采样
            播放块（CABLE 输出回调）  =  240 个采样
        `while got < n` 第一次 `get_nowait()` 就拿到 1024 ≥ 240 → 立刻返回整块，
        调用方 `for i in range(frames)` 只读前 240 个 ⇒ **每块静默丢掉 784 个
        （76%）**。房间里那一路于是变成「5ms 有声 / 16ms 空白」的切片，
        听感是约 67Hz 的嗡嗡声 —— 语音识别直接崩，用户看到的就是
        「输入法面板弹了、也在收音，但一个字都识别不出来」。
        而且丢样本这件事**日志里一个字都没有**，属于本项目最怕的"静默失效"。

        同时这也修掉了"采集比播放快"的错觉：老写法每块都丢 76%，
        队列根本涨不起来，看起来"很实时"，其实是在丢数据。
        """
        import numpy as np
        if n <= 0:
            return _empty()

        chunks = []
        got = 0
        # 先把上次的余量接上 —— 顺序不能反：余量是最老的音频
        if len(self._remainder):
            chunks.append(self._remainder)
            got += len(self._remainder)
            self._remainder = _empty()

        while got < n:
            try:
                blk = self.queue.get_nowait()
            except queue.Empty:
                break
            chunks.append(blk)
            got += len(blk)

        if not chunks:
            return _empty()
        out = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
        if len(out) > n:
            self._remainder = out[n:].copy()   # 存起来给下一次，绝不丢
            out = out[:n]
        return out

    def skip(self, n: int) -> None:
        """按 1:1 的比例**丢掉** n 个采样（不返回、不混音）。

        为什么需要它：这一路被静音/被独奏压掉时，输出回调照样每块消费 240 个
        采样，而老写法是"增益为 0 就**不取数**" —— 队列于是原地积压，
        一路涨到 maxsize（2 秒）为止，`put_nowait` 开始抛 `queue.Full`；
        解除静音的那一刻，先播出来的是**2 秒前的旧音频**（听感是"回声/串音"）。

        现在改成"无论发不发声都按一比一消费"：不发声时用本方法把数据丢掉，
        队列永远不积压，重新出声时听到的就是当下的声音。
        与 `read()` 共用同一套取数逻辑，避免两处各写一遍、日后改一处漏一处。
        """
        self.read(n)
