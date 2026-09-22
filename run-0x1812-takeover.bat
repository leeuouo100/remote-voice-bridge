@echo off
chcp 65001 >nul
title 0x1812 takeover test
cd /d "%~dp0"

net session >nul 2>&1
if not errorlevel 1 goto elevated

echo.
echo  [*] Need administrator rights - a UAC prompt will show up.
echo      Click YES, then a new window appears with the real thing.
echo.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:elevated
echo ==========================================================================
echo   0x1812 TAKEOVER TEST - does the remote send ANY HID button report?
echo ==========================================================================
echo.
echo  [1/3] Closing RemoteVoiceBridge.exe so it lets go of the remote ...
taskkill /F /IM RemoteVoiceBridge.exe >nul 2>&1
echo        done.
echo.
echo  [2/3] Next, the tool will temporarily DISABLE the HID node (~75 sec),
echo        subscribe to Report(0x2A4D), listen for 60 sec, and then
echo        AUTOMATICALLY RE-ENABLE the node. Nothing to do here - just read.
echo.
echo  [3/3] *** THE ONLY THING YOU MUST DO ***
echo        When the tool prints the countdown line (it says "60" and lists
echo        the buttons), press ONE BY ONE on the REMOTE:
echo.
echo            D-pad up, down, left, right, OK, Back, Home, Vol+, Vol-, Mute
echo.
echo        DO NOT TOUCH THE KEYBOARD OR THE MOUSE during those 60 seconds.
echo.
pause
echo.
echo  ---- running ----
echo.
set PY=
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY set PY=python
"%PY%" "tools\takeover_hid_reports.py" --go --seconds 60
echo.
echo  ==========================================================================
echo   Finished. Full report:
echo     %APPDATA%\remote-voice-bridge\takeover-hid-reports.txt
echo   Start RemoteVoiceBridge again to get voice input back.
echo  ==========================================================================
pause
