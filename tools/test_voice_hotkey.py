"""
语音快捷键自检工具 —— 不需要装任何第三方依赖（只用标准库）。

用法（在 remote-voice-bridge 目录下）：
    python tools/test_voice_hotkey.py ralt
    python tools/test_voice_hotkey.py ctrl win
    python tools/test_voice_hotkey.py alt shift m

它会：
  1. 给你 5 秒切到能输入文字的地方（记事本 / 编程工具的输入框）
  2. 按住这组键 6 秒（模拟"遥控器语音键按住说话"）
  3. 松开

判定：
  · 这 6 秒里输入法的「语音输入」界面弹出来了 → 这组键是对的
  · 没弹 → 这组键不对，换一组再试

**这是唯一能给出最终答案的办法。** 别在代码里猜输入法认哪个键 ——
键盘钩子能看到的事件和输入法真正判定的条件不是一回事，只能实测。

各家默认键（2026-09-14 核对）：
  · 微信 PC 端 4.1.8+：按住 **Ctrl+Win**（系统级全局可用）；
    想免按住就用 **Ctrl+Win+Shift** 切"持续输入"。
  · 豆包输入法：**右 Alt**（也提供 右Alt+空格 / 左Ctrl+Win）。
  · 注意是**右侧**那个 Alt —— 这里写 ralt 而不是 alt。

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

from keys import hotkey_down, hotkey_up  # noqa: E402

# 常见候选，按"最可能可用"排序：先试干净的右 Alt，再试受系统限制的 Win 组合
CANDIDATES = [
    ["ralt"],
    ["ralt", "space"],
    ["ctrl", "win"],
    ["ctrl", "win", "shift"],
    ["ctrl", "shift"],
]


def main() -> None:
    keys = sys.argv[1:]
    if not keys:
        print("没给按键。可以先按下面这些候选逐个试：\n")
        for c in CANDIDATES:
            print("    python tools/test_voice_hotkey.py " + " ".join(c))
        print("\n更好的做法：打开微信 / 豆包的设置，直接看「语音输入」的快捷键写的是什么。")
        return

    print(f"快捷键 = {' + '.join(keys)}")
    print("5 秒后开始按住 6 秒 —— 现在切到能输入文字的地方（记事本 / 编程工具输入框）")
    for i in range(5, 0, -1):
        print(f"  {i}...", end="\r", flush=True)
        time.sleep(1)

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
