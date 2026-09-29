@echo off
chcp 936 >nul
cd /d "%~dp0"
setlocal

REM ── 语音热键诊断：到底哪组键能唤起微信输入法 ────────────────────────
REM  桥程序自己判断不了「输入法认不认这组键」—— 键盘钩子能看到的事件，
REM  和输入法真正判定的条件不是一回事。所以只能人眼实测。
REM
REM  [!] 编码必须是 GBK + CRLF + 无 BOM：中文 Windows 的 cmd 读 UTF-8
REM      批处理会把中文全打成乱码（加 chcp 65001 也救不回来）。

set PY=
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\rvb\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY set PY=python

echo.
echo  ================================================================
echo   语音热键诊断
echo  ================================================================
echo.
echo  【第 0 步】先看一眼输入法自己的面板 —— 这一步不用脚本，但最重要
echo.
echo    打开 微信输入法 设置 - 语音输入，把面板上写着的键位抄下来：
echo        「按住说话」     = ？
echo        「启动语音输入」 = ？
echo.
echo    面板里的键是可以被改、也会被升级重置的。
echo    面板上写的是什么，才是最终答案；下面三组只是用来对答案。
echo.
pause

echo.
echo  ----------------------------------------------------------------
echo  【第 1 组】左Ctrl + 左Win + 左Shift，点按一次（切换式）
echo              ^<-- 桥程序现在用的就是这一组
echo              ^<-- 倒计时 5 秒内切到记事本，把光标点进输入区
echo  ----------------------------------------------------------------
echo.
%PY% tools\test_voice_hotkey.py lctrl lwin lshift --tap

echo.
echo  ----------------------------------------------------------------
echo  【第 2 组】Ctrl + Win，按住 6 秒（按住说话）
echo              ^<-- 倒计时 5 秒内切到记事本，按住不放
echo  ----------------------------------------------------------------
echo.
%PY% tools\test_voice_hotkey.py ctrl win

echo.
echo  ----------------------------------------------------------------
echo  【第 3 组】右 Alt，按住 6 秒（注入路径最干净的一组）
echo  ----------------------------------------------------------------
echo.
%PY% tools\test_voice_hotkey.py ralt

echo.
echo  ================================================================
echo   三组试完了。
echo.
echo   * 哪一组能让输入法弹出录音界面  -> 就用哪一组，告诉我，我改配置
echo   * 三组都不弹                   -> 输入法把语音快捷键改了或关了，
echo                                     去面板里改回来（或告诉我面板写的是什么）
echo  ================================================================
pause
