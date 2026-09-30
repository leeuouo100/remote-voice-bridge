"""
Config management — per-device settings, trigger keys, input method bindings.
Config file: ~/.config/remote-voice-bridge/config.json
"""

from __future__ import annotations
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / "remote-voice-bridge"
CONFIG_PATH = CONFIG_DIR / "config.json"

# ── 支持范围（唯一真源，2026-09-29 审查报告第六节 8）─────────────────────────
# 说清楚"我们到底验过哪些 Python"，而不是让人拿 3.12/3.13 去撞：
#   · CI（.github/workflows/ci.yml）只跑 3.11；
#   · `requirements.txt` 里 `numpy==1.24.3` 在 **3.13 上没有 wheel**（要现场编译，
#     基本装不上），3.12 也不保证；
#   · 安装版自带 PyInstaller 打的 3.11 运行时，**用户不需要 Python**。
# ⇒ 所以声明 **3.10 / 3.11**。要放宽范围，先把依赖升级 + 在 CI 里建版本矩阵，
#   两件事一起做；只改这个声明会被 `tools/check_ci_workflow.py` 拦下来
#   （它要求 ci.yml 验的那个版本必须落在下面这个区间内）。
PY_MIN = (3, 10)
PY_MAX = (3, 11)
PY_RANGE_TEXT = "3.10 / 3.11"

# 修复工具的注册表备份目录（2026-09-29 审查报告 P1-7）。
#
# ⚠ 为什么**不能**放在 %APPDATA%（原来就在那儿）：
#   修复工具是**以管理员身份**跑的，而 %APPDATA% 是当前用户可写的普通目录。
#   低权限进程（或任何能在用户会话里落地的东西）可以往里面塞一个"更新"的
#   .reg，等用户下一次点「还原」时被 UAC 提权导入 —— 一次提权放大。
#   而备份里装的是**蓝牙链路密钥**（BTHPORT\Parameters\Keys），
#   那是能解密链路的材料，不能放在谁都能写的目录里。
#
# 放 %ProgramData% 并在首次使用时把 DACL 收紧成"只有管理员/SYSTEM 可写"，
# 普通用户留只读（托盘里的「打开修复备份目录」还要能浏览）。
BACKUP_DIR = (Path(os.environ.get("ProgramData", r"C:\ProgramData"))
              / "remote-voice-bridge" / "backup")

# 版本号唯一真源：控制台「设置 → 关于」显示它，installer.iss 的 MyAppVersion 也要跟着改。
APP_VERSION = "1.0.26"

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
    # ── 「遥控器优先」（v1.0.26）────────────────────────────────────────────
    # 输入法（微信输入法等）读的是**系统默认录音设备**。用户插上一个 USB
    # 麦克风（BOYA mini）之后 Windows 会自动把默认录音设备换成它 ⇒ 输入法去
    # 听 BOYA，遥控器的声音明明写进了 `CABLE Output` 却没人听 ——
    # 现象就是「按语音键说话，一个字都出不来」。
    #
    # 用户的原话：「如果和我们这个遥控器同时存在的话，优先使用我们这个遥控器，
    # 要有这种权利。」⇒ 默认**开**：程序发现默认录音设备不是下面这只，就改回来。
    # 想反过来（开会时用耳机麦），把它设成 false 即可。
    force_default_capture: bool = True
    capture_device_name:   str  = "CABLE Output"
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

    # ── 键盘页兜底映射（⚠ 默认关，2026-09-29 起）─────────────────────
    # 有些遥控器会把按键报在 **HID 键盘页**上，于是低级键盘钩子能"看见"它们。
    # 早先为了兜住这类遥控器，程序把 `space`/`enter`/`esc`/方向键 都映射成了
    # 遥控器的确认/返回/方向键。**这是一个严重的错**：
    #
    #   ⚠ 低级键盘钩子**分不出遥控器和物理键盘**（它只看键名），所以
    #     你按物理**空格**时，程序会以为你按了遥控器的**确认键**、
    #     再替你注入一个 **Enter** —— 在微信里就是**直接把手打的消息发出去**。
    #     真机日志实证（2026-09-29 09:58:32）：
    #         🔘 HID 按键 'space'（scan=57） → 按钮「ok」→ 动作 'enter'
    #     `scan=57` 正是**物理键盘**的空格键码。
    #     同一个毛病还有：物理回车 → 一次变两次；物理 Esc / 方向键 → 每个多按一下。
    #
    #   而**遥控器根本不需要这张表**：本机遥控器的按键走的是
    #     · `frida_hid.RemoteHidTap`（v1.0.20：注入 WUDFHost 抄 IOCTL 报告）
    #     · `remote_hid.RemoteHidButtons`（厂商页 0x2A4D）
    #   两条路都**直接给出 button_id**，与这张表无关。
    #
    # 所以默认关。只有当你手上那台遥控器**确实**把按键报在键盘页、
    # 而上面两条路都拿不到时，才把它打开 —— 代价是打字时这些键会被当成遥控器。
    keyboard_page_keys: bool = False

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

    # 注入蓝牙驱动宿主（WUDFHost.exe）读 HID 报告（v1.0.20 起，**真机上唯一
    # 能拿到遥控器按键的那一路**）。
    #
    # 为什么需要：遥控器的按键报告在 WUDFHost 内部就被 UMDF 驱动消费掉了，
    # 用户态 HID 接口 / Raw Input / 键盘钩子**全都看不到**（`hid_vendor_keys`
    # 那条自开厂商页的路真机实测一直是 0 条）。只有用 Frida 注入 WUDFHost、
    # 在它读 GATT 特征的那次 IOCTL 输出缓冲区上抄一份，才拿得到按键。
    #
    # 什么时候关：① 公司电脑的 EDR 拦注入（日志会说明），关掉退回纯键盘钩子；
    #            ② 不想装 frida（约 130MB）—— 关掉后语音功能完全不受影响，
    #               只是除语音键外的按键不生效。
    hid_frida_tap:      bool  = True

    # 兼容款兜底：VID/PID 对不上时，允许注入「本机第一台 BLE HID 设备」的驱动宿主。
    #
    # ⚠ 默认**关**（2026-09-29 审查报告 P1-8）。为什么：
    #   同一个 WUDFHost.exe 里可能还服务**别的**蓝牙键鼠，"猜第一项"等于
    #   可能去抄/改写别人的报告 —— 而按键旁路是会**原地改写**输出缓冲区的。
    #   精确匹配（节点名里的 VID/PID + 远端 MAC）能对上的话，这条路根本用不着。
    #
    # 什么时候打开：遥控器换了型号 / 换了牌子，日志里出现
    #   「没找到 VID/PID = 18D1/9450 的 BLE HID 驱动宿主」
    #   而你想试试它能不能用。打开后请核对日志里的节点名是不是你那台设备。
    hid_frida_any_hid:  bool  = False

    # ── 日志（2026-09-29 审查报告 P2-6）───────────────────────────────
    # 日志里到处是蓝牙地址（本机适配器地址、远端 MAC），而用户排错时**第一件事
    # 就是把日志发出来**。默认把地址中间几位打掉、只留前 2 / 后 2 字节 ——
    # 同机只有一根蓝牙棒、一只遥控器，认得出是哪个设备就够了。
    #
    # 为什么给开关而不是写死：真机排查偶发链路问题时，偶尔需要**完整**地址去
    # 对注册表/事件日志；写死就没法查了。要贴给别人的日志请保持默认（开）。
    log_redact:         bool  = True
    # `raw=02 42 00` 这类原始 HID 报告字节**默认不记**。
    #
    # 为什么：① 只对开发有用，长期记在盘上没意义；② 它能反推"用户按了什么"，
    # 属于不该默默留在用户目录里的东西。要看的时候把 config.json 里这个字段
    # 改成 true 再重启（属"高级项"，UI 上不铺开关）。
    log_raw_hid:        bool  = False

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
        with _CONFIG_LOCK:
            return cls._load_locked()

    @classmethod
    def _load_locked(cls) -> "Config":
        data = _read_raw()
        if data is None:
            return cls()
        try:
            # 旧版写下的 config.json 没有这个字段 → 0，据此判断要不要迁移。
            # ⚠ 必须读**原始 data**，不能用 defaults：defaults 里它已经是当前版本了。
            saved_version = int(data.get("config_version") or 0)
            defaults = cls().to_dict()
            # ⚠⚠ 未知字段**必须先摘掉**，不能直接喂给 dataclass 构造函数 ——
            #   那会抛 `TypeError: unexpected keyword argument`，被下面的
            #   `except` 抓住 ⇒ **整份配置回退成默认值**（用户的设置一次全没）。
            #   用户手改过 config.json、或者降级装回旧版，都会留下未知字段。
            known = set(defaults)
            unknown = sorted(k for k in data if k not in known)
            if unknown:
                print("[CONFIG] ⚠ config.json 里有不认识的字段，已忽略（其余设置照常生效）："
                      + "、".join(unknown))
                data = {k: v for k, v in data.items() if k in known}
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

    @classmethod
    def update(cls, mutator) -> "Config":
        """在**同一把锁**里完成「读 → 改 → 写」，返回改完的配置。

        为什么必须有它：控制台多个请求各自 `load() → 改字段 → save()` 时，
        两个请求可能都基于**同一份旧快照**改，后写的把先写的**整段覆盖**掉
        （典型症状："我在设置页改了 A，另一个请求同时改了 B，结果 A 没了"）。
        走这个入口，读改写是串行的。

        `mutator(cfg)` 直接在 cfg 上改。返回 `False` 表示"**没有变化**"，
        此时**不写盘**（拖动增益滑块这种高频操作不该反复落盘）；
        抛异常则不写盘。
        """
        with _CONFIG_LOCK:
            cfg = cls._load_locked()
            if mutator(cfg) is False:
                return cfg
            cfg.save()
            return cfg

    def save(self) -> None:
        """**原子写**：临时文件 → flush → fsync → `os.replace`。

        为什么不能直接 `write_text()`：那是"截断 + 写入"，中间有一个窗口期，
        并发的读者会拿到**半截 JSON**（见 `_CONFIG_LOCK` 那段注释）。
        `os.replace` 在同一个文件系统内是原子的 —— 读者要么看到旧内容、
        要么看到新内容，**永远不会看到半截**。
        """
        global _LAST_GOOD_RAW
        with _CONFIG_LOCK:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR),
                                       prefix=".config-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                # ⚠ 必须重试：Windows 上目标文件被别的线程/进程短暂打开时，
                #   `os.replace` 会抛 PermissionError（见 `_retry_io` 的说明）。
                _retry_io(os.replace, tmp, CONFIG_PATH)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            # 磁盘上已经是这份内容了 → 同步"最近有效"快照，
            # 免得下一次 load 因别的原因解析失败而回退到**更旧**的配置。
            _LAST_GOOD_RAW = self.to_dict()


# ── 配置读改写的并发保护与原子写（2026-09-29 审查报告 P1-5）────────────────────
# 控制台是 `ThreadingHTTPServer`，多个请求会**并发**走
# `Config.load() → 改字段 → cfg.save()`。原先既没有锁、`save()` 又是
# `write_text()` 直接截断写入，于是：
#   · 两个请求同时保存 → 后写的覆盖先写的（**丢配置**）；
#   · 一个请求正在写、另一个在读 → 读到**半截 JSON** → 解析失败 →
#     走 `except` 直接 `return cls()` ⇒ **整份回退成默认值**，
#     而调用方紧接着一个 `save()` 就把默认值**写回磁盘**，用户设置一次全没。
#
# 所以这里做四件事：进程内可重入锁 + 临时文件原子替换 +
# 解析失败保留最近一份有效配置 + 未知字段只警告不回退。
_CONFIG_LOCK = threading.RLock()

# 最近一次**成功解析**出来的原始 dict（不是合并后的 Config）。
# 磁盘文件被写坏 / 读到半截时用它兜底，绝不退回"全默认"。
_LAST_GOOD_RAW: Optional[dict] = None


def _retry_io(fn, *args, attempts: int = 25, delay: float = 0.008, **kwargs):
    """带退避重试的文件操作 —— Windows 上的**短暂**占用不是错误。

    ⚠ 真机实测（2026-09-29，闸门 `check_config_atomic.py` B 组）：Windows 上
    `os.replace` 与并发读会**抢文件句柄**，抛
    `PermissionError [WinError 5] 拒绝访问`。触发场景全是真的：
    用户开着 config.json 用记事本看、杀毒/备份软件扫描、同时开了两个程序实例。
    而读方一读完就释放 —— 占用是**毫秒级**的。所以这里退避重试，
    **不能把异常甩给调用方**：对 `save()` 来说，甩出去就等于
    「用户点了保存，其实没保存上」，而界面上什么提示都没有。
    """
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (1 + i * 0.4))



def _read_raw() -> Optional[dict]:
    """读 config.json 的原始 dict。

    解析失败时**返回最近一次成功的快照**（而不是 None），这样调用方不会
    退回默认值、更不会把默认值写回磁盘。真的从来没有成功过才返回 None。
    """
    global _LAST_GOOD_RAW
    try:
        # ⚠ 读也要重试：正在 `save()` 的线程可能刚把目标文件锁进替换过程，
        #   此刻裸读会抛 PermissionError —— 那是**短暂**的，不是"文件坏了"。
        raw = json.loads(_retry_io(CONFIG_PATH.read_text, encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:                                  # noqa: BLE001
        if _LAST_GOOD_RAW is not None:
            print(f"[CONFIG] ⚠ config.json 解析失败（{e}）—— 已回退到**最近一次有效的"
                  f"配置**，而不是默认值（避免把用户的设置整份清掉）")
            return _LAST_GOOD_RAW
        print(f"[CONFIG] ⚠ config.json 解析失败（{e}）—— 没有可用的历史快照，"
              f"本次按默认值运行（**不会**把默认值写回磁盘）")
        return None
    if not isinstance(raw, dict):
        print("[CONFIG] ⚠ config.json 顶层不是对象，已忽略这份内容")
        return _LAST_GOOD_RAW
    _LAST_GOOD_RAW = raw
    return raw


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

    # ⚠ 只要迁移**跑过**就必须落盘，哪怕 `changed` 是空的（审查报告 P2）。
    #   原来这句 `save()` 写在 `if changed:` **里面** ⇒ 那次迁移恰好没改任何
    #   字段时（版本号跳了、但默认值没变），新版本号就**永远写不回文件**，
    #   于是**每一次** load 都会再走一遍迁移。
    #   今天只是白跑一遍（`_migrate` 目前幂等），但任何"不幂等"的未来迁移
    #   都会变成「每次启动都改一次用户配置」—— 那是最难查的一类 bug。
    try:
        cfg.save()
    except Exception as e:                          # noqa: BLE001
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
