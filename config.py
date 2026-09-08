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
        "macos_keys":    ["option", "command"],   # Option+Command
        "windows_keys":  ["alt", "shift", "m"],   # Alt+Shift+M
        "bundle_macos":  "com.tencent.xinshurufa",
        "desc": "微信输入法（默认）",
    },
    "doubao": {
        "macos_keys":    ["option"],              # Option (单键)
        "windows_keys":  ["ctrl", "win"],         # Ctrl+Win
        "bundle_macos":  "com.bytedance.inputmethod.doubaoime",
        "desc": "豆包输入法",
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
    # True = 拦截已映射的键。注意遥控器走 HID，与物理键盘无法区分，
    # 开启后物理键盘的 Enter/Esc/方向键也会被吞掉，故默认关闭。
    suppress_keys:    bool      = False

    def trigger_keys_windows(self) -> list[str]:
        """Return the Windows hotkey list for the configured input method."""
        im = INPUT_METHODS.get(self.input_method, INPUT_METHODS["wechat"])
        if self.input_method == "custom":
            return self.custom_keys if self.custom_keys else ["alt", "shift", "m"]
        return im["windows_keys"]

    def trigger_keys_macos(self) -> list[str]:
        im = INPUT_METHODS.get(self.input_method, INPUT_METHODS["wechat"])
        if self.input_method == "custom":
            return self.custom_keys if self.custom_keys else ["option", "command"]
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
