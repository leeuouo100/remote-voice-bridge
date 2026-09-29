"""闸：采样率必须**先协商、后建流**，迟到也要能重建 —— 2026-09-29 审查报告 P2-1。

## 问题长什么样

采样率是**遥控器**在 CAPS 响应里给我们的（ADPCM 8k / 16k），而 `ATVVState`
的默认值是 `sample_rate = 16000`。老代码的顺序是：

    GET_CAPS 发出去 → （不等）→ 找 CABLE → 建 SystemMic(16000) → 建 OutputStream(16000)

协商出来是 8k 的遥控器就被**按 16k 建流**。后果很阴：**不报错、不崩、不刷日志**，
只是声音变调变速 —— 用户只会说"声音怪怪的"，排查时日志里一个字都没有。

而且 `SystemMic` 是按 `out_rate` 做重采样的（`mixer.resample_linear(in → out)`），
所以"只重建输出流"也是错的：电脑麦克风那一路还按旧速率出样本。

## 这道闸钉什么

  A. 能力响应信号用 `threading.Event`（`on_control` 跑在 BLE 回调线程上，
     `asyncio.Event.set()` 不是线程安全的）
  B. 等的位置：CAPS 请求**之后**、建音频链（找 CABLE / 建 SystemMic / 建流）**之前**
  C. `on_control` 的 capabilities 分支里真的 `set()` 了，而且**在 `if caps:` 里面**
     （解析失败时不能放行 —— 否则拿着默认 16000 建流，跟"没等"一个效果）
  D. `_start_stream()` 读的是 `_audio_rate["sr"]`，**不是** `atvv.state.sample_rate`
     （两处各读一次的话，"什么时候用哪个速率"就成了没人说得清的事）
  E. 电脑麦克风那一路也按同一个速率建（`_make_sysmic(sr)`）
  F. 主循环里有"协商速率 ≠ 建流速率"的比较，并调 `_rebuild_for_rate`
  G. `_rebuild_for_rate` 把**旧的两条**都停掉、清积压、再按新速率重建

每条都配反例（改坏后必须报红），见文件末尾。

用法： python tools/check_audio_rate.py
输出： AUDIO RATE OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "main.py"

FAILS: list[str] = []
PASSES = 0


def check(cond, msg) -> bool:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
    return bool(cond)


# ── 可被反例复用的判据（纯函数，只看源码字符串）──────────────────────────────

def has_caps_event(src: str) -> bool:
    """能力响应信号必须是 threading.Event（跨线程置位）。"""
    return bool(re.search(r"^    caps_ready = threading\.Event\(\)", src, re.M))


def caps_set_inside_caps_branch(src: str) -> bool:
    """`caps_ready.set()` 必须在 capabilities 分支的 `if caps:` 里。"""
    m = re.search(r'if event\["type"\] == "capabilities":(.*?)'
                  r'(?=elif event\["type"\] == "audio_start":)', src, re.S)
    if not m:
        return False
    body = m.group(1)
    i_if = body.find("if caps:")
    i_set = body.find("caps_ready.set()")
    if i_if < 0 or i_set < 0:
        return False
    return i_set > i_if


def _strip_comments(text: str) -> str:
    """剥掉整行注释。

    ⚠ 必须剥：本项目的注释**逐字引用了老写法**（"读 `_audio_rate` 而不是
      `atvv.state.sample_rate`"），`in` 匹配会把说明当成真的代码 ——
      于是改坏了也照绿（`check_voice_session.py` 的 `_strip_docstrings`
      是同一件事，那边剥的是三引号）。
    """
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.strip().startswith("#"))


def waits_before_building_chain(src: str) -> bool:
    """等待必须夹在「CAPS 请求」与「建音频链」之间（比位置，不是比存在）。

    ⚠ 找的是**真的调用**（`run_in_executor(None, caps_ready.wait, …)`），
      不是 `caps_ready.wait(` —— 注释里也会出现这个片段，先找到注释就比错了。
    """
    i_send = src.find("CAPS request sent")
    i_wait = src.find("run_in_executor(None, caps_ready.wait")
    i_chain = src.find("_find_cable, cfg.audio_output")
    if min(i_send, i_wait, i_chain) < 0:
        return False
    return i_send < i_wait < i_chain


def stream_uses_audio_rate(src: str) -> bool:
    """`_start_stream` 必须读 `_audio_rate["sr"]`，不许自己再读一次协商值。"""
    m = re.search(r"def _start_stream\(\):(.*?)(?=\n    stream = await)", src, re.S)
    if not m:
        return False
    body = _strip_comments(m.group(1))
    ret = re.search(r"return _create_stream\((.*?)\)", body)
    if not ret:
        return False
    args = ret.group(1)
    return '_audio_rate["sr"]' in args and "sample_rate" not in args


def rate_change_detected(src: str) -> bool:
    """主循环里必须真的比一次、并且真的调了重建。"""
    return (re.search(r"if _want_sr != _audio_rate\[\"sr\"\]:", src) is not None
            and "await _rebuild_for_rate(_want_sr)" in src)


def rebuild_stops_both(src: str) -> bool:
    """重建必须把**输出流和电脑麦克风**都停掉（只换一条 = 两路速率不一致）。"""
    m = re.search(r"async def _rebuild_for_rate\(new_sr: int\) -> None:(.*?)"
                  r"(?=\n    # ── Keyboard hooks)", src, re.S)
    if not m:
        return False
    body = m.group(1)
    return ("old_stream, old_sysmic = stream, sysmic" in body
            and "_drain_audio_queues()" in body
            and "sysmic = await _make_sysmic(new_sr)" in body)


def main() -> int:
    src = MAIN.read_text(encoding="utf-8")

    # ── A. 跨线程信号 ───────────────────────────────────────────────────────
    check(has_caps_event(src),
          "A1 `caps_ready = threading.Event()`（on_control 在 BLE 回调线程上跑，"
          "asyncio.Event.set() 不是线程安全的）")
    check("import threading" in src, "A2 main.py 导入了 threading")

    # ── B. 等的位置 ─────────────────────────────────────────────────────────
    check(waits_before_building_chain(src),
          "B1 等待夹在「CAPS 请求发出」与「找 CABLE / 建音频链」之间"
          "（位置错 = 等于没等：还是按默认 16000 建流）")
    check("_CAPS_WAIT" in src and "caps_ready.wait(" in src,
          "B2 等待带超时（一条慢响应不该把整个桥卡住）")
    check(re.search(r"run_in_executor\(None, caps_ready\.wait", src) is not None,
          "B3 等待跑在 executor 里（别阻塞事件循环）")
    check("⚠ 发出 CAPS 请求后" in src,
          "B4 超时**必须留痕**：这条故障不报错、只听着不对，没日志就查不出来")

    # ── C. 置位点 ───────────────────────────────────────────────────────────
    check(caps_set_inside_caps_branch(src),
          "C1 `caps_ready.set()` 在 capabilities 分支的 `if caps:` 里"
          "（放行得太早 = 解析失败也继续，等于没等）")

    # ── D. 建流读哪个速率 ───────────────────────────────────────────────────
    check(stream_uses_audio_rate(src),
          "D1 `_start_stream()` 读 `_audio_rate[\"sr\"]`，不自己再读一次协商值"
          "（两处各读一次 → 输出流与电脑麦克风可能用不同速率）")

    # ── E. 电脑麦克风也跟着走 ───────────────────────────────────────────────
    check(re.search(r"async def _make_sysmic\(sr: int\)", src) is not None,
          "E1 有 `_make_sysmic(sr)`（SystemMic 按 out_rate 重采样，速率必须跟着变）")
    check("sysmic = await _make_sysmic(_audio_rate[\"sr\"])" in src,
          "E2 建链时电脑麦克风用的是同一个 `_audio_rate[\"sr\"]`")

    # ── F. 迟到要能发现 ─────────────────────────────────────────────────────
    check(rate_change_detected(src),
          "F1 主循环里比较「协商速率 vs 建流速率」并调 `_rebuild_for_rate`"
          "（没有它，迟到的 CAPS 就永远没人管 → 一直是变调的）")

    # ── G. 重建的完整性 ─────────────────────────────────────────────────────
    check(rebuild_stops_both(src),
          "G1 重建时把输出流**和**电脑麦克风都停掉、清积压、按新速率重建"
          "（只换一条 = 两路速率不一致，混出来还是错的）")
    check("res.sysmic = sysmic" in src and "res.stream = new" in src,
          "G2 重建后两个句柄都重新登记（P1-3 的收尾靠它）")

    # ── 反例（改坏后必须报红）──────────────────────────────────────────────
    n_red = 0

    def _expect_red(label: str, bad: str) -> None:
        nonlocal n_red
        if bad == src:
            FAILS.append(f"反例没构造出来（锚点没找到）：{label}")
            return
        red = 0
        if not has_caps_event(bad):
            red += 1
        if not caps_set_inside_caps_branch(bad):
            red += 1
        if not waits_before_building_chain(bad):
            red += 1
        if not stream_uses_audio_rate(bad):
            red += 1
        if not rate_change_detected(bad):
            red += 1
        if not rebuild_stops_both(bad):
            red += 1
        if red:
            n_red += 1
        else:
            FAILS.append(f"反例：{label} 居然全绿 —— 这道闸拦不住它复发")

    # 反例 1：退回"不等，直接建流"（＝老代码的形状）
    _expect_red("删掉等待（发完 CAPS 直接建流）",
                src.replace(
                    "    got_caps = await loop.run_in_executor(None, caps_ready.wait, _CAPS_WAIT)\n",
                    "    got_caps = True\n", 1))

    # 反例 2：信号改用 asyncio.Event（＝跨线程置位不安全）
    _expect_red("把 caps_ready 改成 asyncio.Event",
                src.replace("    caps_ready = threading.Event()",
                            "    caps_ready = asyncio.Event()", 1))

    # 反例 3：set() 挪到 `if caps:` 之外（＝解析失败也放行）
    _expect_red("把 caps_ready.set() 挪出 `if caps:`",
                src.replace(
                    '                    # 唤醒"等能力响应再建音频流"那条路（P2-1）。\n'
                    '                    # 放在 if caps 里面：解析失败（caps=None）时不能放行 ——\n'
                    '                    # 否则会拿着默认 16000 去建流，跟"没等"是一个效果。\n'
                    '                    caps_ready.set()\n', "", 1))

    # 反例 4：建流时又回去读协商值（＝两处各读一次）
    _expect_red("让 _start_stream 回去读 atvv.state.sample_rate",
                src.replace("        return _create_stream(out_dev, _audio_rate[\"sr\"], sysmic)",
                            "        return _create_stream(out_dev, atvv.state.sample_rate or 16000, sysmic)",
                            1))

    # 反例 5：主循环不再比较速率（＝迟到的 CAPS 永远没人管）
    _expect_red("删掉主循环里的速率比较",
                src.replace('            if _want_sr != _audio_rate["sr"]:',
                            "            if False:", 1))

    # 反例 6：重建时只换输出流、不管电脑麦克风
    _expect_red("重建时不动电脑麦克风",
                src.replace("            sysmic = await _make_sysmic(new_sr)\n",
                            "", 1))

    for m in FAILS:
        print(f"  FAIL {m}")
    if FAILS:
        print(f"AUDIO RATE FAILED（{len(FAILS)} 项）")
        print("  提示：采样率是**遥控器**给的（CAPS），默认值只是 16000。")
        print("        发完请求就建流 = 8k 的遥控器被按 16k 播 —— 不报错，只变调。")
        return 1
    print(f"  OK   {PASSES} 项全过（跨线程信号 / 等待位置 / 置位点 / 建流速率 / "
          f"迟到重建 / 重建完整性）")
    print(f"  反例 {n_red} 条全部报红（改坏拦得住）")
    print("AUDIO RATE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
