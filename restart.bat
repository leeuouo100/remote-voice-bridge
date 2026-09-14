@echo off
REM ---------------------------------------------------------------------------
REM 重启本程序（从源码运行时用；装了安装包的话用托盘右键「重新连接」即可）
REM
REM ⚠️ 旧版这里写的是 taskkill /f /im python.exe —— 那会把电脑上**所有** Python
REM 程序一起杀掉（别的项目、别的服务都遭殃）。现在改成只结束命令行里带
REM tray_app.py / RemoteVoiceBridge 的进程，也就是本程序自己。
REM ---------------------------------------------------------------------------
cd /d "%~dp0"

powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' Or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -and ($_.CommandLine -like '*tray_app.py*' -or $_.CommandLine -like '*RemoteVoiceBridge*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

timeout /t 1 /nobreak >nul
echo Restarting Remote Voice Bridge...
start "" "%~dp0run.bat"
