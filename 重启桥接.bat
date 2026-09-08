@echo off
cd /d "%~dp0"
taskkill /f /im pythonw.exe >nul 2>&1
taskkill /f /im python.exe   >nul 2>&1
timeout /t 1 /nobreak >nul
echo Restarting...
start "" "%~dp0run.bat"
