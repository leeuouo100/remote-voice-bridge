@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

REM ── 遥控器真机诊断 ────────────────────────────────────────────────────────────
REM  让程序把现象测出来：遥控器每个键到底发出什么、跟物理键盘能不能区分。
REM  产出 %APPDATA%\remote-voice-bridge\remote-diag.txt

REM ⚠ 2026-09-23 修的两处，别改回去：
REM   · 本文件是 **UTF-8 字节**，所以必须自己 chcp 65001。以前没写，
REM     中文 Windows 的 cmd 默认 936，下面那些中文 echo / 框线全是乱码。
REM   · 找 Python 的顺序要和「测遥控器按键通道.bat」「修复蓝牙配对.bat」**完全一致**：
REM     本机**没有** .venv；PATH 上那个 python 也没装项目依赖（bleak/winsdk），
REM     唯一装齐的是 WorkBuddy 托管环境里那个 ⇒ 必须排在第一个。
set PY=
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY set PY=python

%PY% -c "import keyboard" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] 当前 Python 里没有 keyboard 库。
    echo          试过：%%USERPROFILE%%\.workbuddy\binaries\python\envs\rvb\ 、.venv\ 、PATH 上的 python
    echo          都不行。先跑一次 run.bat 把依赖装好，或手动执行：
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
