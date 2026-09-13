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
INPUT_METHODS = {
    "wechat": {
        # ⚠ 必须与「微信输入法 → 设置 → 语音输入」里显示的键一致。
        #
        # 微信输入法的语音唤起键是「长按**右 Alt**」——注意是右侧那个 Alt
        # （AltGr），与左 Alt 不是同一个键，所以这里写 "ralt" 而不是 "alt"。
        # 它**全局生效**：微信、豆包、记事本、浏览器……任何输入框都能用。
        #
        # 别跟「微信 PC 客户端」（4.1.8+）搞混：那是按住 Ctrl+Win，
        # 但只在微信自己的窗口里生效，在豆包之类的地方按了毫无反应。
        #
        # 若你的输入法里显示的是别的组合，直接改 config.json 的 voice_hotkey，
        # 不必改这里。
        "macos_keys":    [],                      # macOS 版是长按 Fn，系统级合成受限，见 custom_keys
        "windows_keys":  ["ralt"],
        "bundle_macos":  "com.tencent.xinshurufa",
        "desc": "微信输入法（默认，长按右 Alt）",
    },
    "doubao": {
        # 豆包输入法设置里可选「右 Alt / 右 Alt + 空格 / 左 Ctrl + Win」，
        # 默认是右 Alt，与微信输入法一致。
        "macos_keys":    [],                      # macOS 版是长按右 Option，同上
        "windows_keys":  ["ralt"],
        "bundle_macos":  "com.bytedance.inputmethod.doubaoime",
        "desc": "豆包输入法（长按右 Alt）",
    },
    "custom": {
        "macos_keys":    [],
        "windows_keys":  [],
        "desc": "自定义组合键",
    },
}


# ── Button definitions (Chromecast Voice Remote) ─────────────────────────────
CHROMECAST_BUTTONS = {
    "up":      {"usage": 0x03, "label": "方向上"},
    "down":    {"usage": 0x04, "label": "方向下"},
    "left":    {"usage": 0x05, "label": "方向左"},
    "right":   {"usage": 0x06, "label": "方向右"},
    "ok":      {"usage": 0x07, "label": "确认"},
    "back":    {"usage": 0x0B, "label": "返回"},
    "home":    {"usage": 0x0A, "label": "Home"},
    "youtube": {"usage": 0x0E, "label": "YouTube"},
    "netflix": {"usage": 0x0F, "label": "Netflix"},
    "power":   {"usage": 0x01, "label": "电源"},
    "input":   {"usage": 0x11, "label": "信源"},
    "mute":    {"usage": 0x08, "label": "静音"},
    "vol_up":  {"usage": 0x0C, "label": "音量＋"},
    "vol_down":{"usage": 0x0D, "label": "音量－"},
    "voice":   {"usage": "voice", "label": "语音"},
}

# Default keymap
DEFAULT_KEYMAP = {
    "up":      "up",
    "down":    "down",
    "left":    "left",
    "right":   "right",
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

    def trigger_keys_windows(self) -> list[str]:
        """Return the Windows hotkey list for the configured input method."""
        if self.voice_hotkey:
            return list(self.voice_hotkey)
        im = INPUT_METHODS.get(self.input_method, INPUT_METHODS["wechat"])
        if self.input_method == "custom":
            return self.custom_keys if self.custom_keys else ["ralt"]
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
                defaults.update(data)
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
