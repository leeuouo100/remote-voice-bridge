@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ── 本文件是 UTF-8 字节，所以必须自己 chcp 65001 ─────────────────────────────
REM    2026-09-23 补：以前没写，而下面有十几行中文 echo + 框线，
REM    中文 Windows 的 cmd 默认 936，那些字全是乱码。
REM    ⚠ 不要因为"中文 Windows 就用 GBK"把它转编码 —— 文件自己的 chcp 才是准的。
REM ── Check Python ──────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10+ not found. Install from https://python.org
    pause & exit /b 1
)

REM ── Virtual env ───────────────────────────────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating .venv ...
    python -m venv .venv
)

echo [INFO] Installing dependencies...
.venv\Scripts\pip.exe install -r requirements.txt >nul 2>&1

echo.
echo ╔══════════════════════════════════════════════════════╗
echo ║  remote-voice-bridge 启动中...                        ║
echo ║                                                      ║
echo ║  前提:                                               ║
echo ║    1. VB-CABLE 已安装 (https://vb-audio.com/Cable/)  ║
echo ║    2. 遥控器已在 Windows 蓝牙设置中配对              ║
echo ║    3. 系统默认麦克风已设为 CABLE Input               ║
echo ║                                                      ║
echo ║  按 Ctrl+C 停止                                      ║
echo ╚══════════════════════════════════════════════════════╝
echo.

.venv\Scripts\pythonw.exe main.py %*

if errorlevel 1 (
    echo.
    echo [ERROR] Bridge exited with error. Check bridge.log
    pause
)
