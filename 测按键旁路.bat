@echo off
chcp 936 >nul
cd /d "%~dp0"
setlocal

REM ── 遥控器按键旁路测试 ────────────────────────────────────────────────
REM  注入蓝牙驱动宿主（WUDFHost）读 HID 报告 —— 真机上唯一能拿到遥控器
REM  按键的那一路（自开厂商页那条实测一直是 0 条）。
REM
REM  [!] 编码必须是 GBK + CRLF + 无 BOM：中文 Windows 的 cmd 读 UTF-8 批处理
REM      会把中文全打成乱码（加 chcp 65001 也救不回来）。
REM      符号用 [OK] / [!] 代替 —— 警告三角、对勾这类 GBK 里没有。

REM 找解释器：这台机器上装了依赖的是 workbuddy 的 rvb 环境。
set PY=
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY set PY=python

echo.
echo  ================================================================
echo   遥控器按键旁路测试（注入 WUDFHost 读 HID 报告）
echo  ================================================================
echo.
echo  跑之前请确认遥控器已经连上（按一下遥控器任意键把它唤醒）。
echo.
echo  接下来约 90 秒，请按遥控器上的 方向 / 确认 / 返回 / 音量 键。
echo  每收到一次按键，屏幕会打印一行「[键名] 按下 / 松开」。
echo.
echo  [!] 第一次运行可能弹一次「是否允许此应用对你的设备进行更改」
echo      —— 那是注入需要的管理员授权，点「是」；只弹这一次。
echo.
echo  [!] 如果一行按键都没打印，请把屏幕上的「自检」那一行发出来。
echo.
pause

%PY% tools\check_frida_tap.py --watch 90
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" echo  [!] 脚本退出码 %RC%
echo.
pause
