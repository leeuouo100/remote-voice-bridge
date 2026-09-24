"""
Config management — per-device settings, trigger keys, input method bindings.
Config file: ~/.config/remote-voice-bridge/config.json
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / "remote-voice-bridge"
CONFIG_PATH = CONFIG_DIR / "config.json"

# 版本号唯一真源：控制台「设置 → 关于」显示它，installer.iss 的 MyAppVersion 也要跟着改。
APP_VERSION = "1.0.18"

# 配置**结构**版本号（和 APP_VERSION 是两回事）。
# 改默认值/改字段含义时 +1，并在 _migrate() 里补一条迁移。
# 旧版写下的 config.json 里没有这个字段 → 视为 0。
#
# 2 = v1.0.11：按键改由厂商页接管 → 方向键不能再是「原样直通」
#     （那个默认值的含义已经变了），4 个空项也补上默认动作。
CONFIG_VERSION = 3


# ── Device signatures ────────────────────────────────────────────────────────
@dataclass
class DeviceSig:
    vid: int
    pid: int
    name_patterns: list[str]   # substrings matched against device name (case-insensitive)


DEVICES: dict[str, DeviceSig] = {
    "chromecast": DeviceSig(
        vid=0x18D1, pid=0x9450,
        name_patterns=["google", "chromecast", "remote"],
    ),
    "x6": DeviceSig(
        vid=0x1D5A, pid=0xC081,
        name_patterns=[],   # match by VID/PID only
    ),
}


# ── Input method bindings ────────────────────────────────────────────────────
#
# ⚠ 键位更正（2026-09-14，以输入法自己的设置面板为准）：
# 早前这里写的是「微信输入法默认长按右 Alt」——**那是不对的**。
# 打开微信输入法「设置 → 语音输入」，面板上白纸黑字写着：
#   · 按住说话（PTT）         = **Ctrl + Win**     ← 本程序用的就是这一条
#   · 启动语音输入（切换模式）  = 左 Win + 左 Ctrl + 左 Shift
# **右 Alt 是豆包输入法**的默认（它给 右Alt / 右Alt+空格 / 左Ctrl+Win 三选一）。
#
# 另：微信 PC 客户端 4.1.8+ 的语音输入绑的也是 Ctrl+Win（持续输入用 Ctrl+Win+Shift），
# 且是系统级可用 —— 早前文档里那句"只在微信窗口内有效"同样是错的。
#
# 但各家都允许用户改键，所以**不要把键位当真理写死**：
# 拿不准就去输入法设置面板看一眼上面写的到底是哪几个键，
# 或者用 tools/test_voice_hotkey.py 实测一组。控制台里也能直接改/录制。
INPUT_METHODS = {
    "wechat": {
        "macos_keys":    [],
        "windows_keys":  ["ctrl", "win"],
        "bundle_macos":  "com.tencent.xinshurufa",
        "desc": "微信输入法（按住说话 Ctrl+Win）",
    },
    "wechat_hold_mode": {
        # 「启动语音输入」= 微信输入法面板上的**切换模式**：
        #   按一下开始聆听，**再按一下（或按任意键）结束**，全程不用按住。
        # 与下面 wechat 那条「按住说话」（Ctrl+Win，松手结束）是**两套独立的键**，
        # 解决的是同一个痛点（不想一直按着）的两个不同方案，别混用。
        #
        # 键位以微信输入法「设置 → 语音输入」面板为准：**左Ctrl + 左Win + 左Shift**。
        # 面板明确写的是"左"，所以这里必须下发 lctrl/lwin/lshift ——
        # 用通用的 ctrl/shift 属于"碰运气能触发"，右键不会被认成这一组。
        "macos_keys":    [],
        "windows_keys":  ["lctrl", "lwin", "lshift"],
        "desc": "微信输入法 · 启动语音输入（左Ctrl+左Win+左Shift · 按一下开始）",
    },
    "doubao": {
        "macos_keys":    [],
        "windows_keys":  ["ralt"],
        "bundle_macos":  "com.bytedance.inputmethod.doubaoime",
        "desc": "豆包输入法（按住右 Alt）",
    },
    "custom": {
        "macos_keys":    [],
        "windows_keys":  [],
        "desc": "自定义（下面自己录制）",
    },
}


# 语音触发键的预设档 —— 控制台下拉框直接用这张表。
# 值 = 键名列表；"custom" 是哨兵，表示"用下面录制的组合键"。
#
# label 刻意只写**按键本身**（对标 vRemoter 下拉里的 "Option (⌥)"），
# 那一长串「哪个输入法用它」的说明放进 hint，由界面放在下拉的 tooltip 里 ——
# 下拉项太长会撑破卡片，而且用户扫一眼要的是"按哪几个键"，不是读句子。
VOICE_HOTKEY_PRESETS: list[dict] = [
    # label 只放按键本身（对标 vRemoter 的 "Option (⌥)"）；
    # 哪个输入法用它、有什么限制，全部放 hint（悬浮提示），
    # 否则下拉框会被一整句话撑得又宽又长，反而看不清按的是哪个键。
    {"id": "ctrl+win",       "keys": ["ctrl", "win"],          "label": "Ctrl + Win",
     "hint": "微信输入法「按住说话」就是这一组（设置 → 语音输入里写的）· "
             "注入式 Win 组合受系统限制，建议先用 tools/test_voice_hotkey.py 实测一次"},
    # ⚠️ 这一档现在就是**默认档**：微信输入法的「启动语音输入」写的正是
    #    左Ctrl+左Win+左Shift（按一下开始、再按一下结束）。
    #    表里存的是不带左右前缀的 ctrl/win/shift，而 input_method 内置的是
    #    lctrl/lwin/lshift —— combo_equivalent 认为它们是同一只键，所以下拉框
    #    会正确落在这一档上（别再改成"左右不同"的第二档，会撞档）。
    {"id": "ctrl+win+shift", "keys": ["ctrl", "win", "shift"], "label": "Ctrl + Win + Shift",
     "hint": "微信输入法「启动语音输入」· 按一下开始、再按一下结束（**默认档**，本程序语音键用的就是它）· "
             "键位以输入法「设置 → 语音输入」面板为准"},
    {"id": "ralt",           "keys": ["ralt"],                 "label": "右 Alt",
     "hint": "豆包输入法默认 · 按住说话 · 不涉及 Win，注入路径最干净"},
    {"id": "ralt+space",     "keys": ["ralt", "space"],        "label": "右 Alt + 空格",
     "hint": "豆包输入法备选 · 按住说话"},
    {"id": "custom",         "keys": [],                       "label": "自定义…",
     "hint": "自己录制一组组合键"},
]


# ── Button definitions (Chromecast Voice Remote) ─────────────────────────────
# order 决定按键映射界面里的行序 —— 跟遥控器实机的物理布局对齐
# （从上到下：电源/语音 → 方向/确认 → 返回/Home → 音视频 → 音量）。
CHROMECAST_BUTTONS = {
    "up":      {"usage": 0x03, "label": "方向上", "order": 10},
    "down":    {"usage": 0x04, "label": "方向下", "order": 11},
    "left":    {"usage": 0x05, "label": "方向左", "order": 12},
    "right":   {"usage": 0x06, "label": "方向右", "order": 13},
    "ok":      {"usage": 0x07, "label": "确认",   "order": 14},
    "back":    {"usage": 0x0B, "label": "返回",   "order": 20},
    "home":    {"usage": 0x0A, "label": "Home",   "order": 21},
    "mute":    {"usage": 0x08, "label": "静音",   "order": 30},
    "vol_up":  {"usage": 0x0C, "label": "音量＋", "order": 31},
    "vol_down":{"usage": 0x0D, "label": "音量－", "order": 32},
    "voice":   {"usage": "voice", "label": "语音", "order": 5},
    "youtube": {"usage": 0x0E, "label": "YouTube","order": 40},
    "netflix": {"usage": 0x0F, "label": "Netflix","order": 41},
    "power":   {"usage": 0x01, "label": "电源",   "order": 1},
    "input":   {"usage": 0x11, "label": "信源",   "order": 2},
}


# ── 按键映射目标（对标 vRemoter 的 RemoteMappingTarget，语义换成 Windows）─────
# 键 = 写进 config.json 的 keymap 值；值 = 界面上显示的中文名。
# 空字符串 "" 是"禁用"，和 vRemoter 的 .disabled 对应。
# "native" 是 Windows 特有的一个必要选项，见下方注释。
MAPPING_TARGETS: dict[str, str] = {
    "":            "禁用（不发送任何按键）",
    # ⚠ v1.0.11 起这一项对遥控器按键**等于禁用**（遥控器按键发在厂商页，
    #   Windows 不处理，原生按键并不会自己生效）。保留它是为了给"程序读到键
    #   但什么都不发"这个行为留个名分，也让老配置不会被读成非法值。
    "native":      "原样直通（程序读到但什么都不发）",
    "voice":       "语音输入（ATVV 语音键专用）",
    # 按住说话（PTT）—— 给**非语音键**（默认挂在静音键上）用的第二种语音方式。
    # 与语音键那套「按一下开始 / 再按一下结束」是**两套独立的键、两条独立的通道**：
    #   · 语音键 走 ATVV（audio_start/audio_stop），只能感知"按下/松开"两个沿 → 适合切换
    #   · 静音键 走 HID，down/up 天然配对                                → 适合按住
    # 一个键当开关、一个键当油门，互不干扰，两种习惯都能照顾到。
    "voice_ptt":   "按住说话 PTT（按下=开始，松开=结束）",

    "up":          "方向上  ↑",
    "down":        "方向下  ↓",
    "left":        "方向左  ←",
    "right":       "方向右  →",
    "enter":       "回车 / 换行 / 确认  Enter",
    # 「换行」单独列出来，是因为它**不是**一个键，而是随输入框而变的一件事：
    #   · 多行输入框（评论区、富文本、代码框）：Enter 本身就是换行
    #   · 微信/QQ 这类"Enter 发送"的聊天框：Shift+Enter（或 Ctrl+Enter）才是换行
    # 以前表里只有 Enter 一项、还只叫"回车/确认"，用户按"换行"去找根本找不到，
    # 只能拿 Enter 硬试 —— 试对了是运气，试错了就把没写完的消息发出去。
    "shift+enter": "换行（Shift+Enter）",
    "ctrl+enter":  "换行（Ctrl+Enter）",
    "escape":      "返回 / 取消  Esc",
    "backspace":   "退格删除  Backspace",
    "delete":      "删除  Delete",
    "tab":         "Tab",
    "space":       "空格",
    "pageup":      "Page Up",
    "pagedown":    "Page Down",
    "home":        "Home（行首）",
    "end":         "End（行尾）",
    "volumeup":    "系统音量＋",
    "volumedown":  "系统音量－",
    "mute":        "系统静音（按住＝连删）",
    "playpause":   "播放 / 暂停",
    "win":         "开始菜单  Win",
    "win+d":       "显示桌面  Win+D",
    "win+s":       "搜索  Win+S",
    "win+shift+s": "截图  Win+Shift+S",
    "win+e":       "文件资源管理器  Win+E",
    "alt+tab":     "切换窗口  Alt+Tab",
    "ctrl+c":      "复制  Ctrl+C",
    "ctrl+v":      "粘贴  Ctrl+V",
    "ctrl+z":      "撤销  Ctrl+Z",
    "ctrl+a":      "全选  Ctrl+A",
    "ctrl+shift+esc": "任务管理器  Ctrl+Shift+Esc",
}

# 这几个目标**不是"要下发某个组合键"**，而是由程序内部逻辑接管的虚拟动作，
# 不能拿去 keys._resolve_key() 解析 —— 自检脚本遇到它们要跳过。
#   ""          禁用
#   "native"    原样直通（不注入任何键）
#   "voice"     ATVV 语音键专用（走语音会话状态机，不是组合键）
#   "voice_ptt" 按住说话 PTT（按下发组合键、松开发抬起，逻辑在 buttons.py）
# 新增虚拟目标时**必须同步加进这个集合**，否则 check_keymap.py 会把它当组合键
# 去解析然后报「键名无法解析」而失败。
VIRTUAL_TARGETS = {"", "native", "voice", "voice_ptt"}

# 「原样直通」的说明 —— 界面和文档共用同一份文案，避免两处说法不一致。
#
# ⚠ v1.0.11 改写：以前写的是「它自己的按键 Windows 本来就收得到」，
#   那是**错的**，也是"上下键没反应"却查不出原因的根源 ——
#   遥控器把按键发在厂商自定义页上，Windows 不处理厂商页，
#   原生按键根本收不到。所以「原样直通」对遥控器按键实际等于"什么都不做"。
NATIVE_TARGET_HINT = (
    "遥控器的按键发在 HID 厂商自定义页上，Windows 不处理这一页，\n"
    "所以遥控器自己的按键**并不会**变成 Windows 的按键（v1.0.11 起由本程序读厂商页接管）。\n"
    "选「原样直通」＝程序读到这个键但什么都不发（等于禁用）。\n"
    "想让按键生效，请在这里选一个具体动作（如方向上、Esc、Win+D…）。"
)


# Default keymap
#
# ⚠⚠ v1.0.11 起方向键**不再是**「原样直通」—— 这是本表最重要的一条变更。
#   遥控器把**所有**按键都发在两个厂商自定义页（0xFF01 / 0xFF80）上，
#   而 Windows 根本不处理厂商页（只认键盘页 / 消费类页 / 鼠标页）。
#   也就是说这些键**不会自己变成 Windows 的方向键**，"原样直通"="什么都不做"。
#   所以默认值改成真正注入方向键；程序自己读厂商页、自己解、自己发。
#   （诊断证据：5 路 HID 通道全 0 条；参考实现 vRemoter 同样是自开厂商页。）
#
# 剩下的键按遥控器物理布局给一套开箱即用的默认值，**不留空项** ——
# 「装上就要能用」，不能让用户先去界面上把 4 个键填一遍才发现能按。
DEFAULT_KEYMAP = {
    "up":      "up",
    "down":    "down",
    "left":    "left",
    "right":   "right",
    "ok":      "enter",
    "back":    "escape",
    "home":    "win+d",
    "youtube": "win+s",
    "netflix": "playpause",
    "power":   "win+e",
    "input":   "alt+tab",
    # 静音键默认给「按住说话」用：它是遥控器上唯一一个**按着不别扭**的键，
    # 而语音键已经分给「按一下开始 / 再按一下结束」了。
    # 想要回系统静音，在按键映射界面把它改回「系统静音」即可。
    "mute":    "voice_ptt",
    "vol_up":  "volumeup",
    "vol_down":"volumedown",
    "voice":   "voice",
}


def keymap_targets_with_custom(extra: str | None = None) -> dict[str, str]:
    """给按键映射下拉框用：内置目标 + 当前值（若它是录制出来的自定义组合键）。

    extra 是某个按键现有的自定义值（例如 "ctrl+alt+m"）。它不在 MAPPING_TARGETS
    里，若不补进选项表，ttk.Combobox 会因为值不在列表里而显示空白 ——
    用户会以为映射丢了。
    """
    out = dict(MAPPING_TARGETS)
    if extra and extra not in out:
        out[extra] = f"自定义：{extra}"
    return out


def ordered_buttons() -> list[tuple[str, dict]]:
    """按键列表，按界面上希望的物理顺序排。"""
    return sorted(CHROMECAST_BUTTONS.items(), key=lambda kv: kv[1].get("order", 99))


# ── Config ────────────────────────────────────────────────────────────────────
# 把内部键名翻成人话，给控制台/日志用。
# 为什么要翻：`ralt` 这种写法用户看不懂，而「右 Alt / 左 Alt 是两个不同的键」
# 恰恰是这个项目最容易踩的坑，直接显示中文能省掉一轮排查。
_KEY_LABELS = {
    "ralt": "右Alt", "altgr": "右Alt", "lalt": "左Alt", "alt": "Alt",
    "lctrl": "左Ctrl", "rctrl": "右Ctrl", "ctrl": "Ctrl", "control": "Ctrl",
    "lshift": "左Shift", "rshift": "右Shift", "shift": "Shift",
    "lwin": "左Win", "rwin": "右Win", "win": "Win",
    "space": "空格", "enter": "回车", "tab": "Tab", "esc": "Esc",
}


def hotkey_label(keys: list[str] | None) -> str:
    """['ralt'] → '右Alt'；不认识的原样大写返回。"""
    if not keys:
        return ""
    return "+".join(_KEY_LABELS.get(str(k).strip().lower(), str(k).upper()) for k in keys)


@dataclass
class Config:
    device:           str       = "chromecast"
    keymap:           dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYMAP))
    # 默认直接给「按一下就能长输」那一套，装好即用、不用做任何选择：
    # 语音键转发微信输入法原生的「启动语音输入」（切换式）。
    # 注意分工：输入法的原生能力负责"怎么说话"，**语音会话的开始/结束由本程序管**
    # （见 main.py 的 voice_active 状态机）。
    # 想退回"按住说话"，在控制台「设置」页把触发方式改回「按住说话」即可。
    input_method:     str       = "wechat_hold_mode"
    custom_keys:      list[str] = field(default_factory=list)  # e.g. ["alt", "shift", "m"]
    audio_output:     str       = "CABLE Input"
    gain:             float     = 10.0
    watchdog_timeout: int       = 180
    reconnect_delay:  int       = 5
    key_check_window: float     = 3.0
    heartbeat_cooldown: float   = 2.0
    gatt_timeout:     float     = 5.0
    voice_mode:       str       = "toggle"    # "toggle" | "hold"
    # 语音快捷键的触发方式：
    #   "tap"  = 点一下开始、再点一下结束（切换式）← **默认**
    #            配合上面的「启动语音输入」，遥控语音键按一下就能长输，松手不断。
    #   "hold" = 按住（「按住说话」PTT：长按说话、松开结束识别）
    hotkey_mode:      str       = "tap"
    # 语音会话进行中按「确认键」时，要不要把这一下 Enter 吞掉。
    #
    # 为什么需要吞：遥控器按语音键 → 输入法进入语音输入；此时按确认键，
    # 用户要的是「这段说完了」，不想顺手在聊天框里发出一个回车。
    #
    # ⚠ 为什么给开关：Windows 的低级键盘钩子**分不出遥控器和物理键盘**
    #   （两者走同一套 HID 输入管道，都没有 LLKHF_INJECTED 标志），
    #   所以开着的时候，**物理键盘的 Enter 也会一起被吞**。
    #   需要一边说话一边敲键盘的人，把这个关掉即可 ——
    #   关掉后确认键仍会结束语音会话，只是额外多打一个回车。
    swallow_ok_during_voice: bool = True

    # ── 语音会话结束后「自动发送」（⚠ 默认关）────────────────────────
    # 这是一个**可选**能力，默认关。默认行为是：
    #   说完 → 按语音键结束 → 文字落进输入框 → **什么时候发由你来决定**。
    #
    # 🔴 为什么默认必须是关的（2026-09-17 武哥否掉了原来的"默认开"，他是对的）：
    #   自动发送会把"还没想好的话"替你发出去。真实场景：说到一半去上厕所，
    #   回来想接着改、改完再发 —— 结果程序早就替你把半截话发出去了，
    #   而且是发到一个真人对话里。**这个代价远大于"要自己动手发一下"。**
    #   宁可不发，也不能替用户说出他还没决定要说的话。
    #
    # 那"不用摸鼠标"怎么办？—— 按**键盘上的回车**就够了。
    #   写代码/打字时手本来就在键盘上，回车是一个已经有的、零成本的动作；
    #   原来真正别扭的是"得去够鼠标点一下"，那一下才打断思路。
    #   （遥控器上的按键至今在 Windows 上收不到报告，见 remote_hid.py 文件头；
    #    哪天那条路通了，可以把它绑成一个「发送」键 —— 那才是既省事、又由你决定。）
    #
    # ⚠ 只有你真把开关打开时，下面这些保护才会起作用，且**四条收尾路径都在管**：
    #   · 开始新一段 → 取消上一条待发送（否则"说完觉得不对、接着说"会把残缺消息发出去）
    #   · 这一段一帧音频都没收到 → 不发（误触；按回车会把输入框里**原有内容**发出去）
    #   · 超时收尾（挂着忘了关）→ **永不发送**（那段时间很可能录到环境音/旁人的话）
    #   · 延迟不能太小 → 会在文字落进输入框**之前**就打回车，等于把刚说的话弄丢一次
    send_after_voice: bool = False
    # 等输入法把文字落进输入框再发。太小会"文字还没进去就把消息发出去了"，
    # 那比不自动发更糟（等于把用户刚说的话弄丢一次），所以给得比较宽。
    send_after_voice_delay_ms: int = 800
    # 发送用哪个键。键名同 keys.py（enter / ctrl+enter / shift+enter / space …）。
    # 微信、QQ、以及大部分 AI 对话框都是 Enter 发送。
    send_after_voice_key: str = "enter"

    # 非空则覆盖 INPUT_METHODS 内置组合。
    # 键名支持：ralt(右Alt) / lalt / alt / ctrl / win / shift / 字母 / f1-f24 / space …
    # 例如 ["ralt"] 或 ["ctrl","win"] 或 ["ralt","space"]
    # 用 tools/test_voice_hotkey.py 可以直接试哪组键能唤起输入法。
    voice_hotkey:     list[str] = field(default_factory=list)
    # 静音键等「按住说话」键位用的组合键，与 voice_hotkey 是**两套独立的键**。
    # 默认 Ctrl+Win = 微信输入法「按住说话」（按住可语音，松手结束）。
    # 想改成别的（例如豆包的右 Alt）就在这里填，例如 ["ralt"]。
    voice_ptt_keys:   list[str] = field(default_factory=lambda: ["ctrl", "win"])
    # True = 拦截已映射的键。注意遥控器走 HID，与物理键盘无法区分，
    # 开启后物理键盘的 Enter/Esc/方向键也会被吞掉，故默认关闭。
    suppress_keys:    bool      = False

    # ── 混音设置（系统麦克风 + 遥控器麦克风 → 混合输出）────────────────────
    # 为什么需要：只把遥控器那一路播给 CABLE Input 的话，微信/豆包就**完全听不到
    # 房间里的声音** —— 想一边用电脑麦克风说话、一边用遥控器语音输入，做不到。
    # vRemoter 在 macOS 上靠一个自研 2ch 虚拟声卡做这件事；Windows 上不需要
    # 额外驱动，直接在软件层把两路 PCM 相加即可，而且每路都能单独调增益/静音/独奏。
    system_mic_enabled: bool  = True      # 电脑麦克风是否参与混音
    remote_mic_enabled: bool  = True      # 遥控器麦克风是否参与混音
    system_mic_device:  str   = ""        # 空 = 用系统默认输入设备
    system_mic_gain:    float = 1.0       # 系统麦克风独立增益（1x–10x）

    # 按键映射总开关（对应控制台「按键映射」页右上角的开关）。
    # 关掉后遥控器按键一律不处理 —— 等于让遥控器恢复成一只普通 HID 遥控器。
    mapping_enabled:    bool  = True

    # 读「厂商自定义页」的按键（v1.0.11 起按键映射**全靠这一路**）。
    #
    # 为什么需要：遥控器把所有按键都发在两个厂商页（0xFF01 / 0xFF80）上，
    # Windows **完全不处理厂商页**，键盘钩子永远看不到 —— 关掉这个开关，
    # 除了走 ATVV 的语音键之外，其它按键一个都不会生效。
    #
    # 什么时候关：万一某个键出现了「按一下出两个动作」（遥控器在别的机器上
    # 走的是标准键盘页、原生键也能用），关掉它退回纯键盘钩子那条路排查。
    hid_vendor_keys:    bool  = True

    # 配置结构版本。旧版 config.json 里没有这个字段（= 0），
    # load() 会据此跑一次性迁移 —— 见 _migrate()。
    config_version:     int   = CONFIG_VERSION

    def trigger_keys_windows(self) -> list[str]:
        """Return the Windows hotkey list for the configured input method."""
        if self.voice_hotkey:
            return list(self.voice_hotkey)
        im = INPUT_METHODS.get(self.input_method, INPUT_METHODS["wechat"])
        if self.input_method == "custom":
            # 自定义但还没录制任何键时的兜底 —— 跟默认输入法（微信输入法）保持一致，
            # 别退回右 Alt：那是豆包的键，在这里属于"猜错比不猜更糟"。
            return self.custom_keys if self.custom_keys else ["ctrl", "win"]
        return im["windows_keys"]

    def trigger_keys_macos(self) -> list[str]:
        im = INPUT_METHODS.get(self.input_method, INPUT_METHODS["wechat"])
        if self.input_method == "custom":
            return self.custom_keys if self.custom_keys else ["option"]
        return im["macos_keys"]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                # 旧版写下的 config.json 没有这个字段 → 0，据此判断要不要迁移。
                # ⚠ 必须读**原始 data**，不能用 defaults：defaults 里它已经是当前版本了。
                saved_version = int(data.get("config_version") or 0)
                defaults = cls().to_dict()
                # keymap 单独合并：旧版本写下的 config.json 里没有新增的按键，
                # 直接整体覆盖会让这些键变成"未配置"，表现为按键失效。
                km = dict(DEFAULT_KEYMAP)
                if isinstance(data.get("keymap"), dict):
                    # ⚠ 原来这里是 `if isinstance(v, str)` 一句话过滤掉非字符串，
                    # 被丢掉的条目**一声不响** —— 用户手改 config.json 写错了值
                    # （或旧版本留下了别的类型），那个按键就永远没反应，
                    # 而且日志里一个字都没有。这里补一条告警，别让它继续静默。
                    dropped = [
                        f"{k}={v!r}" for k, v in data["keymap"].items()
                        if not isinstance(v, str)
                    ]
                    if dropped:
                        print("[CONFIG] ⚠ keymap 里有非字符串的值，已忽略（该按键会没反应）："
                              + "、".join(dropped))
                    km.update({k: v for k, v in data["keymap"].items()
                               if isinstance(v, str)})
                defaults.update(data)
                defaults["keymap"] = km
                cfg = cls(**defaults)
                if saved_version < CONFIG_VERSION:
                    # 升级用户的旧配置不会被新默认值覆盖（否则等于偷偷改用户的设置），
                    # 但**默认值本身变了**的那几项必须搬过去 —— 不然新装的用户有
                    # 「静音键按住说话」，老用户永远看不到，还以为是坏了。
                    cfg = _migrate(cfg, saved_version)
                _warn_mode_mismatch(cfg)
                return cfg
            except Exception as e:
                print(f"[CONFIG] Load error: {e}")
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _warn_mode_mismatch(cfg: "Config") -> None:
    """`voice_mode` 与 `hotkey_mode` 说的是同一件事，却可能互相打架。

    · `hotkey_mode` 决定**注入方式**：tap = 点一下（切换式快捷键）/
      hold = 按住不放。这个对真机是**真正生效**的那个。
    · `voice_mode` 决定 session 的开麦状态机 —— 但那条分支只在收到
      `START_SEARCH` 时才走到，而**真机遥控器从不发 START_SEARCH**
      （见 `session.ensure_mic_open` 的说明），所以它对真机行为其实没有影响。

    于是两个字段不一致时，用户看到的行为和配置面板上写的对不上。
    2026-09-15 的真机事故正是这个：配的是微信输入法「启动语音输入」
    （左Ctrl+左Win+左Shift，切换式），`hotkey_mode` 却停在 "hold"，
    程序按「按住不放」的方式发它 —— 表现就是「一松手就不再调用输入法」。

    这里只**提醒**，不擅自改用户配置。
    """
    if (cfg.voice_mode == "hold") != (cfg.hotkey_mode == "hold"):
        what = "按住说话" if cfg.hotkey_mode == "hold" else "按一下开始 / 再按一下结束"
        print("[CONFIG] ⚠ voice_mode 与 hotkey_mode 不一致："
              f"voice_mode={cfg.voice_mode!r}，hotkey_mode={cfg.hotkey_mode!r}")
        print(f"[CONFIG]    实际生效的是 hotkey_mode（当前 = {what}）；"
              "voice_mode 对真机无效（遥控器从不发 START_SEARCH）")
        print("[CONFIG]    请到控制台「设置 → 语音 → 触发方式」核对一下")


def _migrate(cfg: "Config", from_version: int) -> "Config":
    """旧配置一次性迁移。

    为什么需要：配置是「读旧的、补新的」（见 Config.load），**旧文件里已经写死的值
    不会被新默认值覆盖** —— 这是对的，不能偷偷改用户的设置。但默认值**本身**变了
    的那几项就麻烦了：新装用户有「静音键 = 按住说话」，升级用户永远看不到，
    只会觉得「静音键不起作用」（v1.0.4 真实反馈）。

    所以这里只搬**默认值改过**的那几项，且只在用户没动过的时候搬。
    """
    changed = []

    if from_version < 1:
        # v1.0.3 起静音键默认改成了「按住说话」（voice_ptt）。
        # 旧版默认是「系统静音」（mute），用户若是自己改的就不动。
        if cfg.keymap.get("mute") == "mute":
            cfg.keymap["mute"] = "voice_ptt"
            changed.append("静音键 → 按住说话（voice_ptt）")

        # 旧的语音默认（wechat + hold + 未自定义覆盖键）是 v1.0.2 那套
        # 「必须一直按住」；v1.0.3 起改成「按一下就开始、松手也一直听」。
        # 只有**完全没动过**这几项才升级，改过任何一个都尊重用户。
        untouched_voice = (
            cfg.input_method == "wechat"
            and cfg.voice_mode == "hold"
            and cfg.hotkey_mode == "hold"
            and not cfg.voice_hotkey
        )
        if untouched_voice:
            cfg.input_method = "wechat_hold_mode"
            cfg.voice_mode   = "toggle"
            cfg.hotkey_mode  = "tap"
            changed.append("语音键 → 按一下开始 / 再按一下结束（松手也在听）")

    if from_version < 2:
        # v1.0.11：按键改由**厂商自定义页**接管（Windows 不处理厂商页，
        # 所以遥控器按键压根不会自己变成 Windows 按键）。
        #
        # 这条变更**改了默认值的含义**，不是加个新功能那么简单：
        #   · 方向键原来是 "native"（原样直通）—— 那时候的前提是"遥控器的方向键
        #     Windows 本来就收得到"。这个前提是错的（见 NATIVE_TARGET_HINT），
        #     留着它 = 上下左右永远没反应。必须换成真正注入方向键。
        #   · youtube/netflix/power/input 原来是 ""（禁用）—— 那是因为当时按键
        #     走不通、填了也没用。现在能走了，出厂就该有动作，不该让用户自己填。
        #
        # 只搬**还停在旧默认值**上的那几项，用户自己改过的一律不动。
        # ⚠ 已知代价：用户若当初是**故意**把某个键设成禁用/直通，这里会被覆盖
        #   回默认值 —— 区分不了"没动过"和"故意设成一样的值"。
        #   比留着一堆没反应的键强，且改回来只要点一下。
        _OLD_V1_DEFAULTS = {
            "up": "native", "down": "native", "left": "native", "right": "native",
            "youtube": "", "netflix": "", "power": "", "input": "",
        }
        for btn, old_default in _OLD_V1_DEFAULTS.items():
            if cfg.keymap.get(btn) == old_default:
                new_default = DEFAULT_KEYMAP[btn]
                if new_default != old_default:
                    cfg.keymap[btn] = new_default
                    changed.append(f"按键「{CHROMECAST_BUTTONS[btn]['label']}」"
                                   f" → {MAPPING_TARGETS.get(new_default, new_default)}")

    if from_version < 3:
        # v1.0.13 定案：**「说完自动发送」默认必须是关的。**
        #
        # 这不是"加了个新功能"，而是**把默认值改回来**：曾经在未发布的
        # v1.0.13 开发版里把它设成 True，2026-09-17 武哥否掉了，理由是对的 ——
        # 说到一半去上厕所、回来想接着改，程序却已经把半截话发进真人对话里了。
        # 这个代价远大于"要自己动手发一下"。
        #
        # 所以这里强行归位。代价：万一有人在那版开发版上**故意**打开过它，
        # 也会被关掉 —— 但那版从未发布，不可能存在这种用户；
        # 而且这是个安全方向的归位（宁可少发，不可替用户错发）。
        if cfg.send_after_voice:
            cfg.send_after_voice = False
            changed.append("关闭「说完自动发送」（默认改为关：发给谁、发什么、"
                           "什么时候发，都该由你自己决定）")

    cfg.config_version = CONFIG_VERSION

    if changed:
        print(f"[CONFIG] 已自动升级旧版配置（config_version {from_version} → {CONFIG_VERSION}）：")
        for c in changed:
            print(f"         · {c}")
        print("[CONFIG] 不想要？控制台「按键映射」页或「设置 → 语音」里改回来即可。")
        try:
            cfg.save()
        except Exception as e:                      # noqa: BLE001
            print(f"[CONFIG] 迁移结果保存失败（不影响本次启动）：{e}")

    return cfg


def apply_recommended() -> "Config":
    """一键套用「呆瓜配置」——把该设的一次性全设好，一个选择都不留给用户。

    设计目标是**以傻子为设计目的**：装上、配对，按语音键就能长篇输入，
    全程不需要打开任何设置、不需要理解 ATVV / HID / 点按 / 按住 这些概念。

    这套组合的每一条都对应微信输入法**原生支持**的一种模式，程序只做转发：

      遥控语音键 → 微信「启动语音输入」左Ctrl+左Win+左Shift，点一下开始、
                   松手不断，**再按一下（或按任意键，含确认键）结束**
      遥控静音键 → 微信「按住说话」Ctrl+Win，按住说、松手结束

    两者互不干扰：一个当开关，一个当油门。

    ⚠️ 分工要说清楚：**「怎么说话」用输入法的原生能力（不重复造轮子），
    但「这段语音会话什么时候开始、什么时候结束」由本程序自己管。**
    因为只有程序知道「松开遥控器按键 ≠ 说完」—— 输入法判断不了这件事。
    所以程序里有一套自己的语音会话状态机（按下翻转、确认键结束、超时兜底）。
    """
    c = Config.load()
    c.input_method       = "wechat_hold_mode"          # 语音键 → 启动语音输入（切换式）
    c.hotkey_mode        = "tap"                       # 点一下开始 / 再点一下结束
    c.voice_hotkey       = []                          # 清掉自定义覆盖，用输入法内置组合
    c.keymap             = dict(DEFAULT_KEYMAP)        # 含 静音键 → voice_ptt
    c.voice_ptt_keys     = ["ctrl", "win"]             # 静音键 → 按住说话
    # ⚠ 「呆瓜配置」里**也不许**替用户自动发送（2026-09-17 武哥定案）。
    #   呆瓜化的是"怎么让它开始/结束听"，不是"替你决定要不要把话发给别人"。
    #   后者无论如何都得由用户按一下 —— 按键盘回车就行，手本来就在键盘上。
    c.send_after_voice   = False
    c.system_mic_enabled = True                        # 切换模式下松手后，声音靠电脑麦克风
    c.mapping_enabled    = True
    c.save()
    return c


def find_device_by_name(name: str) -> Optional[DeviceSig]:
    """Match a Bluetooth device name to a known DeviceSig."""
    nl = name.lower()
    for key, sig in DEVICES.items():
        if sig.name_patterns and any(p in nl for p in sig.name_patterns):
            return sig
    return None
