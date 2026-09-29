@echo off
chcp 936 >nul
title 已退役：0x1812 接管测试（什么都不会做）
cd /d "%~dp0"

echo ==========================================================================
echo   这个入口已退役（v1.0.22 起）—— 它什么都不会做，不会提权、不会动设备。
echo ==========================================================================
echo.
echo  它以前做的事：弹一次 UAC，强行结束 RemoteVoiceBridge.exe，再把 Windows
echo  的 HOGP 设备节点临时禁用约 75 秒，看遥控器的按键报告到底发不发。
echo.
echo  这个问题 v1.0.20 已经查清了，而且这条路本身被 Windows 封死：
echo.
echo    · 那个 devnode 的 DevNodeStatus 里**没有 DN_DISABLEABLE 位**
echo      （2026-09-22 真机实测 0x0180000A），设备管理器里「禁用设备」也是灰的
echo      —— 用户态没有任何办法禁掉它。
echo    · 所以它每跑一次，最后必然打印 DISABLE_FAIL。那不是「没测成」，
echo      更不能当成「遥控器不发按键」的证据。
echo.
echo  而「按键怎么拿到」已经解决了：报告**不是没发**，是在 WUDFHost.exe 内部
echo  就被 UMDF 驱动消费掉了。v1.0.20 起用 Frida 注入那个宿主，在 IOCTL 的
echo  输出缓冲区上抄一份 —— 真机已验证（90 秒收到 120 条报告、14 个键一个不缺）。
echo.
echo  → 退役理由：它**要管理员**、会**强杀桥程序**、还会去动系统设备状态，
echo    而它想回答的问题已经有答案了。留着它只有风险没有收益。
echo.
echo  文件为什么还在这儿：给「记得有这个 bat」的人一个明确去处，
echo  免得他绕过它去直接跑 --go。
echo.
echo  ---- 历史取证（只读，一个字节都不改）----
echo     python tools\takeover_hid_reports.py     不带 --go 就是纯只读体检
echo     完整判读说明：tools\README.md 里 takeover_hid_reports.py 那一行
echo.
pause
exit /b 0
