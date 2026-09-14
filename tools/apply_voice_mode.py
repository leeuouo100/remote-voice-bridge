"""
语音模式一键切换 —— 呆瓜化入口，不需要懂任何概念，一条命令搞定。

用法（在 remote-voice-bridge 目录下）：

    python tools/apply_voice_mode.py            # 推荐：按一下长输 + 静音键按住说话
    python tools/apply_voice_mode.py --hold     # 退回：语音键按住说话（旧行为）
    python tools/apply_voice_mode.py --show     # 只看当前配置，不改

三种模式都**只用微信输入法原生能力**，程序只负责把遥控器的物理键翻译成
对应的那组键，状态（什么时候开始、什么时候结束）全部由微信输入法自己管：

  【推荐】语音键 = 微信「启动语音输入」左Ctrl+左Win+左Shift
          按一下开始 → **松手也继续听** → 再按一下（或按任意键，含确认键）结束
          静音键 = 微信「按住说话」Ctrl+Win，按住说、松手结束

  【--hold】语音键 = 微信「按住说话」Ctrl+Win，按住说、松手结束
           （遥控器麦克风只在按住时才推流；松手后声音来自电脑麦克风）

改完**立即生效**，不用重启程序（控制台和主程序都按文件修改时间热加载配置）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402  （下面是中文输出，先钉住编码）

_setup_utf8()

from config import (  # noqa: E402
    Config, INPUT_METHODS, apply_recommended, DEFAULT_KEYMAP,
)


def _show(c: Config) -> None:
    im = INPUT_METHODS.get(c.input_method, {})
    print(f"  语音键  : {im.get('desc', c.input_method)}")
    print(f"            转发按键 = {' + '.join(c.trigger_keys_windows()) or '(空)'}"
          f"   触发方式 = {'点一下开始/再点一下结束' if c.hotkey_mode == 'tap' else '按住说话'}")
    print(f"  静音键  : {c.keymap.get('mute')}"
          f"{'（按住 ' + ' + '.join(c.voice_ptt_keys) + ' 说话，松手结束）' if c.keymap.get('mute') == 'voice_ptt' else ''}")
    print(f"  系统麦克风参与混音 : {c.system_mic_enabled}"
          "   ← 切换模式下松手后，声音靠它")
    print(f"  按键映射总开关     : {c.mapping_enabled}")


def main() -> None:
    args = sys.argv[1:]

    if "--show" in args:
        print("当前配置：")
        _show(Config.load())
        return

    if "--hold" in args:
        c = Config.load()
        c.input_method   = "wechat"           # 按住说话 Ctrl+Win
        c.hotkey_mode    = "hold"
        c.voice_hotkey   = []
        c.keymap         = dict(DEFAULT_KEYMAP)
        c.keymap["mute"] = "mute"             # 静音键还给系统静音
        c.save()
        print("已切回「按住说话」模式：")
        _show(c)
        return

    c = apply_recommended()
    print("已应用推荐配置（按一下长输 + 静音键按住说话）：")
    _show(c)
    print()
    print("现在按一下遥控器的语音键 → 应该能一直说，松手也不停；")
    print("再说完按一次语音键（或按确认键）→ 这段结束并上屏。")
    print()
    print("如果按语音键**完全没反应**，说明微信输入法没认到注入的这组三键，")
    print("执行这条退回（不丢任何东西）：")
    print("    python tools/apply_voice_mode.py --hold")


if __name__ == "__main__":
    main()
