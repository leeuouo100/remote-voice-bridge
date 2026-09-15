@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

REM ── 蓝牙配对自检 / 修复 ──────────────────────────────────────────────────────
REM  专治：换过 USB 口之后程序显示「未连接」，而 Windows 显示「已配对」，
REM        而且设置里还删不掉这个设备（一直卡在「正在删除设备」）。
REM
REM  根因：配对记录是按**本地蓝牙地址**存的，而这颗蓝牙棒的地址是按
REM        「插在哪个口」缓存的 —— 换个口地址就变，旧记录就作废了。
REM        于是"能枚举到、显示已配对、就是打不开"（E_INVALIDARG）。
REM
REM  本脚本**两个环境通吃**：
REM    · 安装版（本文件在安装目录里，旁边有 RemoteVoiceBridgeDiag.exe）
REM      → 跑那个 exe，不依赖 Python
REM    · 源码版（旁边有 pairing.py）
REM      → 跑 python pairing.py
REM
REM  流程：先出只读诊断 → 发现问题就弹 UAC → 提权后备份并修复 → 真机验收。
REM  纯只读（不改任何东西）请直接跑：  python pairing.py
REM
REM  产出 %APPDATA%\remote-voice-bridge\pairing-fix.txt

echo.
echo  ╔════════════════════════════════════════════════════════════╗
echo  ║  蓝牙配对自检与修复                                        ║
echo  ║                                                            ║
echo  ║  先诊断；确认要修的话会弹一个 UAC 授权框，请点「是」。      ║
echo  ║  动注册表之前会自动备份到：                                ║
echo  ║     %%APPDATA%%\remote-voice-bridge\backup\                ║
echo  ╚════════════════════════════════════════════════════════════╝
echo.

if exist "%~dp0RemoteVoiceBridgeDiag.exe" (
    "%~dp0RemoteVoiceBridgeDiag.exe" --fix-pairing %*
    echo.
    pause
    exit /b
)

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

%PY% -c "import winreg" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] 当前 Python 跑不起来。先跑一次 run.bat 让它把依赖装好。
    echo.
    pause & exit /b 1
)

%PY% pairing.py --fix-pairing %*

echo.
pause
