"""
remote-voice-bridge — Windows 托盘 GUI 入口（对标 vRemoter 的菜单栏应用）

启动后：
  · 后台线程跑 BLE/ATVV 桥（main.run_bridge），断线自动重连
  · 系统托盘图标反映连接 / 推流状态
  · 右键菜单：状态 / 输入法 / 设备 / 开机启动 / 控制台 / 日志 / 退出
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
import winreg
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from config import CONFIG_DIR, INPUT_METHODS, Config
import state

APP_NAME  = "Remote Voice Bridge"
LOG_FILE  = CONFIG_DIR / "bridge.log"
RUN_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "RemoteVoiceBridge"

# 控制台日志面板的加载上限。
# 这个日志实测能长到 2.4MB / 34000 行 —— 首次打开时一次性塞进 Text 控件
# 会让窗口卡死好几秒，所以只加载尾部，并限制控件内保留的行数。
_LOG_TAIL_BYTES = 120_000
_LOG_MAX_LINES  = 3000

_stop = threading.Event()
_icon: "pystray.Icon | None" = None


# ── 图标 ──────────────────────────────────────────────────────────────────────
def _icon_image(size: int = 64, connected: bool = False, streaming: bool = False):
    """手绘麦克风图标。未连接=灰，已连接=暖橙，推流中=暖橙红。"""
    if streaming:
        body = (232, 96, 48, 255)
    elif connected:
        body = (238, 158, 58, 255)
    else:
        body = (150, 150, 150, 255)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    w = h = size
    cx = w * 0.5
    lw = max(2, int(w * 0.075))          # 线宽

    head_w   = w * 0.24
    head_top = h * 0.15
    head_bot = h * 0.55
    # 话筒头（胶囊）
    d.rounded_rectangle(
        [cx - head_w / 2, head_top, cx + head_w / 2, head_bot],
        radius=head_w / 2, fill=body,
    )
    # U 形支架
    d.arc([cx - w * 0.27, head_bot - h * 0.12, cx + w * 0.27, h * 0.80],
          start=0, end=180, fill=body, width=lw)
    # 立柱 + 底座
    d.line([cx, h * 0.80, cx, h * 0.90], fill=body, width=lw)
    d.line([cx - w * 0.15, h * 0.90, cx + w * 0.15, h * 0.90], fill=body, width=lw)
    return img


# ── 开机启动（注册表 Run 键，无需管理员权限）────────────────────────────────
def _launch_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def _launch_cmd_str() -> str:
    parts = _launch_cmd()
    return subprocess.list2cmdline(parts)


def _is_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(v)
    except OSError:
        return False


def _set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _launch_cmd_str())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except OSError:
                pass


# ── 菜单动作 ──────────────────────────────────────────────────────────────────
def _status_text(item=None) -> str:
    """菜单标题。pystray 会把 MenuItem 自身作为首个参数传给 callable text，
    因此必须接受一个可选参数。"""
    s = state.get()
    dev = s.device or "遥控器"
    if s.streaming:
        return f"● 语音中 · {dev}"
    if s.connected:
        return f"● 已连接 · {dev}"
    return "○ 未连接（按遥控器任意键唤醒）"


def _make_im_action(key: str):
    def _act(icon, item):
        cfg = Config.load()
        cfg.input_method = key
        cfg.save()
        icon.update_menu()
    return _act


def _toggle_autostart(icon, item):
    _set_autostart(not _is_autostart())
    icon.update_menu()


def _relaunch(icon, item):
    subprocess.Popen(_launch_cmd(), close_fds=True)
    _quit(icon, item)


def _open_log(icon, item):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("", encoding="utf-8")
    os.startfile(str(LOG_FILE))


_console_lock = threading.Lock()
_console_open = False


def _open_console(icon=None, item=None):
    """控制台窗口在独立线程里建自己的 Tk root。"""
    global _console_open
    with _console_lock:
        if _console_open:
            return
        _console_open = True
    threading.Thread(target=_console_main, daemon=True).start()


def _quit(icon, item):
    _stop.set()
    state.reset()
    try:
        icon.stop()
    except Exception:
        pass


# ── 菜单 ──────────────────────────────────────────────────────────────────────
def build_menu(icon) -> pystray.Menu:
    im_items = [
        pystray.MenuItem(
            meta["desc"],
            _make_im_action(key),
            checked=lambda item, k=key: Config.load().input_method == k,
            radio=True,
        )
        for key, meta in INPUT_METHODS.items()
    ]

    return pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("输入法", pystray.Menu(*im_items)),
        pystray.MenuItem("设备", pystray.Menu(
            pystray.MenuItem("重新连接", _relaunch),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机启动", _toggle_autostart,
                         checked=lambda item: _is_autostart()),
        pystray.MenuItem("打开控制台", _open_console),
        pystray.MenuItem("打开日志", _open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit),
    )


# ── 波形绘制 ─────────────────────────────────────────────────────────────────
def _draw_waveform(canvas, pts: list[int], fresh: bool,
                   w: int | None = None, h: int | None = None) -> None:
    """在 canvas 上画实时波形。

    自适应幅度：波形的用途是"一眼看出有没有声音进来"。若用固定满量程刻度，
    说话音量偏小就会画成一条直线，看起来像坏了 —— 所以按窗口内峰值归一化，
    真实音量交给旁边的「峰值」数字表达。

    w/h 可显式传入，便于离屏测试（未 map 的 canvas 上 winfo_width() 恒为 1）。
    """
    w = max(1, w if w is not None else canvas.winfo_width())
    h = max(1, h if h is not None else canvas.winfo_height())
    mid = h / 2.0
    canvas.delete("all")
    canvas.create_line(0, mid, w, mid, fill="#ececec")

    if not pts:
        # 这一段会话还没收到任何音频
        canvas.create_text(w / 2, mid, fill="#b0b0b0",
                           font=("Microsoft YaHei UI", 9),
                           text="等待音频…（按住遥控器语音键说话）")
        return

    peak = max((abs(p) for p in pts), default=0) or 1
    scale = (h * 0.44) / peak
    step = w / max(1, len(pts) - 1)
    coords: list[float] = []
    for i, p in enumerate(pts):
        coords.extend((i * step, mid - p * scale))
    if len(coords) >= 4:
        # 停止后**不清空**波形，只转灰 ——
        # 否则一松手波形就消失，看不到刚才说了什么。
        canvas.create_line(*coords, fill=("#e86030" if fresh else "#cfcfcf"), width=1.4)
    canvas.create_text(6, 10, anchor="w", fill="#c8c8c8",
                       font=("Microsoft YaHei UI", 8),
                       text="实时 · 自适应放大" if fresh else "已停止 · 上一段")


# ── 控制台窗口 ────────────────────────────────────────────────────────────────
def _console_main():
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title(f"{APP_NAME} · 控制台")
    root.geometry("780x620")
    root.minsize(640, 500)

    # ── 状态行 ──────────────────────────────────────────────────────────────
    top = tk.Frame(root)
    top.pack(fill="x", padx=12, pady=(12, 6))

    lbl_status = tk.Label(top, text="—", font=("Microsoft YaHei UI", 11, "bold"))
    lbl_status.pack(side="left")

    lbl_im = tk.Label(top, text="", font=("Microsoft YaHei UI", 9), fg="#666")
    lbl_im.pack(side="right")

    # ── 实时波形 ────────────────────────────────────────────────────────────
    tk.Label(root, text="实时语音输入", font=("Microsoft YaHei UI", 9),
             fg="#666").pack(anchor="w", padx=12)

    wave = tk.Canvas(root, height=116, bg="#ffffff",
                     highlightthickness=1, highlightbackground="#dddddd")
    wave.pack(fill="x", padx=12, pady=(2, 6))

    # ── 电平条 ──────────────────────────────────────────────────────────────
    lvl_frame = tk.Frame(root)
    lvl_frame.pack(fill="x", padx=12, pady=(0, 2))
    tk.Label(lvl_frame, text="电平", font=("Microsoft YaHei UI", 9),
             fg="#666").pack(side="left")
    level_canvas = tk.Canvas(lvl_frame, height=10, bg="#eeeeee",
                             highlightthickness=0, width=220)
    level_canvas.pack(side="left", padx=(8, 8))
    lbl_lvl = tk.Label(lvl_frame, text="0%", font=("Consolas", 9), fg="#666")
    lbl_lvl.pack(side="left")

    # ── 诊断指标 ────────────────────────────────────────────────────────────
    lbl_stat = tk.Label(root, text="", font=("Consolas", 9), fg="#666", anchor="w")
    lbl_stat.pack(fill="x", padx=12, pady=(0, 8))

    # ── 日志区 ──────────────────────────────────────────────────────────────
    txt = scrolledtext.ScrolledText(root, font=("Consolas", 9), wrap="none", height=12)
    txt.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    txt.configure(state="disabled")

    # ── 按钮区 ──────────────────────────────────────────────────────────────
    btns = tk.Frame(root)
    btns.pack(fill="x", padx=12, pady=(0, 12))
    tk.Button(btns, text="重新连接", command=lambda: subprocess.Popen(_launch_cmd())
              ).pack(side="left")
    tk.Button(btns, text="清空日志",
              command=lambda: (LOG_FILE.write_text("", encoding="utf-8"))
              ).pack(side="left", padx=(8, 0))
    tk.Button(btns, text="打开日志文件", command=lambda: _open_log()
              ).pack(side="left", padx=(8, 0))

    last_size = [0]

    def _draw_wave(s) -> None:
        fresh = bool(s.audio_last_at) and (time.time() - s.audio_last_at) < 1.0
        _draw_waveform(wave, state.wave_snapshot(), fresh)

    def refresh():
        s = state.get()

        # 状态
        if s.streaming:
            lbl_status.configure(text=f"● 语音中 · {s.device or '遥控器'}", fg="#e86030")
        elif s.connected:
            lbl_status.configure(text=f"● 已连接 · {s.device or '遥控器'}", fg="#ea8c1e")
        else:
            lbl_status.configure(text="○ 未连接", fg="#999999")

        cfg = Config.load()
        im = INPUT_METHODS.get(cfg.input_method, {})
        lbl_im.configure(
            text=f"输入法：{im.get('desc', cfg.input_method)}　语音键："
                 f"{'+'.join(cfg.trigger_keys_windows()) or '未配置'}（{cfg.hotkey_mode}）"
        )

        # 波形
        _draw_wave(s)

        # 电平
        level_canvas.delete("bar")
        cw = max(1, level_canvas.winfo_width())
        fill_w = int(cw * min(100, max(0, s.level)) / 100)
        if fill_w > 0:
            level_canvas.create_rectangle(0, 0, fill_w, 10, fill="#e86030",
                                          outline="", tags="bar")
        lbl_lvl.configure(text=f"{s.level}%")

        # 诊断指标 —— 这一行就是用来回答"输入法没反应，到底是没收到音频还是没触发输入法"
        ago = (time.time() - s.audio_last_at) if s.audio_last_at else None
        parts = []
        if s.sample_rate:
            parts.append(f"{s.sample_rate}Hz")
        if s.frame_bytes:
            parts.append(f"{s.frame_bytes}B/帧")
        if ago is not None:
            parts.append(f"最后音频 {ago:.1f}s 前")
        tail = "　·　".join(parts)

        if s.streaming and s.audio_frames == 0:
            lbl_stat.configure(
                text=f"⚠ 本次 0 帧 —— 音频根本没上来（遥控器在推流，但解码后没有数据）",
                fg="#c62828")
        elif s.audio_frames:
            lbl_stat.configure(
                text=f"本次 {s.audio_frames} 帧　·　峰值 {s.audio_peak}{('　·　' + tail) if tail else ''}",
                fg="#444444")
        else:
            lbl_stat.configure(text=tail, fg="#999999")

        # 日志增量追加（首次只取尾部，见 _LOG_TAIL_BYTES 的说明）
        try:
            if LOG_FILE.exists():
                size = LOG_FILE.stat().st_size
                if size < last_size[0]:
                    last_size[0] = 0
                    txt.configure(state="normal")
                    txt.delete("1.0", "end")
                    txt.configure(state="disabled")
                if size > last_size[0]:
                    with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
                        if last_size[0] == 0 and size > _LOG_TAIL_BYTES:
                            f.seek(size - _LOG_TAIL_BYTES)
                            f.readline()          # 丢掉被截断的半行
                        else:
                            f.seek(last_size[0])
                        chunk = f.read()
                    last_size[0] = size
                    txt.configure(state="normal")
                    txt.insert("end", chunk)
                    if int(txt.index("end-1c").split(".")[0]) > _LOG_MAX_LINES:
                        txt.delete("1.0", f"{_LOG_MAX_LINES // 2}.0")
                    txt.see("end")
                    txt.configure(state="disabled")
        except Exception:
            pass

        # 150ms：波形要跟得上，又不能太费 —— 这个窗口本身很轻
        root.after(150, refresh)

    def on_close():
        global _console_open
        with _console_lock:
            _console_open = False
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    root.mainloop()


# ── 桥线程 ────────────────────────────────────────────────────────────────────
def _bridge_worker():
    import asyncio
    from main import run_bridge

    while not _stop.is_set():
        try:
            asyncio.run(run_bridge())
        except Exception as e:  # noqa: BLE001
            print(f"[bridge] {e}", flush=True)
        if _stop.is_set():
            break
        time.sleep(3)


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    global _icon
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    threading.Thread(target=_bridge_worker, daemon=True).start()

    _icon = pystray.Icon(APP_NAME, _icon_image(), f"{APP_NAME}（启动中…）")
    _icon.menu = build_menu(_icon)

    def _refresh(icon=None):
        """定时刷新图标与菜单，反映最新状态。"""
        s = state.get()
        _icon.icon = _icon_image(connected=s.connected, streaming=s.streaming)
        _icon.title = _status_text()
        try:
            _icon.update_menu()
        except Exception:
            pass
        if not _stop.is_set():
            threading.Timer(0.8, _refresh).start()

    _icon.menu = build_menu(_icon)
    threading.Timer(0.8, _refresh).start()

    _icon.run()


if __name__ == "__main__":
    main()
