@echo off
chcp 936 >nul
cd /d "%~dp0"
setlocal

REM ── 遥控器按键通道监测 ────────────────────────────────────────────────
REM  一次按键，看它到底落在哪条通道；并分清「真实按键」与「本程序自己注入的按键」。
REM  产出 %APPDATA%\remote-voice-bridge\watch-all-channels.txt
REM
REM  [!] 编码必须是 GBK + CRLF + 无 BOM：中文 Windows 的 cmd 读 UTF-8 批处理
REM      会把中文全打成乱码（加 chcp 65001 也救不回来）。
REM      顺带一条：有些符号（警告三角、对勾这类）GBK 里根本没有，
REM      写进去 .encode('gbk') 会直接抛异常 —— 用 [OK] / [!] 代替最稳。

REM 找解释器：这台机器上装了依赖的是 workbuddy 的 rvb 环境。
set PY=
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY set PY=python

echo.
echo  ================================================================
echo   遥控器按键通道监测
echo  ================================================================
echo.

tasklist /FI "IMAGENAME eq RemoteVoiceBridge.exe" /NH | find /I "RemoteVoiceBridge.exe" >nul
if errorlevel 1 goto ready

echo  [!] 桥程序正在运行。它占着蓝牙，会让私有服务那两路听不到。
echo.
choice /C YN /M "  现在替你退出它吗"
if errorlevel 2 goto manual
taskkill /IM RemoteVoiceBridge.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul
echo  [OK] 已退出。
goto ready

:manual
echo.
echo  请到托盘右键「退出」，完成后按任意键继续。
pause >nul

:ready
echo.
echo  接下来 45 秒：
echo    1) 手【不要碰键盘】—— 一碰就会混进物理键盘的按键
echo    2) 只按遥控器，依次按：方向上下左右 -^> 确认 -^> 返回 -^> 主页
echo       -^> 音量＋ -^> 音量－ -^> 静音 -^> 语音键
echo.
pause

%PY% tools\watch_all_channels.py --seconds 45
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" echo  [!] 脚本退出码 %RC%
echo  报告已生成： %APPDATA%\remote-voice-bridge\watch-all-channels.txt
echo  把这个文件发出来即可。
echo.
pause
