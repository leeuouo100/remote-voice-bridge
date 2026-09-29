"""混音取数不许丢样本 —— 2026-09-29 审查报告 P0-2。

为什么单独一条闸
----------------
`mixer.SystemMic.read(n)` 原先的写法是"攒够 n 就 `np.concatenate(chunks)`
整块返回"，而两个时钟域的块大小差着一个数量级：

    采集块（PortAudio 回调，blocksize=1024） = 1024 个采样
    播放块（CABLE 输出回调，blocksize=240）  =  240 个采样

`while got < n` 第一次 `get_nowait()` 就拿到 1024 ≥ 240 → 立刻整块返回，
调用方 `for i in range(frames)` 只读前 240 个 ⇒ **每块静默丢掉 784 个（76%）**。

后果：房间里那一路（电脑麦克风）变成「5ms 有声 / 16ms 空白」的切片，
听感是约 67Hz 的嗡嗡声。语音识别直接崩 —— 用户看到的现象正是
「输入法面板弹了、也在收音，但一个字都识别不出来」。
而"丢样本"这件事**日志里一个字都没有**，属于本项目最怕的静默失效。

这条闸只认行为，不看代码长什么样：**喂进去多少采样，反复 read 就该一个不少
地吐出来**。带反例自证（把 read 换回旧写法，检查必须变红）。

用法
----
    python tools/check_mixer_read.py

退出码 0 = 通过；1 = 失败。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BLOCK = 1024      # 采集块（PortAudio 回调）
PLAY = 240        # 播放块（输出回调）


def _ramp(n: int, start: int):
    """一段可辨认的斜坡：丢样本 / 串位 / 重放一眼就能看出来。"""
    import numpy as np
    return np.arange(start, start + n, dtype=np.float32)


def _feed(mic, blocks: int) -> "object":
    """往采集队列里塞 blocks 块，返回塞进去的全部采样（作为标准答案）。"""
    import numpy as np
    allin = []
    for i in range(blocks):
        blk = _ramp(BLOCK, i * BLOCK)
        allin.append(blk)
        mic.queue.put_nowait(blk)
    return np.concatenate(allin)


def check_no_sample_loss(verbose: bool = True) -> bool:
    """反复 read(PLAY)，直到队列取空 —— 吐出来的必须与喂进去的**逐点相同**。"""
    import numpy as np
    from mixer import SystemMic

    mic = SystemMic("", 16000)
    want = _feed(mic, 4)
    got = []
    over = 0
    for _ in range(200):                      # 上限远大于 4096/240，防死循环
        blk = mic.read(PLAY)
        if len(blk) > PLAY:
            over += 1
        if len(blk) == 0:
            break
        got.append(blk)
    got = np.concatenate(got) if got else np.zeros(0, dtype=np.float32)

    if verbose:
        print(f"   喂进去 {len(want)} 个采样，读出来 {len(got)} 个")
    if over:
        print(f"   ❌ 有 {over} 次 read() 返回了**多于 {PLAY}** 个采样"
              f"（调用方只读前 {PLAY} 个，多出来的必丢）")
        return False
    if len(got) != len(want):
        print(f"   ❌ 丢样本：期望 {len(want)} 个，实际 {len(got)} 个"
              f"（丢了 {len(want) - len(got)} 个，{100 * (1 - len(got) / len(want)):.0f}%）")
        return False
    if not np.array_equal(got, want):
        bad = int(np.argmax(got != want))
        print(f"   ❌ 样本串位：第 {bad} 个起不一致"
              f"（期望 {want[bad]}，实际 {got[bad]}）—— 说明有重放/错序")
        return False
    print(f"   OK 一个不少、一个不错序（{len(got)} 个采样逐点相同）")
    return True


def check_skip_drains(verbose: bool = True) -> bool:
    """不发声时也要按一比一消费 —— 否则队列积压，解除静音先播 2 秒前的旧音频。"""
    import numpy as np
    from mixer import SystemMic

    mic = SystemMic("", 16000)
    _feed(mic, 4)                             # 4096 个采样堆在队列里
    if not hasattr(mic, "skip"):
        print("   ❌ SystemMic 没有 skip()：静音期间没有「消费但不混音」的入口，"
              "队列会一直积压到 maxsize，解除静音时先吐旧音频")
        return False
    mic.skip(PLAY)
    rest = mic.read(4096)
    if len(rest) != 4096 - PLAY:
        print(f"   ❌ skip({PLAY}) 没有真的丢掉 {PLAY} 个："
              f"剩 {len(rest)} 个，期望 {4096 - PLAY} 个")
        return False
    print(f"   OK skip({PLAY}) 精确消费，队列不积压（剩 {len(rest)} 个）")
    return True


def check_caller_drains(verbose: bool = True) -> bool:
    """静态：输出回调在"这一路不发声"时**也要**消费。

    光有 `skip()` 不够 —— 调用方得真的调它。老写法是把增益判断写进 if：
        if sysmic is not None and s_gain > 0.0:
            sys_blk = sysmic.read(frames)
    增益为 0 时**一次都不取数** ⇒ 队列积压 ⇒ 解除静音先播旧音频。
    """
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    ok = True

    if "sysmic.skip(" not in src:
        print("   ❌ main.py 的输出回调里没有 sysmic.skip() —— "
              "不发声的那一路不会被消费，队列会积压")
        ok = False
    else:
        print("   OK 输出回调在不发声时用 sysmic.skip() 消费")

    if re.search(r"if\s+sysmic\s+is\s+not\s+None\s+and\s+s_gain\s*>\s*0\.0\s*:", src):
        print("   ❌ 又出现了 `if sysmic is not None and s_gain > 0.0:` 这个老形态 —— "
              "增益为 0 就完全不取数 = 静音期间积压")
        ok = False
    else:
        print("   OK 增益判断没有把「取数」一起关掉（取数与混音已解耦）")
    return ok


def run_counter_examples(verbose: bool = True) -> bool:
    """反例自证：把 read 换回旧实现，上面的"不丢样本"必须变红。

    为什么必须有这一层：一条永远绿的断言等于没有断言。老实现就是
    "整块返回"，而它在"不丢样本"这条检查下必须失败 —— 否则说明检查本身
    根本没测到那个行为。
    """
    import numpy as np
    import mixer
    from mixer import SystemMic

    print("\n── 反例自证 ──")
    orig = SystemMic.read

    def _old_read(self, n: int):
        """旧写法：攒够 n 就整块返回（多出来的尾部直接丢）。"""
        chunks = []
        got = 0
        while got < n:
            try:
                blk = self.queue.get_nowait()
            except Exception:
                break
            chunks.append(blk)
            got += len(blk)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]

    SystemMic.read = _old_read
    try:
        mic = SystemMic("", 16000)
        _feed(mic, 4)
        got = []
        for _ in range(200):
            blk = mic.read(PLAY)
            if len(blk) == 0:
                break
            got.append(blk[:PLAY])            # 调用方就是这么用的：只读前 n 个
        got = np.concatenate(got) if got else np.zeros(0, dtype=np.float32)
        if len(got) == 4 * BLOCK:
            print("   ❌ [反例] 换回旧写法后仍然「一个不少」→ 说明这条检查无效")
            return False
        print(f"   OK [反例] 换回旧写法 → 4×{BLOCK} 个采样只剩 {len(got)} 个"
              f"（丢 {100 * (1 - len(got) / (4 * BLOCK)):.0f}%）⇒ 检查有效")
    finally:
        SystemMic.read = orig

    # 反例 2：把 skip 拿掉，静音积压那条必须红
    orig_skip = getattr(SystemMic, "skip", None)
    try:
        if orig_skip is not None:
            del SystemMic.skip
        mic = SystemMic("", 16000)
        _feed(mic, 4)
        has = hasattr(mic, "skip")
        if has:
            print("   ❌ [反例] 删掉 skip() 之后 hasattr 仍然为真 → 检查无效")
            return False
        print("   OK [反例] 删掉 skip() → 检查能发现（hasattr 为假）⇒ 检查有效")
    finally:
        if orig_skip is not None:
            SystemMic.skip = orig_skip

    return True


def main() -> int:
    print("=" * 60)
    print("mixer 取数自检 —— 混音那一路不许丢样本（审查报告 P0-2）")
    print("=" * 60)

    try:
        import numpy  # noqa: F401
    except Exception as e:                     # noqa: BLE001
        print(f"⚠ 跳过：装不了 numpy（{e}）")
        return 0

    print("\n── 1. 反复 read(240) 一个样本都不许丢 ──")
    ok = check_no_sample_loss()

    print("\n── 2. 静音期间也要消费（skip）──")
    ok = check_skip_drains() and ok

    print("\n── 3. 调用方在不发声时也消费 ──")
    ok = check_caller_drains() and ok

    ok = run_counter_examples() and ok

    print("\n" + ("✅ 通过" if ok else "❌ 失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
