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
· `/api/open` 的白名单写死（日志 / 配置目录 / 项目主页），不做通用 shell 打开；
· **所有 `/api/*` 都要令牌**（v1.0.23 起，审查报告 P1-1），见下面 `_TOKEN` 那段。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
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
    apply_recommended, hotkey_label, ordered_buttons,
)
import state
import logsetup

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


# ── 控制台令牌（2026-09-29 审查报告 P1-1）────────────────────────────────────
# 控制台原先**完全无鉴权**：同机任意低权限进程都能直接 POST /api/config、
# /api/test_hotkey、/api/reconnect……；恶意网页只要能命中那个随机端口，也可能
# 构造出**无需预检**的请求（`text/plain` 的简单请求不触发 CORS preflight）。
# 随机端口只是把概率压低，**它不是鉴权**。
#
# 现在的做法：进程启动时生成 256 位随机令牌，用 **HttpOnly + SameSite=Strict
# 的 Cookie** 交给控制台页面（页面本身由本服务提供，所以拿得到）；`/api/*`
# 一律要令牌。为什么走 Cookie 而不是把令牌写进 URL：
#   · URL 会进 `bridge.log`（那行「🖥 控制台已就绪：http://…」就在日志里）——
#     令牌写 URL 等于**把令牌写进日志**；
#   · `HttpOnly` 让页面脚本读不到它，XSS 也偷不走（P1-2 的纵深防御）；
#   · `SameSite=Strict` 让跨站请求**根本带不上**这个 Cookie。
# 另加三道闸：精确 Host（防 DNS rebinding）、Origin 白名单（防跨站）、
# 只收 `application/json`（把简单请求挡在预检之外）+ 64 KiB 体积上限。
_TOKEN: str = ""
_TOKEN_LOCK = threading.Lock()
_TOKEN_COOKIE = "rvb_token"
_MAX_BODY = 64 * 1024
# Host 只认本机回环的三种写法
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# CSP（2026-09-29 审查报告 P1-2）：即使有值漏进 DOM，也不让它变成可执行的东西。
#   · `script-src 'self'`  —— 禁内联脚本、禁 eval；页面只有一个外链 app.js
#   · `style-src 'self'`   —— 禁内联 <style>/style=；样式全在 style.css
#   · `object-src/base-uri/form-action 'none'` —— 禁插件、禁改基地址、禁表单外发
#   · `frame-ancestors 'none'` —— 不许被别人 iframe 套住（防点击劫持）
#   · `connect-src 'self'` —— 只许 fetch 自己这一个源
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; object-src 'none'; "
        "frame-ancestors 'none'")


def console_token() -> str:
    """本进程的控制台令牌（256 位）。首次调用时生成，之后固定。"""
    global _TOKEN
    with _TOKEN_LOCK:
        if not _TOKEN:
            _TOKEN = secrets.token_urlsafe(32)
        return _TOKEN


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
    # 三级匹配 + 同级别取最短名。
    #
    # ⚠ 原来注释写着"子串命中优先取最短的"，但代码其实是 `return` 第一个命中的
    #   —— 注释和实现不一致，而设备顺序由驱动枚举决定，不保证谁在前。
    #   装了 VB-CABLE + CABLE 2 时，`CABLE Input` 会先撞上 `CABLE 2 Input`，
    #   下拉框显示的和实际用的就不是一只声卡了。
    #   取最短名则天然偏向"最贴近用户填的那个词"的那个设备。
    exact = [n for n in out_list if n.lower() == want]
    if exact:
        return min(exact, key=len)
    prefix = [n for n in out_list if n.lower().startswith(want)]
    if prefix:
        return min(prefix, key=len)
    sub = [n for n in out_list if want in n.lower()]
    if sub:
        return min(sub, key=len)
    return cfg.audio_output


def _resolve_in_device(cfg: Config) -> tuple[str, bool, str]:
    """返回（电脑麦克风会用的设备名, 是否可用, 原因码）。

    注意：这里判断的是"程序到底会用哪只麦克风"，与"当前是否已经打开"无关 ——
    桥接没连遥控器时麦克风本来就是关的，用后者会让检查项一直红叉，属于误报。

    ⚠⚠ 这里**必须**和运行时走同一个函数（`mixer.resolve_input_device`）。

       历史上这里是**独立实现**的，而且用 `sd.default.device[0]` 取默认设备 ——
       它的类型是 `_InputOutputPair` 而**不是 list/tuple**，于是
       `isinstance(dev, (list, tuple))` 判 False → 整段静默掉到 `in_list[0]`，
       报出 `Microsoft Sound Mapper - Input` 并打了**绿勾**；
       而运行时实际打开的是 `CABLE Output`（＝本程序自己的输出，闭环）。

       用户看到的就是「检查项全绿，语音却一个字都识别不出来」。
       2026-09-29 真机实测：系统那一路比遥控器**响 24 dB**。

       同一份判断只能有一个出处 —— 别再在这里写第二份。
    """
    from mixer import resolve_input_device, INPUT_OK

    idx, label, reason = resolve_input_device(getattr(cfg, "system_mic_device", "") or "")
    return (label or "未找到输入设备"), (reason == INPUT_OK), reason


def _checklist(cfg: Config, s) -> list[dict]:
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

    mic_name, mic_ok, mic_reason = _resolve_in_device(cfg)
    mic_value = mic_name + ("（系统默认）" if not (cfg.system_mic_device or "").strip() else "")
    if mic_reason == "loopback":
        # 这一条以前是**绿的** —— 因为那时显示的是另一只（根本没被使用的）设备名。
        # 报红并把后果说清楚，否则用户只会看到"检查项全绿、却识别不出字"。
        mic_value = (f"⚠「{mic_name}」是本程序**自己的输出**，采回来会形成自听自的闭环"
                     f"（实测比遥控器响 24 dB、语音识别时好时坏）→ 已拒绝打开。"
                     f"请到「音频」页显式选一只真实麦克风")
    elif mic_reason == "alias":
        mic_value = (f"⚠「{mic_name}」是 Windows 的别名设备，指向「当前系统默认」，"
                     f"而默认可能就是 CABLE Output（＝本程序自己的输出）→ 已拒绝打开。"
                     f"请到「音频」页显式选一只真实麦克风")
    elif mic_reason == "missing":
        mic_value = f"⚠ 配置里的「{mic_name}」在系统里找不到 → 请到「音频」页重选"
    elif mic_reason == "none":
        mic_value = "系统里没有可用的真实麦克风（混音只保留遥控器一路）"
        mic_ok = not bool(getattr(cfg, "system_mic_enabled", True))
    elif not bool(getattr(cfg, "system_mic_enabled", True)):
        mic_value += "（未参与混音）"

    keys = cfg.trigger_keys_windows()
    return [
        {"name": "遥控器连接",
         # ⚠ 只说"蓝牙已连接"，**不要**替 HID 按键打包票。
         #   蓝牙连上 ≠ 遥控器的按键就能变成 Windows 按键 —— 这两件事在
         #   2026-09-15 的真机上正好是分开的（语音键好使、其他键全没反应）。
         #   写"BLE · HID 已就绪"会把人往错方向带，所以顺手把排查入口指出来。
         "value": ("蓝牙已连接（若按键无反应，请跑一次「遥控器诊断」）" if s.connected
                   else "未连接（按遥控器任意键唤醒）"),
         "ok": bool(s.connected), "mono": False},
        {"name": "音频驱动",
         "value": (f"{cfg.audio_output} 可用" if out_dev_ok else f"未找到 {cfg.audio_output}"),
         "ok": out_dev_ok, "mono": False},
        {"name": "电脑麦克风",
         "value": mic_value,
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


# 遥控器电平的过期门限（秒）：超过这么久没收到音频帧，就把「遥控器麦克风」
# 当成没声音。正常推流约每 20ms 一帧，0.7s 有两个数量级的余量。
_REMOTE_LEVEL_TTL = 0.7


def build_live() -> dict:
    """只含"每次刷新都在变"的部分 —— 供高频轮询（波形要画得流畅）。

    刻意**不碰**声卡枚举、配置读取、按键表这些：它们要么开销大，要么结果稳定，
    放进 150ms 一次的轮询里纯属浪费。完整快照见 build_state()。
    """
    s = state.get()
    # ── 遥控器电平的"过期保护" ─────────────────────────────────────────────
    # 遥控器那一路的电平是**按音频帧喂**的（有帧才有读数），遥控器一旦停止推流
    # 就再没有人来把它刷回去 —— 读数会冻在最后一帧上，界面就一直显示"有声音"、
    # 电平条一直亮着，看着像"卡住了"。
    # 另外两路不需要这么处理：电脑麦克风与混音输出都有常驻回调在持续写，
    # 没声音时它们自己会写 -96。
    # 判据用"最后一次收到音频帧的时间"，超过 _REMOTE_LEVEL_TTL 就当成没声音；
    # 这个门限比一帧的间隔（约 20ms）宽两个数量级，不会误伤正常语音里的停顿。
    now = time.time()
    remote_db = s.remote_level_db
    if s.audio_last_at and (now - s.audio_last_at) > _REMOTE_LEVEL_TTL:
        remote_db = -96.0
    return {
        "status": {
            "connected": bool(s.connected),
            "streaming": bool(s.streaming),
            "device": s.device or "",
            "running": True,
            # 鼠标模式（v1.0.31）。放进 `status` 而不是另开一块：前端是**高频**
            # 轮询这个接口的（150ms），模式切换必须**立刻**在界面上看到 ——
            # 这个项目被"静默状态"坑过太多次，而鼠标模式是**接管方向键**的模式，
            # 用户不知道自己在里面就会把"按方向键没反应"当成故障来报。
            "mouse_mode": bool(s.mouse_mode),
        },
        "levels": {
            "sys": round(s.sys_level_db, 1),
            "remote": round(remote_db, 1),
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
            # 混合输出里被软限幅的采样占比（%），-1 = 还没统计。
            # 「增益开太大」的唯一客观依据 —— 顶格就是这个数在涨。
            "mix_limit_pct": round(s.mix_limit_pct, 1),
            # 自动增益（v1.0.29）：实际生效的遥控器增益 = remote_gain × agc_factor。
            # 界面上要能看见"AGC 正在替你往下收"，否则用户只会觉得"声音怎么变小了"。
            "agc_factor": round(s.agc_factor, 3),
            "agc_enabled": bool(s.agc_enabled),
            # 鼠标模式（v1.0.31）：当前每帧位移 + 本次进入以来累计走了多少像素。
            # 「到底动没动」是用户最想知道的一件事 —— 只给一个"已进入"没法排查。
            "mouse_speed_now": round(s.mouse_speed_now, 2),
            "mouse_moved_px": int(s.mouse_moved_px),
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
    # 解析一次，给 devices 与 checklist 共用（_checklist 内部还会再算一次，
    # 但两者走的是**同一个函数**，不会出现两个结论）。
    _mic_resolved = _resolve_in_device(cfg)

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
        # ⚠ 这里**不要**再写一遍 levels。
        #   之前 build_state 在展开 build_live() 之后又覆盖了一次 levels，
        #   等于把 build_live 里刚做的"遥控器电平过期保护"当场抹掉 ——
        #   同一份数据有两个来源，改了一个另一个悄悄赢，是最难查的那种 bug。
        #   电平/波形的唯一出处就是 build_live()。
        "waves": state.waves_payload(),
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
            "swallow_ok": bool(getattr(cfg, "swallow_ok_during_voice", True)),
            # 语音结束后自动发送（v1.0.13）：**默认关** —— 什么时候发由用户决定。
            # 面板上要能看见、也能改，否则"发不出去/乱发"都只能靠猜。
            # ⚠ 这里的 getattr 兜底也必须是 False：属性缺失时若报 True，
            #   面板会显示成"已勾上"、而后端实际不发（方向相反，最难查）。
            "send_after_voice": bool(getattr(cfg, "send_after_voice", False)),
            "send_after_voice_delay_ms": int(
                getattr(cfg, "send_after_voice_delay_ms", 800) or 800),
            "send_after_voice_key": str(
                getattr(cfg, "send_after_voice_key", "enter") or "enter"),
            # 厂商页按键读取（v1.0.11 起按键映射全靠这一路）——
            # 面板上要能看见它是不是开着，否则"按键没反应"又变成猜谜。
            "hid_vendor_keys": bool(getattr(cfg, "hid_vendor_keys", True)),
            # 按键旁路 + 兼容款兜底（P1-8）：兜底默认关，面板要能看出
            # "这次是精确匹配还是猜的"，否则"按键串台"查不出来。
            "hid_frida_tap": bool(getattr(cfg, "hid_frida_tap", True)),
            "hid_frida_any_hid": bool(getattr(cfg, "hid_frida_any_hid", False)),
            # 日志脱敏 / raw HID（P2-6）。要能从 `/api/config` 读出来 ——
            # 否则用户往聊天窗口贴日志时**不知道自己在贴什么**。
            # （跟 hid_frida_* 一样属于"高级项"，UI 上不铺开关，改 config.json。）
            "log_redact": bool(getattr(cfg, "log_redact", True)),
            "log_raw_hid": bool(getattr(cfg, "log_raw_hid", False)),
            # 鼠标模式（v1.0.31）。面板要能看见、也要能改 ——
            # 尤其是「空闲自动退出」：遥控器搁沙发上很容易压到方向键。
            "mouse_mode_enabled": bool(getattr(cfg, "mouse_mode_enabled", True)),
            "mouse_speed": float(getattr(cfg, "mouse_speed", 5.0) or 5.0),
            "mouse_speed_max": float(
                getattr(cfg, "mouse_speed_max", 20.0) or 20.0),
            "mouse_accel_ms": int(getattr(cfg, "mouse_accel_ms", 800) or 0),
            "mouse_idle_exit_s": int(
                getattr(cfg, "mouse_idle_exit_s", 60) or 0),
        },
        "devices": {
            # `system_mic` 是**程序真正打开**的那只（由 SystemMic.start 写入 state），
            # 而 `system_mic_resolved/reason` 是**解析出来应该用**的那只。
            # 两者都吐出来：以前只有前者，检查项却自己另算一份，于是"面板说 A、
            # 日志说 B"，用户只能靠猜（2026-09-29 真机就是这个局面）。
            "system_mic": s.sys_mic_name or "",
            "system_mic_resolved": _mic_resolved[0],
            "system_mic_reason": _mic_resolved[2],
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
    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 防 MIME 嗅探：把一个 .txt 当脚本执行是 XSS 的常见入口
        self.send_header("X-Content-Type-Options", "nosniff")
        # CSP 对所有响应都发（对 JSON 无害），漏一个分支就等于没设
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
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

    # ── 鉴权（P1-1）──────────────────────────────────────────────────────
    # ⚠ 为什么必须自己写：`ThreadingHTTPServer` 默认**什么都不验**。这个服务
    #   绑在 127.0.0.1 上，看着"外网进不来"就安全了 —— 但同机的任何进程、
    #   以及用户浏览器里打开的任何网页，都能直接打这些接口。
    def _cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == _TOKEN_COOKIE:
                return v
        return ""

    def _token_ok(self) -> bool:
        want = console_token()
        got = self._cookie_token() or (self.headers.get("X-RVB-Token") or "").strip()
        # compare_digest：别用 `==`，那会把令牌长度/前缀通过耗时泄出去
        return bool(got) and hmac.compare_digest(got, want)

    def _host_ok(self) -> bool:
        """Host 必须**精确**是本机这个端口 —— 防 DNS rebinding。

        攻击者把自己的域名解析到 127.0.0.1，再让浏览器去打这个域名：
        Cookie 会带上（同站），Origin 也是攻击者的域名 —— 所以这条 + Origin
        白名单必须一起用，只留一条都不够。
        """
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return False
        if raw.startswith("["):                    # [::1]:1234
            name, _, rest = raw[1:].partition("]")
            port = rest.lstrip(":")
        else:
            name, _, port = raw.partition(":")
        if name.lower() not in _ALLOWED_HOSTS:
            return False
        if port:
            try:
                if int(port) != self.server.server_address[1]:
                    return False
            except ValueError:
                return False
        return True

    def _origin_ok(self) -> bool:
        """有 Origin 就必须是本机控制台自己的来源；没有 Origin 才放行。

        浏览器对**跨站** POST 一定会带 Origin（表单、fetch 都带），所以
        "带了但不是自己" = 跨站，直接拒。同源的 fetch 也带 Origin，值是
        `http://127.0.0.1:<port>`，能对上。非浏览器客户端不带 Origin，
        由令牌兜住。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                          f"http://[::1]:{port}")

    def _guard(self, *, post: bool) -> bool:
        """API 统一入口检查。返回 False 时**已经把响应发出去了**。"""
        if not self._host_ok():
            self.close_connection = True
            self._err("bad host", 403)
            return False
        if not self._origin_ok():
            self.close_connection = True
            self._err("bad origin", 403)
            return False
        if not self._token_ok():
            self.close_connection = True
            self._err("unauthorized", 401)
            return False
        if post:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                # 只收 application/json：`text/plain` 这类"简单请求"**不触发
                # CORS 预检**，是跨站写接口最省事的入口，所以从类型上掐掉。
                self.close_connection = True
                self._err("需要 Content-Type: application/json", 415)
                return False
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n > _MAX_BODY:
                self.close_connection = True
                self._err(f"请求体过大（上限 {_MAX_BODY} 字节）", 413)
                return False
        return True

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > _MAX_BODY:                      # 双保险：_guard 已挡一次
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    # ── GET ──
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._guard(post=False):
            return
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
        # 页面/静态资源本身就是"发令牌"的地方：控制台是同源页面，Cookie 一下发
        # 后续 /api/* 就自动带上。不需要页面脚本碰令牌（HttpOnly，脚本也读不到）。
        cookie = (f"{_TOKEN_COOKIE}={console_token()}; Path=/; "
                  f"HttpOnly; SameSite=Strict")
        self._send(200, target.read_bytes(),
                   _MIME.get(target.suffix, "application/octet-stream"),
                   extra={"Set-Cookie": cookie})

    # ── POST ──
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not self._guard(post=True):
            return
        body = self._body()
        try:
            if path == "/api/config":
                return self._json(self._patch_config(body))
            if path == "/api/mix":
                return self._json(self._patch_mix(body))
            if path == "/api/mapping":
                return self._json(self._patch_mapping(body))
            if path == "/api/mapping/reset":
                Config.update(lambda c: setattr(c, "keymap", dict(DEFAULT_KEYMAP)))
                # 恢复默认也是一次映射变更 → 同样要推给钩子并等确认（P1-6）。
                return self._json({"ok": True,
                                   "effective": self._push_mapping_now()})
            if path == "/api/hotkey":
                from keys import combo_bad_parts
                keys_in = [str(k).strip() for k in (body.get("keys") or []) if str(k).strip()]
                # 服务端把住最后一道关：解析不出来的键名写进配置只会静默失效，
                # 用户看到的就是「设了没用」。宁可直接报错。
                bad = [p for p in keys_in if combo_bad_parts(p)]
                if bad:
                    return self._err(f"不认识这些键名：{'、'.join(bad)}")
                cfg = Config.update(lambda c: setattr(c, "voice_hotkey", keys_in))
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

    # 内存参数名 → config.json 字段名。只有这几项在配置里有家；
    # 静音/独奏（*_muted / *_solo）是**临时**操作，故意不落盘。
    _MIX_TO_CFG = (
        ("sys_enabled",    "system_mic_enabled"),
        ("remote_enabled", "remote_mic_enabled"),
        ("sys_gain",       "system_mic_gain"),
        ("remote_gain",    "gain"),
    )

    def _patch_mix(self, body: dict) -> dict:
        """改混音参数 —— 并且把**在 config.json 里有对应项**的那几个落盘。

        ⚠ 为什么必须落盘（v1.0.8 的真机反馈）：
          音频页的「参与混合 / 增益」原来只改内存里的 state._mix，config.json
          一个字没动。于是下面任何一件事发生，用户的勾选就被**悄悄回滚**：
            · 在设置页改了任何一项 —— _patch_config 结尾会拿 cfg 重新灌一遍 set_mix
            · 重连 / 重启 —— run_bridge 启动时同样用 cfg 灌 set_mix
          用户看到的就是「我明明不让系统麦克风参与说话了，它还是在输出」。
          界面上既然给了开关，就得让它留得住。
        """
        mix = state.set_mix(**body)
        saved = {"v": False}

        def _apply(cfg) -> bool:
            for mix_key, cfg_key in self._MIX_TO_CFG:
                if mix_key not in body or not hasattr(cfg, cfg_key):
                    continue
                new = mix.get(mix_key)
                if new is not None and getattr(cfg, cfg_key) != new:
                    setattr(cfg, cfg_key, new)
                    saved["v"] = True
            return saved["v"]

        # ⚠ 走 Config.update（同一把锁里"读→改→写"），**不要**自己 load/save：
        #   控制台是 ThreadingHTTPServer，并发请求各读各的旧快照再写回，
        #   会把彼此改的字段整段覆盖掉（2026-09-29 审查报告 P1-5）。
        Config.update(_apply)
        return {"mix": mix, "saved": saved["v"]}

    def _patch_config(self, body: dict) -> dict:
        allowed = {
            "gain": float, "audio_output": str, "system_mic_device": str,
            "system_mic_gain": float, "hotkey_mode": str, "suppress_keys": bool,
            "input_method": str, "device": str, "voice_mode": str,
            "mapping_enabled": bool, "swallow_ok_during_voice": bool,
            "hid_vendor_keys": bool,
            # 兼容款兜底（P1-8）：VID/PID 对不上时是否允许猜第一台 BLE HID。
            # 必须进白名单 —— 不进就是"改了 config.json 没反应"（静默）。
            "hid_frida_any_hid": bool,
            # ⚠ 这两个原先漏在白名单外：前端一发过来就被**静默丢掉**，
            #   界面显示"已关闭"、后端仍按旧配置把这一路混进去 ——
            #   「我明明不让系统麦克风参与说话了，它还是在输出」有一半出在这里。
            "system_mic_enabled": bool, "remote_mic_enabled": bool,
            # v1.0.13 语音结束自动发送。同样必须进白名单：不进就是
            # "界面上改了、后端当没听见"，而用户看到的只是"它不听话"。
            "send_after_voice": bool, "send_after_voice_delay_ms": int,
            "send_after_voice_key": str,
            # v1.0.22 键盘页兜底映射开关（默认关）。不进白名单 = 用户打开后
            # 重启又变回关的，而界面会显示成打开 —— 方向相反，最难查。
            "keyboard_page_keys": bool,
            # 日志脱敏 / raw HID（P2-6）。**必须进白名单** —— 不进就是
            # "改了 config.json 或调 API 没反应"（静默），更糟的是用户以为
            # 脱敏开着、其实没开（贴日志时把完整地址漏出去）。
            "log_redact": bool, "log_raw_hid": bool,
            # 自动增益（v1.0.29）。**必须进白名单** —— 不进就是"界面上关了、
            # 后端还在自动收你的增益"，方向相反，最难查。
            "auto_gain": bool,
            # 「等遥控器出声再叫输入法」（v1.0.29，默认关）。同样必须进白名单：
            # 不进就是"界面上打开了、后端当没听见"，而用户只会觉得"它不听话"。
            "hotkey_wait_first_frame": bool, "hotkey_wait_max_ms": int,
            # 鼠标模式（v1.0.31）。**必须进白名单** —— 不进就是"界面上拖了滑块
            # 没反应"，而 `_get_cfg()` 那边还会**静默**退回默认值。
            "mouse_mode_enabled": bool, "mouse_speed": float,
            "mouse_speed_max": float, "mouse_accel_ms": int,
            "mouse_idle_exit_s": int,
        }
        ignored: list[str] = []

        def _apply(cfg) -> None:
            for k, v in body.items():
                if k not in allowed or not hasattr(cfg, k):
                    ignored.append(k)
                    continue
                try:
                    setattr(cfg, k, allowed[k](v))
                except (TypeError, ValueError):
                    ignored.append(k)
                    continue

        # 走 Config.update：同一把锁里"读→改→写"，并发请求不会互相覆盖（P1-5）。
        cfg = Config.update(_apply)
        if ignored:
            # 静默忽略是不可调试的：前端以为改成功了，后端当没听见。
            # 留一条日志 + 在返回值里说明，下次"设了没用"能立刻对上号。
            logger.warning("⚠ /api/config 忽略了这些字段（不在白名单或类型不符）：%s", ignored)
        # 增益是"热"参数：面板一改立即生效，不用重连。
        # 注意要同时更新两处 —— state.set_gain 是给诊断/显示用的，
        # state.set_mix(remote_gain=...) 才是混音回调真正读的那个值；
        # 只改前一处的话滑块看着动了、音量没变。
        state.set_gain(cfg.gain)
        state.set_mix(remote_gain=cfg.gain,
                      sys_gain=cfg.system_mic_gain,
                      sys_enabled=cfg.system_mic_enabled,
                      remote_enabled=cfg.remote_mic_enabled)
        # 自动增益（v1.0.29）的开关也是**热**的：面板一关立刻生效。
        # ⚠ 必须同步到 main 里那个 `_agc` —— 音频回调读的是它。
        #   只写 config / state 的话，界面上显示"已关闭"、回调却还在自动
        #   往下收增益（方向相反，最难查的那种）。
        try:
            import main
            main._agc["enabled"] = bool(cfg.auto_gain)
            if not cfg.auto_gain:
                main._agc_reset()      # 关掉就立刻回到"完全不介入"
        except Exception:                            # noqa: BLE001
            pass
        state.update(agc_enabled=bool(cfg.auto_gain))
        # 日志开关是**热**的：改完立刻生效，不用重启（P2-6）。
        # ⚠ 只影响**之后**写下的行 —— 已经落盘的内容不会回头改写（那是历史，
        #   而且改写历史会让日志失去可信度）。要彻底干净就重启一次。
        logsetup.set_redact(cfg.log_redact)
        logsetup.set_raw_hid(cfg.log_raw_hid)
        out = {"ok": True, "config": body, "ignored": ignored}
        # `mapping_enabled` 变了也必须立刻下发（关掉 → 下发空表）。
        # 只在这里判一次：其它字段改不动屏蔽表，白推一次没有意义（P1-6）。
        if "mapping_enabled" in body:
            out["effective"] = self._push_mapping_now()
        return out

    def _push_mapping_now(self) -> bool:
        """把当前 keymap 下发给 Frida 钩子，并**等它确认**（审查报告 P1-6）。

        为什么要等：JS 侧是"按这张表把报告里的 usage 原地写 0"的。下发是异步的，
        不等确认就回「已生效」，用户可能立刻按一个键，而钩子还在用旧表 ——
        看到的是"改了没用"。等不到就如实说"未确认"，别让界面撒谎。

        返回 True = 钩子确认了（或本来就没有旁路在跑，没什么要确认的）。
        """
        try:
            import frida_hid
        except Exception:                               # noqa: BLE001
            return True
        try:
            cfg = Config.load()
            km = dict(cfg.keymap) if getattr(cfg, "mapping_enabled", True) else {}
            return frida_hid.push_mapping(km, wait=True, timeout=1.5)
        except Exception as e:                          # noqa: BLE001
            logger.warning("下发按键屏蔽表失败：%s", e)
            return False

    def _patch_mapping(self, body: dict) -> dict:
        btn = str(body.get("button") or "")
        if btn not in CHROMECAST_BUTTONS:
            return {"error": f"未知按键 {btn!r}"}
        if CHROMECAST_BUTTONS[btn]["usage"] == "voice":
            return {"error": "语音键由语音通道处理，不可映射"}
        value = str(body.get("value") or "")
        # 走 Config.update：并发改不同按键时不会互相覆盖（P1-5）。
        Config.update(lambda cfg: cfg.keymap.__setitem__(btn, value))
        # ⚠ 改完必须把新表推给钩子 —— 否则运行中改映射要重连才生效（P1-6）。
        return {"ok": True, "button": btn, "value": value,
                "effective": self._push_mapping_now()}

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
    console_token()          # 先把令牌定下来，别等第一个请求才生成
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
