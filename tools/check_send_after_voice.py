"""闸：语音说完之后必须能自己发出去（v1.0.13）。

## 为什么要有这道闸

2026-09-17 武哥的原话：

  「我说完话了，按完语音结束，就可以退出，然后按回车就把这个消息发送给你。
    但是你现在没有把这个功能开发出来，等我在这里跟你说完语音、按完语音结束，
    我还要去电脑上操作鼠标去点发送。这已经完全没有达到我要求的那种
    voice coding 的感觉了。」

问题不在语音识别 —— 识别一直是好用的。缺的是**闭环的最后一环**：
识别完，人还得伸手够鼠标。这一下就把"对着遥控器干活"打回了"对着电脑干活"。

为什么当时只能靠鼠标：遥控器的按键（确认/方向/返回）走 HID 厂商页，
在 Windows 上至今收不到报告（见 remote_hid.py 文件头）。所以"给某个遥控器键
绑一个回车"这条路走不通。

但**根本不需要那个键**：语音会话的开始/结束本来就是这个程序在记账的
（main.py 的 voice_active 状态机 —— 因为只有程序知道「松手 ≠ 说完」）。
既然"这段说完了"这个时刻我们本来就知道，那就在那一刻替用户按一下发送键。

## 这道闸钉住什么

  1. 配置项存在、默认开、**延迟不能太小**
     —— 太小会在输入法把文字落进输入框**之前**就回车，
        等于把用户刚说的话弄丢一次，比不自动发更糟；
  2. 结束分支登记发送、开始分支取消发送
     —— 少了取消那半条，用户"说完觉得不对、接着说"会把上一条残缺消息发出去；
  3. 零音频帧时不发送
     —— 误触时按回车会把输入框里**原有的内容**发出去（替用户发了条错消息）；
  4. 三个字段进了 `_get_cfg` 的白名单
     —— 不进的话用户改了 config.json 也不生效，而且是**静默**不生效；
  5. main.py 能编译 —— 这条顺带挡住 `global` 在函数里晚于使用而 SyntaxError
     那个坑（第一版就是这么炸的：name '_pending_send_at' is used prior to
     global declaration）。
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _utf8 import setup as _setup_utf8  # noqa: E402
_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")
CFG = os.path.join(ROOT, "config.py")


def run_checks(main_src: str, cfg_src: str) -> list[tuple[bool, str]]:
    """纯函数：给两份源码，返回 (是否通过, 说明) 列表。

    做成纯函数是为了能跑反例（见 main 末尾）—— 这道闸必须自己先证明会红，
    否则"全绿"什么都说明不了。
    """
    checks: list[tuple[bool, str]] = []

    # ── 1. 配置项 ────────────────────────────────────────────────────
    m = re.search(r"send_after_voice:\s*bool\s*=\s*(True|False)", cfg_src)
    checks.append((bool(m) and m.group(1) == "True",
                   "config：send_after_voice 默认开（武哥要的就是「说完即发」）"))

    m = re.search(r"send_after_voice_delay_ms:\s*int\s*=\s*(\d+)", cfg_src)
    delay = int(m.group(1)) if m else -1
    checks.append((delay >= 300,
                   f"config：发送延迟 ≥ 300ms（当前 {delay}ms）"
                   " —— 太小会在文字落进输入框之前就打回车，等于把刚说的话弄丢"))

    m = re.search(r'send_after_voice_key:\s*str\s*=\s*"([^"]+)"', cfg_src)
    checks.append((bool(m) and m.group(1) == "enter",
                   "config：默认发送键是 enter"))

    # ── 2. 三个函数都在 ──────────────────────────────────────────────
    for fn, why in (("request_voice_send", "登记待发送"),
                    ("cancel_voice_send", "取消待发送"),
                    ("maybe_send_after_voice", "到点执行发送")):
        checks.append((f"def {fn}(" in main_src, f"main：定义了 {fn}()（{why}）"))

    checks.append(("maybe_send_after_voice(_get_cfg())" in main_src,
                   "main：主循环真的调用了 maybe_send_after_voice"))

    # ── 3. 结束分支登记发送 ──────────────────────────────────────────
    i = main_src.find("语音会话【结束】（第二次按下语音键）")
    tail = main_src[i:i + 900] if i >= 0 else ""
    checks.append(("request_voice_send(_audio_frames)" in tail,
                   "main：语音结束后登记发送，且**带上本次帧数**"))

    # ── 4. 开始分支取消发送（最容易漏的那一半）──────────────────────
    j = main_src.find("语音会话【开始】")
    head = main_src[max(0, j - 900):j] if j >= 0 else ""
    checks.append(("cancel_voice_send(" in head,
                   "main：开始新一段时**取消**待发送"
                   "（否则「说完觉得不对、接着说」会把上一条残缺消息发出去）"))

    # ── 5. 误触保护 + 注入方式 ───────────────────────────────────────
    k = main_src.find("def maybe_send_after_voice(")
    body = main_src[k:k + 2500] if k >= 0 else ""
    checks.append(("if frames <= 0:" in body,
                   "main：零音频帧时不发送（误触保护 —— 否则会把输入框里原有内容发出去）"))
    checks.append(("tap_key(key)" in body,
                   "main：用 keys.tap_key 注入（SendInput，且会登记回声防自激）"))
    checks.append(("send_after_voice_delay_ms" in body and "send_after_voice_key" in body,
                   "main：延迟与按键都从配置读，不写死"))

    # ── 6. 白名单（漏了 = 用户改了配置静默不生效）────────────────────
    w = main_src.find("def _get_cfg()")
    wbody = main_src[w:w + 1600] if w >= 0 else ""
    for field in ("send_after_voice", "send_after_voice_delay_ms", "send_after_voice_key"):
        # ⚠ 必须匹配 `"字段":` 这个**白名单条目**的形态，不能只搜 `"字段"`。
        #   第一版就是只搜子串，于是同一条里的 getattr(new, "字段", 默认值)
        #   把检查蒙混过去了 —— 反例一跑就露馅（改坏了却全绿）。
        #   "看它出现过" 不等于 "它真的在那个 dict 里"。
        checks.append((f'"{field}":' in wbody,
                       f"main：_get_cfg 白名单含 {field}（否则改 config.json 静默不生效）"))

    # ── 7. main.py 能编译 ────────────────────────────────────────────
    #    顺带挡住 `global` 晚于使用那个坑（第一版就是它炸的）。
    try:
        compile(main_src, "main.py", "exec")
        compiles = True
    except SyntaxError:
        compiles = False
    checks.append((compiles, "main.py 能编译（含 global 时序这类硬错）"))

    checks.append((bool(re.search(r"from keys import[^\n]*tap_key", main_src)),
                   "main：从 keys 导入了 tap_key"))

    # ── 8. 帧计数清零的位置（这条揪出过一个"永远不触发"的真 bug）──────
    #    真机前的复核发现：清零原本写在 audio_start 分支**顶部**，而"第二次按下
    #    （＝收尾）"也会走 audio_start → 先清零、再回头读它去登记发送 →
    #    传进去永远是 0 → 零帧误触保护每次都被触发 → **自动发送永远不触发**，
    #    而且日志还会理直气壮地报「误触」，把排查方向指歪。
    #    这是本项目的老病：0 帧和误触长得一模一样，这次是程序自己清零造成的。
    a0 = main_src.find('logger.info("▶ Audio START")')
    a1 = main_src.find("elif not voice_active:")
    seg_start = main_src[a0:a1] if a0 >= 0 and a1 > a0 else ""
    checks.append(("_audio_frames = 0" not in seg_start,
                   "main：帧计数**不在** audio_start 顶部清零"
                   "（在那儿清零 = 收尾那次会被先清零 → 登记到的永远是 0 帧 → "
                   "零帧保护把每一次都当误触 → 自动发送永不触发）"))

    b0 = main_src.find('cancel_voice_send("又开始了一段新的语音")')
    b1 = main_src.find("voice_hotkey_down()", b0 if b0 >= 0 else 0)
    seg_new = main_src[b0:b1] if b0 >= 0 and b1 > b0 else ""
    checks.append(("_audio_frames = 0" in seg_new,
                   "main：帧计数在「开新一段」时清零（保证登记到的是本段帧数）"))

    # ── 9. 收尾路径不止一条：每一条都得接上（漏一条 = 那条路静默失效）──
    for anchor in ("语音会话【结束】（确认键）", "语音会话【结束】（厂商页确认键）"):
        p = main_src.find(anchor)
        w2 = main_src[p:p + 700] if p >= 0 else ""
        checks.append(("request_voice_send(" in w2,
                       f"main：收尾路径「{anchor}」也登记了发送"
                       "（只接一条路＝另外几条静默失效 —— 本项目反复踩的坑）"))

    checks.append((bool(re.search(r"if swallow:\s*\n\s*request_voice_send\(", main_src)),
                   "main：确认键那条只在 swallow 时补发送"
                   "（swallow=False 时这一下 OK 自己就变 Enter，补了就是连发两下）"))

    tp = main_src.find("自动结束，避免一直挂着听")
    tw = main_src[tp:tp + 1400] if tp >= 0 else ""
    checks.append(("cancel_voice_send(" in tw and "request_voice_send(" not in tw,
                   "main：超时收尾**不**自动发送"
                   "（麦克风开着最多 10 分钟，里面可能是环境音/旁人的话；"
                   "自动发出去比少发一次严重得多）"))

    return checks


def main() -> int:
    main_src = open(MAIN, encoding="utf-8").read()
    cfg_src = open(CFG, encoding="utf-8").read()

    checks = run_checks(main_src, cfg_src)
    ok = True
    print("=" * 66)
    print(" 闸：语音说完之后能自己发出去")
    print("=" * 66)
    for good, name in checks:
        print(f"  {'✅' if good else '❌'} {name}")
        if not good:
            ok = False

    # ── 反例：故意改坏，确认这道闸真的会红 ───────────────────────────
    #    "闸全绿"只有在"它本来会红"的前提下才有意义。
    print()
    print("── 反例（改坏后应当报红）──")

    def drop_send_after(src: str, anchor: str, span: int = 800) -> str:
        """把 anchor 之后 span 字内的 request_voice_send(...) 删掉（构造反例用）。

        ⚠ 必须按**位置窗口**删，不能用「日志行紧跟调用」那种正则：
        真实代码里那句话和调用之间夹着解释注释，正则匹配不上 → 等于没改坏 →
        反例会假绿（这道闸第一版就在这里假绿过一次）。
        """
        p = src.find(anchor)
        if p < 0:
            return src
        return (src[:p] + src[p:p + span].replace(
            "request_voice_send(_audio_frames)", "pass") + src[p + span:])

    negatives = [
        ("拿掉白名单里的 send_after_voice",
         main_src.replace('"send_after_voice": bool(', '"send_after_voice_DISABLED": bool('),
         cfg_src),
        ("拿掉「开始新一段就取消发送」",
         main_src.replace('cancel_voice_send("又开始了一段新的语音")', "pass"),
         cfg_src),
        ("拿掉零帧误触保护",
         main_src.replace("if frames <= 0:", "if False:"),
         cfg_src),
        ("把发送延迟改小到 100ms",
         main_src,
         cfg_src.replace("send_after_voice_delay_ms: int = 800",
                         "send_after_voice_delay_ms: int = 100")),
        ("拿掉结束分支的登记",
         main_src.replace("request_voice_send(_audio_frames)", "pass"),
         cfg_src),
        # 下面几条对应"复核时揪出来的真 bug"，每一条都必须能报红。
        ("把帧清零挪回 audio_start 顶部（＝自动发送永不触发的那个真 bug）",
         main_src.replace('logger.info("▶ Audio START")\n                state.clear_audio()',
                          'logger.info("▶ Audio START")\n                _audio_frames = 0\n                state.clear_audio()'),
         cfg_src),
        ("拿掉确认键那条收尾路径的登记",
         drop_send_after(main_src, "语音会话【结束】（确认键）"),
         cfg_src),
        ("拿掉厂商页确认键那条收尾路径的登记",
         drop_send_after(main_src, "语音会话【结束】（厂商页确认键）"),
         cfg_src),
        ("让超时收尾也自动发送（会把 10 分钟环境音发出去）",
         main_src.replace('cancel_voice_send("超时收尾：这段时间录到的内容不适合自动发")',
                          'cancel_voice_send("x")\n            request_voice_send(_audio_frames)'),
         cfg_src),
    ]
    for name, ms, cs in negatives:
        bad = [n for good, n in run_checks(ms, cs) if not good]
        if bad:
            print(f"  ✅ 反例：{name} → 报了 {len(bad)} 项红，这道闸是有效的")
        else:
            print(f"  ❌ 反例：{name} → **没报红**，这道闸有洞")
            ok = False

    print()
    print("PASS" if ok else "FAIL —— 上面打 ❌ 的那几条会让「说完还要去点鼠标」复发")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
