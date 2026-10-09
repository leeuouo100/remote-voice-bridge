"""
remote-voice-bridge — 托盘入口

· 后台线程跑 BLE/ATVV 桥（main.run_bridge），断线自动重连
· 系统托盘图标反映连接 / 推流状态
· 右键菜单 → 控制台：开本地 Web 控制台（对标 vRemoter 的界面）

为什么控制台不是 tkinter
------------------------
要做到 vRemoter 那种精致度（暗色卡片、三路电平表、遥控器示意图、细排版），
tkinter 的控件体系表达不出来 —— 硬做只能得到"一堆灰按钮"。
现在控制台是本地起的 Web UI（见 console_server.py + ui/），
只用标准库 http.server，不新增依赖，视觉上也不再受限。
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

from config import (APP_VERSION, BACKUP_DIR, CONFIG_DIR, INPUT_METHODS, Config,
                    reconnect_backoff)
import state

APP_NAME  = "Remote Voice Bridge"
LOG_FILE  = CONFIG_DIR / "bridge.log"
RUN_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "RemoteVoiceBridge"
REPO_URL  = "https://github.com/leeuouo100/remote-voice-bridge"

# 修复工具的产出（路径与 pairing.py 保持一致，别各写各的）
PAIRING_REPORT = CONFIG_DIR / "pairing-fix.txt"
# 备份目录从 config 拿**同一个常量**（P1-7 之后它在 %ProgramData% 下，
# 不再是 CONFIG_DIR/backup；这里再写一遍字面量迟早会漂）。
PAIRING_BACKUP = BACKUP_DIR

_stop = threading.Event()
_icon: "pystray.Icon | None" = None
# 桥线程的引用：`_quit` 要 join 它，等它把 GattSession / Frida / 音频流收干净
# 再放进程走（2026-09-29 审查报告 P1-4）。
_bridge_thread: "threading.Thread | None" = None


# ── DPI ───────────────────────────────────────────────────────────────────────
def _enable_dpi_awareness() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ── 图标 ──────────────────────────────────────────────────────────────────────
def _icon_image(size: int = 64, connected: bool = False, streaming: bool = False):
    """手绘麦克风图标。未连接=灰，已连接=暖橙，推流中=橙红。"""
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
    lw = max(2, int(w * 0.075))

    head_w   = w * 0.24
    head_top = h * 0.15
    head_bot = h * 0.55
    d.rounded_rectangle(
        [cx - head_w / 2, head_top, cx + head_w / 2, head_bot],
        radius=head_w / 2, fill=body,
    )
    d.arc([cx - w * 0.27, head_bot - h * 0.12, cx + w * 0.27, h * 0.80],
          start=0, end=180, fill=body, width=lw)
    d.line([cx, h * 0.80, cx, h * 0.90], fill=body, width=lw)
    d.line([cx - w * 0.15, h * 0.90, cx + w * 0.15, h * 0.90], fill=body, width=lw)
    return img


# ── 开机启动 ──────────────────────────────────────────────────────────────────
def _launch_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


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
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                              subprocess.list2cmdline(_launch_cmd()))
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except OSError:
                pass


# ── 动作 ──────────────────────────────────────────────────────────────────────
def _status_text(item=None) -> str:
    """菜单标题。pystray 会把 MenuItem 自身作为首个参数传给 callable text。"""
    s = state.get()
    dev = s.device or "遥控器"
    if s.streaming:
        return f"● 语音中 · {dev}"
    if s.connected:
        return f"● 已连接 · {dev}"
    # ⚠ 这里以前无条件写「按遥控器任意键唤醒」—— 那句话只对"设备睡着了"成立。
    #   遇上配对记录失效、无线电被禁用这类硬故障，它就是在骗人：
    #   2026-09-15 真机事故，武哥照这句话按了几十次遥控器，
    #   而桥其实每 3 秒崩一次。有具体原因就报具体原因。
    if s.last_event:
        return f"○ 未连接 · {s.last_event}"
    return "○ 未连接（按遥控器任意键唤醒）"


def _make_im_action(key: str):
    def _act(icon, item):
        # 走 Config.update：同一把锁里"读→改→写"。
        # 托盘动作和控制的 HTTP 请求是**两个线程**，各读各的旧快照再写回
        # 会把彼此改的字段整段覆盖掉（2026-09-29 审查报告 P1-5）。
        Config.update(lambda c: (setattr(c, "input_method", key),
                                 setattr(c, "voice_hotkey", [])))
        icon.update_menu()
    return _act


def _toggle_autostart(icon, item):
    _set_autostart(not _is_autostart())
    icon.update_menu()


def _request_reconnect(icon=None, item=None):
    """让桥线程断开重连，而不是再起一个进程。

    早前的「重新连接」是 Popen 一个新进程再退出自己 —— 两个实例会同时抢同一个
    BLE 连接和同一个托盘图标，谁赢不确定，表现为"点了重连就时好时坏"。
    """
    try:
        import main
        main.request_reconnect()
    except Exception:
        pass
    if icon:
        icon.update_menu()


def _open_console(icon=None, item=None):
    try:
        import console_server
        url = console_server.open_console()
        print(f"[console] {url}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[console] 启动失败：{e}", flush=True)


def _open_log(icon=None, item=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("", encoding="utf-8")
    os.startfile(str(LOG_FILE))            # noqa: S606


def _open_config_dir(icon=None, item=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.startfile(str(CONFIG_DIR))          # noqa: S606


# ── 修复 / 诊断工具 ───────────────────────────────────────────────────────────
# 2026-09-29（武哥的建议）：把这些工具收进托盘右键菜单 —— 出问题时不用再去
# 开始菜单或仓库目录里翻 .bat，「右键 → 修复 / 诊断」就有。
#
# 三条设计要点，每条都是踩过的坑：
#
#  · **只起窗口、绝不等待**。这些工具会弹 UAC、会 `pause`、会打印几十行诊断。
#    用 `subprocess.run` 等它 = 把托盘线程**卡死**（菜单点不动、图标不刷新、
#    连"退出"都点不了）。所以一律 `Popen` / `startfile` 后立刻返回。
#
#  · **提权交给工具自己**。`修复蓝牙配对.bat` 与 `RemoteVoiceBridgeDiag.exe`
#    内部会 `ShellExecute runas` 弹 UAC（见 pairing.elevate_and_wait）。
#    托盘进程自己不提权 —— 不把整个常驻进程的安全边界扩大。
#
#  · **安装版与源码版通吃**。优先用仓库里那两个 .bat：它们自己会判断
#    "旁边是 Diag.exe 还是 .py"，还带 `pause` 让用户看清结果。只有 bat
#    不在时才退回直接调 exe / python。
#
# ⚠ 这些工具**不要**改用 `os.system` —— 那会继承本进程的 stdout（GUI 版是空的），
#   输出直接扔掉，用户看到的就是"点了没反应"。

def _app_dir() -> Path:
    """程序所在目录。安装版 = exe 旁边；源码版 = 仓库根（本文件所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _find_tool(*names: str) -> Path | None:
    d = _app_dir()
    for n in names:
        p = d / n
        if p.exists():
            return p
    return None


def _spawn(cmd: list[str]) -> bool:
    """在新控制台窗口里跑命令。返回是否成功起进程。"""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010) if os.name == "nt" else 0
    try:
        subprocess.Popen(cmd, cwd=str(_app_dir()), creationflags=flags)  # noqa: S603
        return True
    except Exception as e:                       # noqa: BLE001
        print(f"[tools] 启动失败：{e}", flush=True)
        return False


def _open_path(p: Path) -> bool:
    try:
        os.startfile(str(p))                     # noqa: S606
        return True
    except Exception as e:                       # noqa: BLE001
        print(f"[tools] 打不开 {p}：{e}", flush=True)
        return False


def _notify(icon, msg: str, title: str = APP_NAME) -> None:
    """尽力弹一条托盘气泡。pystray 在部分后端上不支持，失败就算了。"""
    if icon is None:
        return
    try:
        icon.notify(msg, title)
    except Exception:                            # noqa: BLE001
        pass


def _run_pairing_tool(extra: list[str], icon=None) -> None:
    """蓝牙配对：优先走 .bat（自己找 exe / python），否则直接调 exe / 脚本。

    `extra` 里的开关对两个环境都成立：
      · `--fix-pairing`        诊断 + 需要时弹 UAC 修复
      · `--fix-pairing --dry-run`  只诊断、不改任何东西、不弹 UAC
    """
    bat = _find_tool("修复蓝牙配对.bat")
    if bat is not None:
        # .bat 走 ShellExecute 起新窗口：它内部 `cd /d "%~dp0"` 自己定工作目录，
        # 而且结尾有 `pause`，用户能看清结果。
        if _open_path(bat):
            return

    diag = _find_tool("RemoteVoiceBridgeDiag.exe")
    if diag is not None:
        _spawn([str(diag), *extra])
        return

    # 源码版兜底：直接跑 pairing.py（同样自己弹 UAC）
    src = _find_tool("pairing.py")
    if src is not None:
        # dry-run 时把 --fix-pairing 换成纯只读（pairing.py 不带 --fix 就是只读诊断）
        args = [a for a in extra if a != "--fix-pairing"]
        _spawn([sys.executable, str(src), *args])
        return

    _notify(icon, "找不到修复工具（安装目录里应有「修复蓝牙配对.bat」）")


def _fix_pairing(icon=None, item=None):
    """蓝牙配对修复 —— 换过 USB 口 / 连不上时跑这个。"""
    _run_pairing_tool(["--fix-pairing"], icon)
    _notify(icon, "已在新窗口里启动「蓝牙配对修复」。\n"
                  "它会先只读诊断，确认要改才弹 UAC。")


def _pairing_check(icon=None, item=None):
    """蓝牙配对体检（只读）—— 不改任何东西、不弹 UAC。"""
    _run_pairing_tool(["--fix-pairing", "--dry-run"], icon)
    _notify(icon, "已启动「蓝牙配对体检」（只读，不改任何设置）。")


def _diag_remote(icon=None, item=None):
    """遥控器诊断 —— 按键没反应时跑这个（要按几下遥控器）。"""
    diag = _find_tool("RemoteVoiceBridgeDiag.exe")
    if diag is not None:
        _spawn([str(diag)])
        return
    bat = _find_tool("diag-remote.bat")
    if bat is not None and _open_path(bat):
        return
    src = _find_tool(os.path.join("tools", "diag_remote.py"))
    if src is not None:
        _spawn([sys.executable, str(src)])
        return
    _notify(icon, "找不到诊断工具（安装目录里应有 RemoteVoiceBridgeDiag.exe）")


def _open_pairing_report(icon=None, item=None):
    """打开上次的配对体检报告。"""
    if not PAIRING_REPORT.exists():
        _notify(icon, "还没有体检报告。先跑一次「蓝牙配对体检」或「蓝牙配对修复」。")
        return
    _open_path(PAIRING_REPORT)


def _open_backup_dir(icon=None, item=None):
    """打开修复前的注册表备份目录（出问题可以双击 .reg 还原）。"""
    if not PAIRING_BACKUP.exists():
        _notify(icon, "还没有备份。修复工具动注册表之前会自动备份到这里。")
        return
    _open_path(PAIRING_BACKUP)


def _quit(icon, item):
    """退出：**先让桥线程自己收尾**，再关控制台和托盘。

    ⚠ 原来这里只 `_stop.set()` 就去 `icon.stop()` 了。桥线程是 daemon，
    主进程一结束就被直接掐掉 —— `run_bridge` 的 `finally`（关 GattSession /
    停 Frida 注入 / 关音频流）**不保证执行**。现象是"退出再开就连不上"，
    而日志里看不出任何异常（2026-09-29 审查报告 P1-4）。

    现在：置位 → 等桥线程自己走完 finally → 超时才放它走（并且**说出来**）。
    """
    _stop.set()
    try:
        import main
        main.request_stop()          # 让**正在跑的** run_bridge 也能看见
    except Exception:                # noqa: BLE001
        pass

    t = _bridge_thread
    if t is not None and t.is_alive():
        # 90 秒的连接等待已经会被 stop 叫醒，所以 8 秒足够；超时说明卡在别处，
        # 这时**必须留下痕迹** —— 否则"清理没跑完"会变成一个查不到的幽灵。
        # ⚠ 痕迹走 logger（→ bridge.log），**不要**只 print：主 exe 是
        #   console=False，print 的流向是空的，等于把唯一的线索扔掉。
        t.join(timeout=8.0)
        try:
            import logging
            _lg = logging.getLogger("rvb")
            if t.is_alive():
                _lg.warning("⚠ 桥线程 8 秒内没退出 → 进程将直接结束"
                            "（GattSession / 注入 / 音频流可能没清理干净）")
            else:
                _lg.info("🧹 托盘退出：桥线程已收尾")
        except Exception:                            # noqa: BLE001
            pass

    state.reset()
    try:
        import console_server
        console_server.ensure_started().stop()
    except Exception:
        pass
    try:
        icon.stop()
    except Exception:
        pass

    # ⚠⚠ 硬保底：`icon.stop()` 只是往托盘窗口 `PostMessage(WM_STOP)`，
    #   要靠 MainThread 的消息循环把它翻成 `PostQuitMessage`，`icon.run()`
    #   才会返回。任何一步没走通（消息循环抛异常、窗口句柄已失效、
    #   stop 在消息循环起来之前被调用……），`main()` 就永远不返回 ——
    #   用户点完「退出」什么也没发生，只能去任务管理器强杀。
    #
    #   清理这时已经跑完（或已按上面的超时放弃），所以这里直接退是安全的。
    #   正常路径下 `main()` 会在这 3 秒内返回，这条**永远不会执行**。
    def _force_exit() -> None:
        time.sleep(3.0)
        os._exit(0)

    threading.Thread(target=_force_exit, daemon=True,
                     name="rvb-force-exit").start()


# ── 定时刷新 ──────────────────────────────────────────────────────────────────
# 菜单里"会变"的那些东西 —— 只有它变了才值得重建菜单。
_menu_fp: "tuple | None" = None


def _menu_fingerprint() -> tuple:
    """菜单内容的指纹（见 `_refresh` 里那段事故说明）。"""
    s = state.get()
    try:
        im = Config.load().input_method
    except Exception:                                # noqa: BLE001
        im = ""
    try:
        auto = _is_autostart()
    except Exception:                                # noqa: BLE001
        auto = False
    return (bool(s.connected), bool(s.streaming), s.device or "",
            s.last_event or "", im, bool(auto))


def _refresh(icon=None) -> None:
    """定时刷新托盘：图标 + 标题 + **只在内容变了时**重建菜单。

    ⚠⚠ 为什么菜单不能每次刷新都重建 —— 2026-10-09 真机事故
    -----------------------------------------------------
    用户原话：「我点退出没有反应，只能用任务管理器强制关闭。」

    pystray 的 Win32 后端显示菜单是这样的（`_win32.py::_on_notify`）：

        hmenu, descriptors = self._menu_handle
        index = TrackPopupMenuEx(hmenu, ..., TPM_RETURNCMD, ...)   # 阻塞
        if index > 0:
            descriptors[index - 1](self)     # ← index==0 时**静默什么都不做**

    而 `update_menu()` 的第一件事是 `DestroyMenu(旧句柄)`。
    老代码在这里**每 0.8 秒无条件**调一次 `update_menu()` ⇒ 菜单只要开着
    超过 0.8 秒，句柄就被销毁 ⇒ `TrackPopupMenuEx` 返回 0 ⇒ **点任何一项
    都毫无反应，而且一声不响**（没有异常、日志里一个字都没有）。

    py-spy 现场取证（2026-10-09 14:56，进程 PID 1356）：

        MainThread   停在 pystray `_mainloop`   （正常等消息）
        rvb-bridge   停在 asyncio `_poll`       （正常空转）

    ⇒ **没有任何线程在执行 `_quit`**，进程也没卡死。
    所以不是"退不掉"，是**那个回调根本没被调用**。

    ⇒ 菜单只在内容真的变了才重建；图标与标题照旧每 0.8 秒刷新
      （它们走 `NIM_MODIFY`，不碰菜单句柄）。
    """
    global _menu_fp
    ic = icon if icon is not None else _icon
    if ic is None:
        return
    s = state.get()
    try:
        ic.icon = _icon_image(connected=s.connected, streaming=s.streaming)
        ic.title = f"{APP_NAME} — {_status_text()}"
    except Exception:                                # noqa: BLE001
        pass
    try:
        fp = _menu_fingerprint()
    except Exception:                                # noqa: BLE001
        fp = None
    if fp != _menu_fp:
        _menu_fp = fp
        try:
            ic.update_menu()
        except Exception:                            # noqa: BLE001
            pass
    if not _stop.is_set():
        threading.Timer(0.8, _refresh).start()


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
        pystray.MenuItem("控制台", _open_console, default=True),
        pystray.MenuItem("输入法 / 语音触发键", pystray.Menu(*im_items)),
        pystray.MenuItem("重新连接", _request_reconnect),
        # 修复 / 诊断（2026-09-29 武哥建议）：出问题时右键就有，不用去翻目录。
        # ⚠ 子菜单里的顺序 = 出问题的排查顺序：连不上 → 先体检 → 再修；
        #   按键不灵 → 遥控器诊断。别把"修"排在"查"前面。
        pystray.MenuItem("修复 / 诊断", pystray.Menu(
            pystray.MenuItem("蓝牙配对体检（只读，不改动）", _pairing_check),
            pystray.MenuItem("蓝牙配对修复（换过 USB 口、连不上时跑这个）", _fix_pairing),
            pystray.MenuItem("遥控器诊断（按键没反应时跑这个）", _diag_remote),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开配对体检报告", _open_pairing_report),
            pystray.MenuItem("打开修复备份目录", _open_backup_dir),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机启动", _toggle_autostart,
                         checked=lambda item: _is_autostart()),
        pystray.MenuItem("打开配置文件夹", _open_config_dir),
        pystray.MenuItem("打开日志", _open_log),
        pystray.MenuItem("项目主页", lambda i, it: webbrowser.open(REPO_URL)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit),
    )


# ── 桥线程 ────────────────────────────────────────────────────────────────────
def _bridge_worker():
    import asyncio
    import logging
    from main import run_bridge

    # ⚠⚠ 这里原来是 print(f"[bridge] {e}") + traceback.print_exc()。
    #   主 exe 是 console=False（PyInstaller 不分配控制台窗口）——
    #   print 与 sys.stderr 的流向是空的，等于**把唯一的线索直接扔掉**。
    #   2026-09-15 真机事故就是栽在这：桥每 3 秒崩一次、桥日志里一行报错都没有、
    #   Windows 事件日志也查不到（异常是 Python 层的，不是进程崩溃），
    #   最后只能靠手写探针才把 OSError 挖出来。异常必须走 logger（→ bridge.log）。
    #   注：main 导入时已由 logsetup.install() 装好「轮转文件 + 控制台」两个
    #   handler（P2-6），这里拿同名 logger 即可。
    log = logging.getLogger("rvb")

    fails = 0
    while not _stop.is_set():
        try:
            # 把托盘的退出事件**交给** run_bridge：它每一轮都看，收到就走
            # 自己的 finally 去清理（P1-4）。只设 `_stop` 是不够的 ——
            # 桥线程看不到托盘模块里的那个事件。
            ok = asyncio.run(run_bridge(stop_event=_stop))
            fails = 0 if ok else fails + 1
        except Exception:  # noqa: BLE001
            fails += 1
            log.error("💥 桥线程异常退出（连续第 %d 次）", fails, exc_info=True)

        if _stop.is_set():
            break

        # 连续失败要退避：原来固定 3 秒，故障时每 3 秒把整段启动横幅刷一遍，
        # 4 分钟就把日志撑到 47 KB，真正的线索被淹没。
        #
        # ⚠ v1.0.28：第一档改用 `config.json` 里的 `reconnect_delay`。
        #   以前这里写死 3 秒，而 `main()` 里那个**没人用**的循环读的才是
        #   `cfg.reconnect_delay` —— 用户在设置里把重连延时从 5 改成 30，
        #   装的却是托盘版，**改了完全没反应**，而且日志上一点痕迹都没有。
        # ⚠ v1.0.29：退避策略抽成 `config.reconnect_backoff()`，两处共用 ——
        #   同一件事不再有两份实现（那正是上面那个坑的成因）。
        try:
            base = float(Config.load().reconnect_delay)
        except Exception:                            # noqa: BLE001
            base = 5.0
        delay = reconnect_backoff(fails, base)
        if fails and not state.get().last_event:
            # 不覆盖 run_bridge 写下的具体原因（如"配对记录已失效"），
            # 那比"启动失败"有用得多。
            state.update(last_event=f"启动失败，{delay:.0f} 秒后重试（第 {fails} 次）")
        time.sleep(delay)


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    global _icon, _bridge_thread
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _enable_dpi_awareness()

    _bridge_thread = threading.Thread(target=_bridge_worker, daemon=True,
                                      name="rvb-bridge")
    _bridge_thread.start()

    _icon = pystray.Icon(APP_NAME, _icon_image(),
                         f"{APP_NAME} v{APP_VERSION}（启动中…）")
    _icon.menu = build_menu(_icon)

    # 刷新是**模块级**函数（见 `_refresh` 的事故说明）—— 这样闸门能直接调它，
    # 不用去 main() 的闭包里捞。Timer 会无参调用它，所以它自己取全局 `_icon`。
    threading.Timer(0.8, _refresh).start()

    # 首次启动自动弹一次控制台：装完就能看见界面，
    # 不用去翻右键菜单 —— 第一次用的人根本不知道托盘里有什么。
    if os.environ.get("RVB_NO_AUTO_CONSOLE") != "1":
        threading.Timer(2.0, _open_console).start()

    _icon.run()


if __name__ == "__main__":
    main()
