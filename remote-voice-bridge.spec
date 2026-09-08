# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 生成 dist/RemoteVoiceBridge/ （onedir + GUI）"""

from PyInstaller.utils.hooks import collect_all

# winrt 命名空间由多个 wheel 拼装（winrt-runtime + winrt-Windows.*），
# PyInstaller 的静态分析抓不全，必须整包收集；否则打包后运行直接
# ModuleNotFoundError: winrt.windows.devices.bluetooth
winrt_datas, winrt_binaries, winrt_hidden = collect_all('winrt')

a = Analysis(
    ['tray_app.py'],
    pathex=[],
    binaries=winrt_binaries,
    datas=winrt_datas,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name='RemoteVoiceBridge',
)
