@echo off
cd /d "%~dp0"
setlocal

REM ── 遥控器真机诊断 ────────────────────────────────────────────────────────────
REM  让程序把现象测出来：遥控器每个键到底发出什么、跟物理键盘能不能区分。
REM  产出 %APPDATA%\remote-voice-bridge\remote-diag.txt

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

%PY% -c "import keyboard" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] 当前 Python 里没有 keyboard 库。
    echo          先跑一次 run.bat 让它把依赖装好，或手动执行：
    echo              %PY% -m pip install -r requirements.txt
    echo.
    pause & exit /b 1
)

echo.
echo  ╔════════════════════════════════════════════════════════════╗
echo  ║  开始前请确认：桥接程序【已经退出】（托盘右键 → 退出）     ║
echo  ║  它开着的话，它注入的按键也会被记进来，报告就不准了。      ║
echo  ╚════════════════════════════════════════════════════════════╝
echo.

%PY% tools\diag_remote.py

echo.
pause
