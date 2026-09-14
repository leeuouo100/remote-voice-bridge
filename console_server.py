"""
console_server.py — 控制台 Web UI 的本地服务。

为什么控制台改成 Web 而不是 tkinter
-----------------------------------
要做出 vRemoter 那种精致度（暗色卡片、电平表、遥控器示意图、细粒度排版），
tkinter 的控件体系根本表达不出来 —— 硬做的话只能是"一堆灰按钮"。
本地起一个只监听 127.0.0.1 的小 HTTP 服务、用浏览器渲染，就能拿到完整的
CSS/HTML 能力，而且**零新增依赖**（只用标准库 http.server）。

安全边界
--------
· 只绑定 `127.0.0.1`，不监听外网；
· 只提供读配置/改配置/打开文件这几类接口，不接受任意路径；
· `/api/open` 的白名单写死（日志 / 配置目录 / 项目主页），不做通用 shell 打开。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import (
    APP_VERSION, CHROMECAST_BUTTONS, CONFIG_DIR, DEFAULT_KEYMAP, INPUT_METHODS,
    MAPPING_TARGETS, NATIVE_TARGET_HINT, VOICE_HOTKEY_PRESETS, Config,
    hotkey_label, ordered_buttons,
)
import state

logger = logging.getLogger("rvb.console")

REPO_URL = "https://github.com/leeuouo100/remote-voice-bridge"
LOG_FILE = CONFIG_DIR / "bridge.log"

# 状态清单里体现的"权限"项 —— Windows 上没有 macOS 那套辅助功能/输入监控授权，
# 这里换成 Windows 上真正会影响功能的检查项，不照搬名字骗人。
_TARGET_GROUPS = [
    {"label": "常用", "ids": [
        "", "native", "voice", "up", "down", "left", "right",
        "enter", "escape", "backspace", "delete", "tab", "space",
        "pageup", "pagedown", "home", "end",
    ]},
    {"label": "系统", "ids": [
        "volumeup", "volumedown", "mute", "playpause",
        "win", "win+d", "win+s", "win+e",
    ]},
    {"label": "组合快捷键", "ids": [
        "alt+tab", "win+shift+s", "ctrl+c", "ctrl+v", "ctrl+z", "ctrl+a",
        "ctrl+shift+esc",
    ]},
]


# ── 资源定位 ──────────────────────────────────────────────────────────────────
def ui_dir() -> Path:
    """ui/ 目录位置。打包成 exe 后 PyInstaller 会解压到 _MEIPASS。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "ui"
        if p.is_dir():
            return p
    return Path(__file__).resolve().parent / "ui"


_MIME = {".html": "text/html; charset=utf-8",
         ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8",
         ".svg": "image/svg+xml",
         ".png": "image/png",
         ".ico": "image/x-icon"}


# ── 录制状态 ──────────────────────────────────────────────────────────────────
class Recorder:
    """录制任意组合键。

    状态放在服务端而不是浏览器里：浏览器侧没法拿到"全局键盘按下"——
    用户是在别的窗口里按键，页面根本没焦点。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = "idle"      # idle | recording | ok | cancel | timeout | error
        self._value = ""          # 录到的组合键（归一化后）
        self._message = ""        # 给用户看的结果说明（超时 / 发不出去的原因）
        self._held: list[str] = []
        self._cancel = threading.Event()
        self._until = 0.0

    MAX_SECONDS = 10.0

    def start(self) -> None:
        self.cancel()
        with self._lock:
            self._state = "recording"
            self._value = ""
            self._message = ""
            self._held = []
        self._cancel.clear()
        self._until = time.time() + self.MAX_SECONDS
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self._state, "value": self._value,
                    "held": list(self._held), "message": self._message}

    def _reset(self, status: str, value: str = "", message: str = "") -> None:
        with self._lock:
            self._state = status
            if value:
                self._value = value
            if message:
                self._message = message
            elif status == "timeout":
                self._message = "超时未检测到按键"
            self._held = []

    def _worker(self) -> None:
        try:
            import keyboard as kb
        except Exception as e:  # noqa: BLE001
            self._reset("error", str(e))
            return

        from keys import combo_bad_parts, is_modifier, modifier_family, normalize_combo_part

        # held 里存的是**归一化后**的键名。
        #
        # 为什么必须先归一化：keyboard 库对同一只键会报出多个名字 ——
        # 右 Alt 可能报 `alt gr`（AltGr），也可能报 `right menu`（VK 0xA5），
        # 有时两个都报。拿原始名去比集合，`right menu` 会被当成"主键已按下"，
        # 于是组合键在 Alt 落下的那一刻就结束了，用户根本录不到后面的键。
        held: list[str] = []
        peak: list[str] = []          # 见过的最长组合，用于"纯修饰键"收尾

        def finish(combo: str) -> None:
            # 解析不出来的键名要当场报错。写进配置再静默失效，
            # 用户只会看到「设了没用」，却完全不知道错在哪。
            bad = combo_bad_parts(combo)
            if bad:
                self._reset("error", message=f"这个键发不出去：{'、'.join(bad)}")
            else:
                self._reset("ok", value=combo)

        try:
            while time.time() < self._until and not self._cancel.is_set():
                e = kb.read_event(suppress=False)
                name = (getattr(e, "name", "") or "").lower().strip()
                if not name:
                    continue
                norm = normalize_combo_part(name)

                if e.event_type == "up":
                    if norm in held:
                        held.remove(norm)
                    with self._lock:
                        self._held = list(held)
                    # 修饰键全松开 = 用户按完了。
                    # 没有这一步，「Ctrl + Win」这种**只有修饰键**的组合永远录不出来：
                    # 它等不到"非修饰键"来收尾，只能一直超时。
                    # （微信输入法的默认语音键恰好就是这种。）
                    if not held and peak:
                        finish("+".join(peak))
                        return
                    continue

                if norm == "esc":
                    self._reset("cancel")
                    return
                if is_modifier(norm):
                    # 同族只留一个：`alt` 和 `ralt` 是同一只物理键的两种叫法，
                    # 都记下来会下发两次 Alt 按下。
                    fam = modifier_family(norm)
                    if not any(modifier_family(h) == fam for h in held):
                        held.append(norm)
                    if len(held) > len(peak):
                        peak = list(held)
                    with self._lock:
                        self._held = list(held)
                    continue

                finish("+".join(held + [norm]))
                return
        except Exception as e:  # noqa: BLE001
            self._reset("error", message=str(e))
            return
        if self._cancel.is_set():
            self._reset("cancel")
        else:
            self._reset("timeout")


# ── 数据组装 ──────────────────────────────────────────────────────────────────
def _current_preset(keys: list[str]) -> str:
    """当前生效的键对应哪个预设档。

    用等价判断而不是字符串相等：录制器存下来的是 `lctrl+lwin`（keyboard 库
    给的名字），预设表里写的是 `ctrl+win` —— 两者是同一只键，直接比字符串
    会让下拉框显示成「自定义…」，用户以为自己录的东西没被认出来。
    """
    from keys import combo_equivalent
    for p in VOICE_HOTKEY_PRESETS:
        if p["keys"] and combo_equivalent(p["keys"], keys):
            return p["id"]
    return "custom"


def _resolve_out_device(cfg: Config, out_list: list[str]) -> str:
    """把配置里的音频输出名对上设备列表里的真实名字。

    配置里默认存的是 "CABLE Input" 这种**前缀名**（sounddevice 靠子串匹配），
    但设备列表里是 "CABLE Input (VB-Audio Virtual Cable)" 这种全名。
    不做这层解析的话，<select> 的 value 匹配不上任何 option，
    下拉框会显示成空白 —— 看起来像"没设置"，用户会以为坏了。
    """
    want = (cfg.audio_output or "").strip().lower()
    if not want:
        return ""
    if any(n.lower() == want for n in out_list):
        return next(n for n in out_list if n.lower() == want)
    for n in out_list:                      # 子串命中优先取最短的，避免撞上 16ch 之类的长名
        if want in n.lower():
            return n
    return cfg.audio_output


def _resolve_in_device(cfg: Config, in_list: list[str]) -> tuple[str, bool]:
    """返回（实际使用的输入设备名, 是否可用）。

    注意：这里判断的是"系统里到底有没有这只麦克风"，而不是"桥接程序当前是否
    已经把它打开了"。桥接没连遥控器时麦克风本来就是关的，用后者会让检查项
    一直显示红叉，属于误报。
    """
    try:
        import sounddevice as sd
    except Exception:  # noqa: BLE001
        # 音频后端缺失/装坏时不能把整个 /api/state 带崩 —— 检查项退化成
        # "列出来的设备里找一找"，其余功能照常（浏览器的 500 对用户毫无信息量）。
        sd = None

    want = (cfg.system_mic_device or "").strip()
    if not want:
        if sd is not None:
            try:
                dev = sd.default.device
                idx = dev[0] if isinstance(dev, (list, tuple)) else dev
                if isinstance(idx, int) and idx >= 0:
                    name = (sd.query_devices(idx).get("name") or "").strip()
                    if name:
                        return name, True
            except Exception:  # noqa: BLE001
                pass
        return (in_list[0] if in_list else "未找到输入设备"), bool(in_list)

    low = want.lower()
    for n in in_list:
        if low in n.lower():
            return n, True
    return want, False


def _checklist(cfg: Config, s) -> list[dict]:
    from mixer import list_input_devices

    out_dev_ok = False
    try:
        import sounddevice as sd
        for d in sd.query_devices():
            if (cfg.audio_output or "").lower() in (d.get("name") or "").lower() \
                    and d.get("max_output_channels", 0) > 0:
                out_dev_ok = True
                break
    except Exception:  # noqa: BLE001
        pass

    in_list = list_input_devices()
    mic_name, mic_ok = _resolve_in_device(cfg, in_list)

    keys = cfg.trigger_keys_windows()
    return [
        {"name": "遥控器连接",
         "value": ("BLE · HID 已就绪" if s.connected else "未连接（按遥控器任意键唤醒）"),
         "ok": bool(s.connected), "mono": False},
        {"name": "音频驱动",
         "value": (f"{cfg.audio_output} 可用" if out_dev_ok else f"未找到 {cfg.audio_output}"),
         "ok": out_dev_ok, "mono": False},
        {"name": "电脑麦克风",
         "value": mic_name + ("（系统默认）" if not (cfg.system_mic_device or "").strip() else ""),
         "ok": mic_ok, "mono": False},
        {"name": "语音触发键",
         "value": f"{hotkey_label(keys) or '未设置'}（{'按住说话' if cfg.hotkey_mode == 'hold' else '按一下切换'}）",
         "ok": bool(keys), "mono": False},
        {"name": "遥控器按键映射",
         "value": "已启用" if getattr(cfg, "mapping_enabled", True) else "已停用",
         "ok": bool(getattr(cfg, "mapping_enabled", True)), "mono": False},
        {"name": "键盘接管",
         "value": "已拦截原生按键" if cfg.suppress_keys else "原生按键直接生效（不拦截）",
         "ok": True, "mono": False},
        {"name": "开机自动启动",
         "value": "已开启" if _is_autostart() else "未开启",
         "ok": True, "mono": False},
    ]


def _is_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            v, _ = winreg.QueryValueEx(k, "RemoteVoiceBridge")
            return bool(v)
    except Exception:  # noqa: BLE001
        return False


def _set_autostart(enabled: bool) -> None:
    import subprocess as sp
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            if getattr(sys, "frozen", False):
                cmd = sp.list2cmdline([sys.executable])
            else:
                cmd = sp.list2cmdline([sys.executable,
                                       os.path.abspath(Path(__file__).parent / "tray_app.py")])
            winreg.SetValueEx(k, "RemoteVoiceBridge", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "RemoteVoiceBridge")
            except OSError:
                pass


def _supported_devices(cfg: Config, s) -> list[dict]:
    from config import DEVICES
    items = []
    for key, sig in DEVICES.items():
        active = (cfg.device == key)
        # 只有当前选中的型号才可能是"已连接"，另一个型号必然是未连接
        items.append({
            "key": key,
            "name": {"chromecast": "Chromecast Voice Remote", "x6": "X6 Remote"}.get(key, key),
            "signature": f"{sig.vid:04X} · {sig.pid:04X}",
            "active": active,
            "connected": bool(s.connected and active),
        })
    return items


def build_live() -> dict:
    """只含"每次刷新都在变"的部分 —— 供高频轮询（波形要画得流畅）。

    刻意**不碰**声卡枚举、配置读取、按键表这些：它们要么开销大，要么结果稳定，
    放进 150ms 一次的轮询里纯属浪费。完整快照见 build_state()。
    """
    s = state.get()
    return {
        "status": {
            "connected": bool(s.connected),
            "streaming": bool(s.streaming),
            "device": s.device or "",
            "running": True,
        },
        "levels": {
            "sys": round(s.sys_level_db, 1),
            "remote": round(s.remote_level_db, 1),
            "mix": round(s.mix_level_db, 1),
        },
        # 三路波形（各 128 点，int16 量纲），前端归一化后画曲线。
        # 光有电平条看不出「声音长什么样」—— 波形才能一眼认出是人在说话还是底噪。
        "waves": state.waves_payload(),
        "mix": dict(state.mix_params()),
        "diagnostics": {
            "frames": s.audio_frames,
            "peak": s.audio_peak,
            "sample_rate": s.sample_rate,
            "frame_bytes": s.frame_bytes,
            "last_audio_ago": (round(time.time() - s.audio_last_at, 1)
                               if s.audio_last_at else None),
        },
    }


# 声卡列表缓存：枚举一次要遍历 MME/DirectSound/WASAPI/WDM-KS 四套驱动的全部设备，
# 是 build_state() 里最贵的一步。设备热插拔后用"刷新"按钮强制重取。
_DEV_CACHE: dict = {"at": 0.0, "in": [], "out": []}
_DEV_TTL = 8.0


def _device_lists(force: bool = False) -> tuple[list[str], list[str]]:
    from mixer import list_input_devices, list_output_devices
    now = time.time()
    if force or now - _DEV_CACHE["at"] > _DEV_TTL or not _DEV_CACHE["out"]:
        _DEV_CACHE["in"] = list_input_devices()
        _DEV_CACHE["out"] = list_output_devices()
        _DEV_CACHE["at"] = now
    return _DEV_CACHE["in"], _DEV_CACHE["out"]


def build_state(force_devices: bool = False) -> dict:
    cfg = Config.load()
    s = state.get()
    keys = cfg.trigger_keys_windows()

    in_list, out_list = _device_lists(force_devices)

    buttons = []
    for bid, meta in ordered_buttons():
        val = str(cfg.keymap.get(bid, "") or "")
        buttons.append({
            "id": bid,
            "label": meta["label"],
            "voice": meta["usage"] == "voice",
            "value": val,
            "value_label": MAPPING_TARGETS.get(val, f"自定义：{val}" if val else "禁用"),
        })

    return {
        **build_live(),
        "version": APP_VERSION,
        "config_dir": str(CONFIG_DIR),
        "repo": REPO_URL,
        "levels": {
            "sys": round(s.sys_level_db, 1),
            "remote": round(s.remote_level_db, 1),
            "mix": round(s.mix_level_db, 1),
        },
        # 三路波形（各 128 点，int16 量纲），前端按满量程归一化后画曲线。
        # 光有电平条看不出"声音长什么样"——波形才能一眼认出是人在说话还是底噪。
        "waves": state.waves_payload(),
        "config": {
            "device": cfg.device,
            "gain": cfg.gain,
            "audio_output": cfg.audio_output,
            "system_mic_device": cfg.system_mic_device,
            "system_mic_gain": cfg.system_mic_gain,
            "system_mic_enabled": cfg.system_mic_enabled,
            "remote_mic_enabled": cfg.remote_mic_enabled,
            "hotkey_mode": cfg.hotkey_mode,
            "suppress_keys": cfg.suppress_keys,
            "input_method": cfg.input_method,
            "mapping_enabled": bool(getattr(cfg, "mapping_enabled", True)),
        },
        "devices": {
            "system_mic": s.sys_mic_name or "",
            "input_list": in_list,
            "output": cfg.audio_output,
            "output_list": out_list,
            "output_resolved": _resolve_out_device(cfg, out_list),
        },
        "hotkey": {
            "keys": keys,
            "label": hotkey_label(keys),
            "preset": _current_preset(keys),
            "mode": cfg.hotkey_mode,
        },
        "hotkey_presets": VOICE_HOTKEY_PRESETS,
        "methods": [{"id": k, "label": v["desc"]} for k, v in INPUT_METHODS.items()],
        "buttons": buttons,
        "targets": MAPPING_TARGETS,
        "target_groups": _TARGET_GROUPS,
        "native_hint": NATIVE_TARGET_HINT,
        "supported_devices": _supported_devices(cfg, s),
        "checklist": _checklist(cfg, s),
        "autostart": _is_autostart(),
    }


# ── HTTP 处理 ─────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "RVBCONSOLE/" + APP_VERSION

    # 静默默认的访问日志（控制台每 260ms 轮询一次，刷屏没有意义）
    def log_message(self, fmt, *args):  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ── 工具 ──
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    # ── GET ──
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/state":
                # ?devices=1 强制重新枚举声卡（设置页的「刷新」按钮用）
                q = parse_qs(urlparse(self.path).query)
                return self._json(build_state(force_devices=bool(q.get("devices"))))
            if path == "/api/live":
                # 轻量接口：只回状态 + 三路电平/波形，供前端高频轮询画波形。
                return self._json(build_live())
            if path == "/api/log":
                return self._json(self._read_log())
            if path == "/api/record/poll":
                return self._json(RECORDER.snapshot())
            return self._static(path)
        except Exception as e:  # noqa: BLE001
            logger.exception("GET %s 失败", path)
            return self._err(str(e), 500)

    def _read_log(self) -> dict:
        if not LOG_FILE.exists():
            return {"text": "", "size": 0}
        size = LOG_FILE.stat().st_size
        limit = 200_000
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()
            text = f.read()
        return {"text": text, "size": size}

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        # 只允许 ui/ 下的普通文件，禁止 ../ 逃逸
        target = (ui_dir() / rel).resolve()
        try:
            target.relative_to(ui_dir().resolve())
        except ValueError:
            return self._err("forbidden", 403)
        if not target.is_file():
            return self._err("not found", 404)
        self._send(200, target.read_bytes(), _MIME.get(target.suffix, "application/octet-stream"))

    # ── POST ──
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/config":
                return self._json(self._patch_config(body))
            if path == "/api/mix":
                return self._json({"mix": state.set_mix(**body)})
            if path == "/api/mapping":
                return self._json(self._patch_mapping(body))
            if path == "/api/mapping/reset":
                cfg = Config.load()
                cfg.keymap = dict(DEFAULT_KEYMAP)
                cfg.save()
                return self._json({"ok": True})
            if path == "/api/hotkey":
                from keys import combo_bad_parts
                cfg = Config.load()
                keys_in = [str(k).strip() for k in (body.get("keys") or []) if str(k).strip()]
                # 服务端把住最后一道关：解析不出来的键名写进配置只会静默失效，
                # 用户看到的就是「设了没用」。宁可直接报错。
                bad = [p for p in keys_in if combo_bad_parts(p)]
                if bad:
                    return self._err(f"不认识这些键名：{'、'.join(bad)}")
                cfg.voice_hotkey = keys_in
                cfg.save()
                return self._json({"ok": True, "label": hotkey_label(cfg.trigger_keys_windows())})
            if path == "/api/record/start":
                RECORDER.start()
                return self._json({"ok": True})
            if path == "/api/record/cancel":
                RECORDER.cancel()
                return self._json({"ok": True})
            if path == "/api/test_hotkey":
                return self._json(self._test_hotkey())
            if path == "/api/autostart":
                _set_autostart(bool(body.get("enabled")))
                return self._json({"autostart": _is_autostart()})
            if path == "/api/devices":
                st = build_state()
                return self._json(st["devices"])
            if path == "/api/reconnect":
                try:
                    import main
                    main.request_reconnect()
                except Exception as e:  # noqa: BLE001
                    return self._err(f"重连请求失败：{e}")
                return self._json({"ok": True})
            if path == "/api/open":
                return self._json(self._open_what(body.get("what")))
            if path == "/api/log/clear":
                LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOG_FILE.write_text("", encoding="utf-8")
                return self._json({"ok": True})
            if path == "/api/bye":
                return self._json({"ok": True})
            return self._err("not found", 404)
        except Exception as e:  # noqa: BLE001
            logger.exception("POST %s 失败", path)
            return self._err(str(e), 500)

    def _patch_config(self, body: dict) -> dict:
        cfg = Config.load()
        allowed = {
            "gain": float, "audio_output": str, "system_mic_device": str,
            "system_mic_gain": float, "hotkey_mode": str, "suppress_keys": bool,
            "input_method": str, "device": str, "voice_mode": str,
            "mapping_enabled": bool,
        }
        for k, v in body.items():
            if k not in allowed or not hasattr(cfg, k):
                continue
            try:
                setattr(cfg, k, allowed[k](v))
            except (TypeError, ValueError):
                continue
        cfg.save()
        # 增益是"热"参数：面板一改立即生效，不用重连。
        # 注意要同时更新两处 —— state.set_gain 是给诊断/显示用的，
        # state.set_mix(remote_gain=...) 才是混音回调真正读的那个值；
        # 只改前一处的话滑块看着动了、音量没变。
        state.set_gain(cfg.gain)
        state.set_mix(remote_gain=cfg.gain,
                      sys_gain=cfg.system_mic_gain,
                      sys_enabled=cfg.system_mic_enabled,
                      remote_enabled=cfg.remote_mic_enabled)
        return {"ok": True, "config": body}

    def _patch_mapping(self, body: dict) -> dict:
        btn = str(body.get("button") or "")
        if btn not in CHROMECAST_BUTTONS:
            return {"error": f"未知按键 {btn!r}"}
        if CHROMECAST_BUTTONS[btn]["usage"] == "voice":
            return {"error": "语音键由语音通道处理，不可映射"}
        value = str(body.get("value") or "")
        cfg = Config.load()
        cfg.keymap[btn] = value
        cfg.save()
        return {"ok": True, "button": btn, "value": value}

    def _test_hotkey(self) -> dict:
        def worker():
            try:
                from keys import voice_hotkey_down, voice_hotkey_up
                voice_hotkey_down()
                time.sleep(1.0)
                voice_hotkey_up()
            except Exception as e:  # noqa: BLE001
                logger.error("测试触发键失败：%s", e)
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def _open_what(self, what: str) -> dict:
        # 白名单：不接受任意路径，避免被当成"打开任意文件"的通用接口
        if what == "log":
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if not LOG_FILE.exists():
                LOG_FILE.write_text("", encoding="utf-8")
            os.startfile(str(LOG_FILE))          # noqa: S606
            return {"ok": True}
        if what == "config":
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(CONFIG_DIR))        # noqa: S606
            return {"ok": True}
        if what == "repo":
            webbrowser.open(REPO_URL)
            return {"ok": True}
        return {"error": f"不允许打开 {what!r}"}


RECORDER = Recorder()


# ── 启停 ──────────────────────────────────────────────────────────────────────
class ConsoleServer:
    def __init__(self, port: int = 0):
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.3}, daemon=True)
        self._thread.start()
        logger.info("🖥  控制台已就绪： http://127.0.0.1:%d/", self.port)
        return self.port

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass


_SERVER: ConsoleServer | None = None
_SERVER_LOCK = threading.Lock()


def ensure_started(port: int = 0) -> ConsoleServer:
    """启动（或复用）控制台服务，返回实例。"""
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is None:
            _SERVER = ConsoleServer(port)
            _SERVER.start()
        return _SERVER


def open_console(port: int = 0, browser: bool = True) -> str:
    srv = ensure_started(port)
    url = srv.url()
    if browser:
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            logger.warning("打开浏览器失败：%s", e)
    return url
