"""
语音快捷键自检工具 —— 不需要装任何第三方依赖（只用标准库）。

用法（在 remote-voice-bridge 目录下）：

  # 按住说话（PTT）—— 微信输入法「按住说话」Ctrl+Win 就是这一类
  python tools/test_voice_hotkey.py ctrl win

  # 按一下开始 / 再按一下结束（切换模式）—— 微信输入法「启动语音输入」
  # 左Ctrl+左Win+左Shift 就是这一类，**必须带 --tap**
  python tools/test_voice_hotkey.py lctrl lwin lshift --tap

它会：
  1. 给你 5 秒切到能输入文字的地方（记事本 / 编程工具的输入框）
  2-A. 不带 --tap：按住这组键 6 秒，再松开（模拟"按住说话"）
  2-B. 带 --tap：点按一次 → 等 8 秒看界面是否**持续留着** → 再点一次结束

判定：
  · 这 6 秒里输入法的「语音输入」界面弹出来了 → 这组键是对的
  · 没弹 → 这组键不对，换一组再试

**这是唯一能给出最终答案的办法。** 别在代码里猜输入法认哪个键 ——
键盘钩子能看到的事件和输入法真正判定的条件不是一回事，只能实测。

各家默认键（2026-09-14，以输入法自己的设置面板为准）：
  · **微信输入法**「设置 → 语音输入」里的「按住说话」= **Ctrl + Win** ← 本程序默认
    （同一页那条「启动语音输入」= 左Win+左Ctrl+左Shift，是切换模式，不是我们用的）
  · **豆包输入法** = **右 Alt**（也提供 右Alt+空格 / 左Ctrl+Win）。
  · 右 Alt 注意是**右侧**那个 Alt —— 这里写 ralt 而不是 alt。
  · ⚠ 面板里的键位是可以改的，所以**先打开面板看一眼，再决定这里试哪组**。

⚠ 关于 Ctrl+Win 的一条已知系统限制（实测，见 keys.py 的 hotkey_down 注释）：
  注入式按键里，**Ctrl 已经按下时再发 Win，系统会把 Win 的 vkCode 换成 0xFC**，
  钩子收不到真正的 0x5B；而且 Ctrl 和 Win 不会同时出现在 GetAsyncKeyState 里。
  我们已把顺序改成"Win 先落下"，这是合成按键能做到的最好情况
  （钩子能收到货真价实的 vk=0x5B/scan=0x5B 后再收到 Ctrl）。
  如果你的输入法仍然不认，就改用右 Alt —— 那一条是干净可靠的。
  所以**先用这条命令试一次**，再决定 config 里填哪组。

为什么必须"按住"：微信 / 豆包的语音输入都是
「长按说话、松开结束识别」。点一下只会录到几十毫秒的空气。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402  （下面是中文输出，先钉住编码）

_setup_utf8()

from keys import hotkey_down, hotkey_up, send_combo  # noqa: E402

# 常见候选，按"最可能可用"排序：默认对接的微信输入法用的就是 Ctrl+Win，先试它；
# 再试不涉及 Win、注入路径最干净的右 Alt（豆包的键）。
CANDIDATES = [
    ["ctrl", "win"],
    ["ctrl", "win", "shift"],
    ["ralt"],
    ["ralt", "space"],
    ["ctrl", "shift"],
]


def main() -> None:
    args = sys.argv[1:]
    # --tap / -t：点按一次（对应「启动语音输入」这类**切换模式**）。
    # 不带这个参数 = 按住 6 秒（对应「按住说话」这类 PTT 模式）。
    tap = "--tap" in args or "-t" in args
    keys = [a for a in args if a not in ("--tap", "-t")]

    if not keys:
        print("没给按键。可以先按下面这些候选逐个试：\n")
        for c in CANDIDATES:
            print("    python tools/test_voice_hotkey.py " + " ".join(c))
        print("\n切换模式（按一下开始 / 再按一下结束）要加 --tap，例如：")
        print("    python tools/test_voice_hotkey.py lctrl lwin lshift --tap")
        print("\n更好的做法：打开微信 / 豆包的设置，直接看「语音输入」的快捷键写的是什么。")
        return

    mode = "点按（切换模式）" if tap else "按住（PTT）"
    print(f"快捷键 = {' + '.join(keys)}   模式 = {mode}")
    print("5 秒后开始 —— 现在切到能输入文字的地方（记事本 / 编程工具输入框）")
    for i in range(5, 0, -1):
        print(f"  {i}...", end="\r", flush=True)
        time.sleep(1)

    if tap:
        # 切换模式：**点一下就开始**，之后不用按住，界面应当一直留在那儿。
        print("  点按一次 —— 看语音输入界面有没有弹出来**并且留着不消失**", flush=True)
        send_combo(keys)
        print("  已点按。接下来 8 秒请**不要碰键盘鼠标**，只观察界面是否持续在。")
        print("  （这 8 秒里对着麦克风说句话，看看有没有文字上屏）")
        time.sleep(8)
        print("  再点一次，结束这一次测试。")
        send_combo(keys)
        print("  已再点一次。")
        print("  ✓ 第 1 次点按后界面弹出并持续、说话有字上屏 → 这组键可用作切换模式")
        print("  ✗ 没弹 / 弹出后立刻消失 → 这组键注入没被输入法认到，看下面的兜底办法")
        return

    print("  按住中 —— 看输入法的语音输入界面有没有弹出来", flush=True)
    try:
        hotkey_down(keys)
        time.sleep(6)
    finally:
        # 无论如何都要松开，否则 Ctrl/Win 会卡在按下状态
        hotkey_up()

    print("  已松开。")
    print("  ✓ 弹出来了 → 这组键是对的，把它填进 config.json 的 voice_hotkey")
    print("  ✗ 没弹      → 换一组再试")


if __name__ == "__main__":
    main()
