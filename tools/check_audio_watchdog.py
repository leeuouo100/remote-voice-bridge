"""闸：音频输出流停摆必须能被发现并自愈（v1.0.12）。

## 为什么要有这道闸

2026-09-17 武哥报：「有几分钟整个语音输入卡死 —— 能看到波形（语音输入状态开着），
但收录不到声音，重启软件才恢复。」

这个症状之所以难查，是因为**两个"看起来正常"的东西把它盖住了**：

  · 波形在动 —— 那走的是 on_audio → state.push_audio，和输出流完全无关；
  · 日志一片干净 —— 丢帧那句原来是 logger.debug，正式版级别是 INFO。

而真正的根因有两个，都属于"没人管"：

  ① 输出流是**建一次、start 一次**的，PortAudio/WASAPI 那一路死后不会自己回来，
     也没有任何监护在看着它；
  ② 混音回调只在**这一路发声时**才去取队列 —— 静音期间队列一点都不消费，
     涨到 maxsize=65536 后每一帧 put_nowait 都抛 queue.Full 被丢掉，
     重新出声时播的还是几分钟前的旧音频。

这道闸把三件事钉住：
  1. 回调用 `_cb_last_at` 报心跳；
  2. 主循环每轮调 `_supervise_audio`，且超时会走 `_rebuild_audio`；
  3. 混音回调**无论增益是否为 0** 都按一比一消费队列
     （否则 queue.Full 会静默丢帧，而且丢的时候日志里没证据）。
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "main.py")


def main() -> int:
    src = open(SRC, encoding="utf-8").read()
    checks: list[tuple[bool, str]] = []

    # 1) 回调必须报心跳，且要在最前面（否则混合逻辑一抛异常就被误判成"流死了"）
    cb = re.search(r"def cb\(outdata, frames, timeinfo, status\):(.*?)\n    return sd\.OutputStream",
                   src, re.S)
    checks.append((bool(cb), "找得到输出流回调 cb()"))
    if cb:
        body = cb.group(1)
        checks.append(("_cb_last_at = time.time()" in body,
                       "回调里写了 _cb_last_at 心跳"))
        i_hb = body.find("_cb_last_at = time.time()")
        i_mix = body.find("for i in range(frames):")
        checks.append((0 <= i_hb < i_mix if i_mix >= 0 else False,
                       "心跳在混合逻辑**之前**（异常时不会误判）"))

        # 2) 无论增益是否为 0，都要按一比一消费队列
        checks.append(("if not _pending_samples:" in body,
                       "取队列这段**不在 r_gain > 0 里**（静音时也消费）"))
        # 老写法： r_gain > 0 分支里再判断队列
        legacy = re.search(r"if r_gain > 0\.0:\s*\n\s*if _pending_samples:", body)
        checks.append((legacy is None,
                       "没有退回老写法（r_gain>0 才取队列 → 队列涨满静默丢帧）"))

    # 3) 监护函数 + 主循环调用
    checks.append(("async def _supervise_audio()" in src, "定义了 _supervise_audio()"))
    checks.append(("await _supervise_audio()" in src, "主循环里调用了 _supervise_audio()"))
    checks.append(("async def _rebuild_audio(" in src, "定义了 _rebuild_audio()"))
    checks.append(("_AUDIO_SILENT_LIMIT" in src, "有静默上限常量"))
    checks.append(("_AUDIO_REBUILD_GAP" in src, "有重建间隔下限（防死循环重建）"))
    checks.append(("_drain_audio_queues" in src, "重建前会清积压（不播旧声音）"))

    # 4) 丢帧不许再静默
    checks.append(("queue.Full" in src and "logger.warning" in src, "队列满时走 warning"))
    m = re.search(r"except queue\.Full:(.*?)(?=\n\n)", src, re.S)
    if m:
        seg = m.group(1)
        # ⚠ 必须剥掉注释再判断：老写法的原句被**原样写进了注释**（作为"以前是这样"的
        #   说明），直接 in 匹配会把自己绊倒 —— 这道闸第一版就是这么误报的。
        code = "\n".join(ln for ln in seg.splitlines()
                         if not ln.strip().startswith("#"))
        checks.append(("_drop_frames += 1" in code, "队列满时累加 _drop_frames 计数"))
        has_debug = any(ln.strip().startswith("logger.debug(")
                        for ln in code.splitlines())
        checks.append((not has_debug,
                       "队列满不再只打 debug（等于静默丢弃）"))
    else:
        checks.append((False, "找得到 except queue.Full 分支"))

    ok = True
    print("=" * 66)
    print(" 闸：音频输出流停摆可见 + 可自愈")
    print("=" * 66)
    for good, name in checks:
        print(f"  {'✅' if good else '❌'} {name}")
        if not good:
            ok = False
    print()
    print("PASS" if ok else "FAIL —— 上面打 ❌ 的那几条会让『波形在动、却没声音』复发")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
