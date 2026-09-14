"""
Button mapper — resolves remote button events to Windows actions.

映射规则完全由 config.json 的 `keymap` 决定，键位表见 config.MAPPING_TARGETS。
本模块只负责"查表 → 分发"，不内置任何按键行为，这样按键映射界面改完即时生效。
"""

from __future__ import annotations
import logging
from typing import Callable

logger = logging.getLogger("rvb.btn")

from config import CHROMECAST_BUTTONS, DEFAULT_KEYMAP, Config
from keys import send_key, send_combo, handle_mute_hold, hotkey_down, hotkey_up


# VK_DEFAULTS 已经删除：早前它把"按键 → 键名"硬编码在代码里，
# 于是 config 里的 keymap 只对不上号的那几个键有效，改配置改不动行为。
# 现在唯一的真源是 config.json 的 keymap。


def resolve_button(
    button_id: str,
    event_type: str,          # "down" | "up"
    keymap: dict[str, str] | None = None,
    on_voice: Callable[[bool], None] | None = None,
) -> bool:
    """
    Handle a remote button event.
    Returns True if the event was handled (should suppress system default).
    """
    cfg  = keymap if keymap is not None else Config.load().keymap
    mapped = str(cfg.get(button_id, "") or "").strip()

    # 语音键 —— 永远由 ATVV 语音通道处理，不参与普通按键映射
    if button_id == "voice" or mapped == "voice":
        if on_voice:
            on_voice(event_type == "down")
        return True

    # 禁用：什么都不做，也不拦截（遥控器原生键仍然生效）
    if not mapped:
        return False

    # 原样直通：遥控器自己的按键在 Windows 上本来就收得到，
    # 我们再注入一次只会变成双份。所以这里主动什么都不发。
    if mapped == "native":
        return False

    # 静音键按住 = 连发退格（沿用上游 remote-voice-bridge 的便捷行为）
    if button_id == "mute" and mapped == "mute":
        handle_mute_hold(event_type == "down")
        return True

    # 按住说话（PTT）—— 与语音键的「按一下开始 / 再按一下结束」是两套独立的键。
    #
    # ⚠ 必须放在下面 `if event_type != "down": return True` **之前**：
    #   那句是给普通映射用的（只在按下时触发一次、抬起不重复），
    #   PTT 恰恰**需要** up 事件去松开按键，被它挡掉的话
    #   Ctrl/Win 会永远卡在按下状态 —— 键盘直接失控。
    #
    # 用 HID 通道做 PTT 比用 ATVV 语音键合适得多：HID 的 down/up 天然配对，
    # 不像 ATVV 只能从 audio_start/audio_stop 两个沿去推断。
    if mapped == "voice_ptt":
        keys = Config.load().voice_ptt_keys or ["ctrl", "win"]
        if event_type == "down":
            hotkey_down(keys)
        else:
            hotkey_up()
        return True

    # 其它映射只在按下时触发一次，抬起不重复
    if event_type != "down":
        return True

    if "+" in mapped:
        send_combo([k.strip() for k in mapped.split("+") if k.strip()])
    else:
        send_key(mapped)
    return True
