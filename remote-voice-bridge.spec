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

a = Analysis(
    ['tray_app.py'],
    pathex=[SPEC_DIR],
    binaries=winrt_binaries,
    datas=winrt_datas + ui_datas,
    hiddenimports=winrt_hidden + [
        'winrt.runtime',
        'keyboard',
        'sounddevice',
        'numpy',
        'pystray._win32',
        'tkinter',
        'tkinter.scrolledtext',
        'tkinter.ttk',
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
diag = Analysis(
    ['tools/diag_remote.py'],
    pathex=[SPEC_DIR],
    binaries=[],
    datas=[],
    hiddenimports=['keyboard'],
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
