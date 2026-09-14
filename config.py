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
APP_VERSION = "1.0.2"


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
        # 「持续输入」模式：按一次开始，再按一次（或回车）结束，不用一直按着。
        # 微信 PC 客户端用 Ctrl+Win+Shift；微信输入法面板里这条叫「启动语音输入」，
        # 显示的是 左Win+左Ctrl+左Shift —— 两家不一样，这里给的是客户端那一套。
        "macos_keys":    [],
        "windows_keys":  ["ctrl", "win", "shift"],
        "desc": "微信 · 持续输入（Ctrl+Win+Shift）",
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
    {"id": "ctrl+win+shift", "keys": ["ctrl", "win", "shift"], "label": "Ctrl + Win + Shift",
     "hint": "微信 PC 客户端 · 持续输入（按一次开始，不用一直按住）· "
             "微信输入法里那条切换键是「左Win+左Ctrl+左Shift」，和这组不一样"},
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
    "native":      "原样直通（遥控器自己的按键生效）",
    "voice":       "语音输入（长按唤起输入法）",
    "up":          "方向上  ↑",
    "down":        "方向下  ↓",
    "left":        "方向左  ←",
    "right":       "方向右  →",
    "enter":       "回车 / 确认  Enter",
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

# 「原样直通」的说明 —— 界面和文档共用同一份文案，避免两处说法不一致。
NATIVE_TARGET_HINT = (
    "遥控器走的是 HID 键盘通道，它自己的按键 Windows 本来就收得到。\n"
    "选「原样直通」＝不做任何额外动作，直接让遥控器原生按键生效（不重复、不冲突）。\n"
    "改成其它动作则会「原生键 + 映射键」一起发出 —— Windows 无法单独拦掉遥控器原生键，\n"
    "要完全接管请在「设置」页打开「拦截遥控器原生按键」（代价：物理键盘的同名键也会被吞）。"
)


# Default keymap
#
# 方向键默认「原样直通」：遥控器的方向键本来就是标准方向键，再注入一次只会变成双份。
# 确认/返回/Home/静音/音量沿用各自默认（Home 在原生的"浏览器主页"在桌面上没用，
# 所以补一个 Win+D；音量/静音是遥控器原生的媒体键）。
DEFAULT_KEYMAP = {
    "up":      "native",
    "down":    "native",
    "left":    "native",
    "right":   "native",
    "ok":      "enter",
    "back":    "escape",
    "home":    "win+d",
    "youtube": "",
    "netflix": "",
    "power":   "",
    "input":   "",
    "mute":    "mute",
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
    input_method:     str       = "wechat"      # "wechat" | "doubao" | "custom"
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
    #   "hold" = 按住（微信输入法 / 豆包输入法都是「长按说话、松开结束识别」）← 默认
    #   "tap"  = 点一下开始、再点一下结束（部分输入法是端点式）
    hotkey_mode:      str       = "hold"
    # 非空则覆盖 INPUT_METHODS 内置组合。
    # 键名支持：ralt(右Alt) / lalt / alt / ctrl / win / shift / 字母 / f1-f24 / space …
    # 例如 ["ralt"] 或 ["ctrl","win"] 或 ["ralt","space"]
    # 用 tools/test_voice_hotkey.py 可以直接试哪组键能唤起输入法。
    voice_hotkey:     list[str] = field(default_factory=list)
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
                defaults = cls().to_dict()
                # keymap 单独合并：旧版本写下的 config.json 里没有新增的按键，
                # 直接整体覆盖会让这些键变成"未配置"，表现为按键失效。
                km = dict(DEFAULT_KEYMAP)
                if isinstance(data.get("keymap"), dict):
                    km.update({k: v for k, v in data["keymap"].items() if isinstance(v, str)})
                defaults.update(data)
                defaults["keymap"] = km
                return cls(**defaults)
            except Exception as e:
                print(f"[CONFIG] Load error: {e}")
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def find_device_by_name(name: str) -> Optional[DeviceSig]:
    """Match a Bluetooth device name to a known DeviceSig."""
    nl = name.lower()
    for key, sig in DEVICES.items():
        if sig.name_patterns and any(p in nl for p in sig.name_patterns):
            return sig
    return None
