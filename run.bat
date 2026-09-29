@echo off
chcp 936 >nul
cd /d "%~dp0"

REM ── 本文件是 UTF-8 字节，所以必须自己 chcp 936 ─────────────────────────────
REM    2026-09-23 补：以前没写，而下面有十几行中文 echo + 框线，
REM    中文 Windows 的 cmd 默认 936，那些字全是乱码。
REM    [!] 不要因为"中文 Windows 就用 GBK"把它转编码 —— 文件自己的 chcp 才是准的。
REM
REM ── 2026-09-29（审查报告第六节）三处整改 ────────────────────────────────────
REM    ① 入口从 `main.py` 改成 **`tray_app.py`**：直接跑 main.py 没有托盘、也没有
REM       「退出」这个正常收尾入口 —— 用户只能关控制台窗口，而那是硬杀进程，
REM       GattSession / Frida 注入 / 音频流都不会被清理（现象是"退出再开连不上"）。
REM    ② 框里那句「系统默认麦克风已设为 CABLE Input」**是错的**：VB-CABLE 的两个
REM       端点命名是反的 —— `CABLE Input` 是**播放**端（本程序往里写），
REM       `CABLE Output` 才是**录音**端（输入法/系统默认麦克风要读的那只）。
REM       按错的设，读到的是"没有任何东西在写的那个端点"，症状就是"完全没声音"。
REM    ③ pip 的输出以前用 `>nul 2>&1` 整个吞掉、也不看退出码 —— 装不上时脚本
REM       照样往下跑，最后崩在一个和"依赖没装上"毫无关系的 ImportError 上。
REM       现在落盘到 pip-install.log，失败就打印尾巴并**停下来**。
REM ── Check Python ──────────────────────────────────────────────────────────────
REM    [!] 支持范围是 **Python 3.10 / 3.11**（唯一真源在 config.py 的 PY_MIN/PY_MAX，
REM      这里这个区间由 tools\check_ci_workflow.py 盯着不许漂）。
REM      不先说清的话，3.13 用户会一路走到 `pip install` 才失败，而失败原因
REM      （numpy==1.24.3 没有 3.13 的 wheel）和"程序跑不起来"看起来毫无关系。
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 没找到 Python。本程序源码版需要 Python 3.10 / 3.11。
    echo         装一个 3.11: https://www.python.org/downloads/release/python-3119/
    echo         （或者直接用安装版 —— 那个自带运行时，不需要 Python。）
    pause & exit /b 1
)
python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,11) else 1)"
if errorlevel 1 (
    echo.
    echo [ERROR] 本版本只支持 Python 3.10 / 3.11，你当前是：
    python --version
    echo.
    echo         为什么：requirements.txt 里 numpy==1.24.3 在 3.12/3.13 上没有
    echo         预编译包，装不上。要么换 3.11，要么用安装版（自带运行时）。
    pause & exit /b 1
)

REM ── Virtual env ───────────────────────────────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] 建 .venv 失败（看上面的输出）。
        pause & exit /b 1
    )
)

echo [INFO] Installing dependencies ... (完整输出 -> pip-install.log)
.venv\Scripts\python.exe -m pip install -r requirements.txt > pip-install.log 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] 依赖没装上 —— 已停下，不会带着残缺环境往下跑。
    echo         完整日志: %~dp0pip-install.log
    echo         最后 25 行:
    echo ---------------------------------------------------------------
    powershell -NoProfile -Command "Get-Content -Tail 25 'pip-install.log'" 2>nul
    echo ---------------------------------------------------------------
    pause & exit /b 1
)

echo.
echo ╔══════════════════════════════════════════════════════╗
echo ║  remote-voice-bridge 启动中...                        ║
echo ║                                                      ║
echo ║  前提:                                               ║
echo ║    1. VB-CABLE 已安装 (https://vb-audio.com/Cable/)  ║
echo ║    2. 遥控器已在 Windows 蓝牙设置中配对              ║
echo ║    3. 系统默认麦克风已设为 CABLE Output              ║
echo ║       （注意是 Output —— 那是录音端）                ║
echo ║                                                      ║
echo ║  退出：托盘图标右键 → 退出（这样才会优雅收尾）       ║
echo ║        关掉本窗口是硬杀进程，不推荐                  ║
echo ╚══════════════════════════════════════════════════════╝
echo.

REM 走托盘入口（不要改成 main.py —— 那样没有托盘和正常退出）。
.venv\Scripts\python.exe tray_app.py

if errorlevel 1 (
    echo.
    echo [ERROR] 桥程序异常退出。日志: %%APPDATA%%\remote-voice-bridge\bridge.log
    pause
)
