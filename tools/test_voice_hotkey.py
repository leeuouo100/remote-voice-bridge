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

为什么默认是「右 Alt」：
  微信输入法（Windows 2.1.0+）的语音唤起键就是长按**右 Alt**，全局生效；
  豆包输入法也是右 Alt。注意是**右侧**那个 Alt —— 这里写 ralt 而不是 alt。
  右边没有 Alt 键的键盘（如部分 60% 配列），用右 Alt 的等价物 AltGr 位置。

为什么必须"按住"：微信输入法 / 豆包输入法的语音输入都是
「长按说话、松开结束识别」。点一下只会录到几十毫秒的空气。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keys import hotkey_down, hotkey_up  # noqa: E402

# 常见候选，按可能性排序
CANDIDATES = [
    ["ralt"],
    ["ralt", "space"],
    ["ctrl", "win"],
    ["ctrl", "shift"],
    ["ctrl", "shift", "v"],
]


def main() -> None:
    keys = sys.argv[1:]
    if not keys:
        print("没给按键。可以先按下面这些候选逐个试：\n")
        for c in CANDIDATES:
            print("    python tools/test_voice_hotkey.py " + " ".join(c))
        print("\n更好的做法：打开微信输入法设置，直接看「语音输入」的快捷键写的是什么。")
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
