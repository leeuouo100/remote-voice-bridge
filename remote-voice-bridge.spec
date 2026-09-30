# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 生成 dist/RemoteVoiceBridge/ （onedir）

产出**两个** exe：

  · RemoteVoiceBridge.exe       主程序（无控制台窗口，托盘运行）
  · RemoteVoiceBridgeDiag.exe   遥控器诊断（有控制台窗口）

⚠ 诊断工具必须和主程序一起打包。安装版用户机器上通常**没有 Python**，
  仓库根目录那个 diag-remote.bat 对他们等于不存在 —— v1.0.5 发出去之后
  才发现这个洞（当时只在源码环境下测过），所以这里补上。
"""

import os

from PyInstaller.utils.hooks import collect_all

# SPECPATH 由 PyInstaller 注入 = 本 spec 所在目录（仓库根）。
# 两个 Analysis 都显式带上它：诊断脚本在 tools/ 子目录里，入口脚本所在目录
# 是 tools/ 而不是根，不显式给的话 `from config import ...` 会解析失败。
SPEC_DIR = os.path.abspath(SPECPATH)  # noqa: F821

# winrt 命名空间由多个 wheel 拼装（winrt-runtime + winrt-Windows.*），
# PyInstaller 的静态分析抓不全，必须整包收集；否则打包后运行直接
# ModuleNotFoundError: winrt.windows.devices.bluetooth
winrt_datas, winrt_binaries, winrt_hidden = collect_all('winrt')

# 控制台的前端资源（HTML/CSS/JS）必须打进包里：
# 打包后它们不在源码目录，console_server.ui_dir() 会去 sys._MEIPASS/ui 找。
# 漏掉这一步的表现是"控制台打开一片白 + 404"。
ui_datas = [('ui', 'ui')]

# Frida 注入脚本（按键旁路用）：frida_hid.py 读它、喂给 frida。漏了的表现是
# 「语音能用、除语音键外的按键一个都不灵」—— 同 remote_hid 那条路的坑。
tap_datas = [('frida_tap.js', '.')]

a = Analysis(
    ['tray_app.py'],
    pathex=[SPEC_DIR],
    binaries=winrt_binaries,
    datas=winrt_datas + ui_datas + tap_datas,
    hiddenimports=winrt_hidden + [
        'winrt.runtime',
        'keyboard',
        'sounddevice',
        'numpy',
        'pystray._win32',
        'tkinter',
        'tkinter.scrolledtext',
        'tkinter.ttk',
        # 厂商页按键解码（v1.0.11 起按键映射全靠它）。
        # main.py 里是在 run_bridge 内部 `import remote_hid` 的（为了它挂了
        # 也不拖垮语音），显式列一遍更保险 —— 漏了的表现是「语音能用、
        # 其它按键一个都不灵」，而且日志里只有一行 warning，极难往这上面想。
        'remote_hid', 'hidinfo', 'hidwatch',
        # 按键旁路（v1.0.20）：注入 WUDFHost 从 HID 驱动内部抄报告 ——
        # **真机上唯一能拿到遥控器按键的那一路**。同样是 run_bridge 内部
        # `import frida_hid`，显式列一遍；frida 本身也列上（装不上时
        # PyInstaller 只给警告，运行时 frida_hid 会优雅降级）。
        'frida_hid', 'frida',
        # 「遥控器优先」（v1.0.26）：把系统默认**录音**设备钉在 CABLE Output。
        # 同样是 main.py 里**函数内部** `import audiodefault`（为了非 Windows /
        # 精简环境下 import 失败也不拖垮桥），显式列一遍更保险 ——
        # 漏了的表现是「插上别的麦克风后输入法又听不到了」，而日志里
        # 只有一行 debug，极难往这上面想。
        'audiodefault',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pyinstaller', 'pytest'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RemoteVoiceBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # GUI 应用：不弹黑色控制台窗口
    icon='app.ico',
)

# ── 第二个入口：遥控器诊断工具 ──────────────────────────────────────────
# 它也承担「蓝牙配对自检 / 修复」（--fix-pairing → pairing.py）。
# 用户机器上没有 Python，所以修复能力必须跟着这个 exe 一起进包，
# 否则「换 USB 口连不上」的用户拿不到救命的那个工具（v1.0.5 的教训）。
diag = Analysis(
    ['tools/diag_remote.py'],
    pathex=[SPEC_DIR],
    binaries=winrt_binaries,
    datas=winrt_datas,
    # hidinfo / hidwatch 显式列出来：它们在 diag_remote 里是**包在
    # try/except ImportError 里** import 的（为了打包漏模块时老功能还能用）。
    # PyInstaller 对 try/except 里的 import 只给警告、有时会漏收，
    # 漏了的表现就是"报告里没有 HID 那两段" —— 正好把最关键的结论弄丢。
    #
    # pairing / winrt：同理显式加。pairing 是 --fix-pairing 的实现；
    # winrt 是它做真机验收（真的把 BLE 设备打开一次）用的 ——
    # 缺了 winrt 不会崩（pairing 会老实报 SKIPPED），但验收就退化成
    # "只验地址对不对"，所以还是带上。
    hiddenimports=['keyboard', 'hidinfo', 'hidwatch', 'pairing']
                  + winrt_hidden + ['winrt.runtime'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pyinstaller', 'pytest'],
    noarchive=False,
)

diag_pyz = PYZ(diag.pure)

diag_exe = EXE(
    diag_pyz,
    diag.scripts,
    [],
    exclude_binaries=True,
    name='RemoteVoiceBridgeDiag',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,           # 命令行交互，必须有控制台窗口
    icon='app.ico',
)

coll = COLLECT(
    exe,
    diag_exe,
    a.binaries,
    a.datas,
    diag.binaries,
    diag.datas,
    strip=False,
    upx=True,
    name='RemoteVoiceBridge',
)
