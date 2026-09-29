"""跑一遍全部静态校验，把结果汇总到一个 UTF-8 报告里（供 CI / 本地一键验收）。

用法：
    python tools/check_all.py           本地全量（含真实按键注入）
    python tools/check_all.py --ci      **CI 安全集**：排除需要真机 / 交互桌面的那几道，
                                        其余全部必跑；且**必需项 SKIP 一律算失败**

为什么要有 `--ci`（2026-09-29 审查报告 P1-10）：
    老工作流只挑了十来道闸手动列一遍，剩下的"本地绿、CI 不跑" —— 而 CI 才是
    "用户能不能拿到这个包"的唯一守门人。两份清单还会各自漂。现在只有**一份**清单
    （下面这个 STEPS），CI 用 `--ci` 跑同一份，只是把"需要真人按键 / 交互桌面"
    的那几道显式摘出来，并在结尾**点名报出没跑的是哪几道**（"没跑"不许被读成"过了"）。

    同时把"跳过"升格成**结构化结论**：脚本打印 `SKIPPED` 就是 SKIP，
    SKIP 的必需项按 FAIL 计。原先 `smoke_console.py` 之类对任意 OSError
    都以 SKIP/成功退出 ⇒ 环境一坏，整道闸静默变成永远绿的摆设。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

from _utf8 import setup as _setup_utf8  # 下面是中文输出，先钉住编码（CI 是 cp1252）

_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def _find_node() -> str | None:
    """找 node —— 前端的"真实浏览器"那两道闸要用它。

    先看 PATH（CI 的 runner 自带），再找本机托管的那个版本。
    """
    n = shutil.which("node")
    if n:
        return n
    base = os.path.join(os.path.expanduser("~"), ".workbuddy-ai",
                        "binaries", "node", "versions")
    if os.path.isdir(base):
        for d in sorted(os.listdir(base), reverse=True):
            exe = os.path.join(base, d, "node.exe")
            if os.path.isfile(exe):
                return exe
    return None


NODE = _find_node()


def _find_npm_global() -> str | None:
    """npm 的**全局** node_modules —— Node 的 require 解析不到它，只能靠 NODE_PATH。

    ⚠ 不能写死 `%APPDATA%\\npm\\node_modules`：那是**本机** npm 的默认前缀。
      GitHub 的 windows runner 把全局前缀设成 `C:\\npm\\prefix`，写死的话
      CI 上 `require('playwright')` 直接抛 Cannot find module ⇒ 那道闸打
      SKIPPED ⇒ 被"必需项不许跳过"判 FAIL ⇒ 整轮构建挂掉
      （2026-09-29：v1.0.23 第一次 CI 就是这么红的）。
      所以**先问 npm 自己**（`npm root -g`），再退回几个已知位置。
    """
    cands: list[str] = []
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        try:
            p = subprocess.run([npm, "root", "-g"], capture_output=True,
                               text=True, encoding="utf-8",
                               errors="replace", timeout=60)
            if p.returncode == 0 and (p.stdout or "").strip():
                cands.append(p.stdout.strip().splitlines()[-1].strip())
        except Exception:
            pass
    home = os.path.expanduser("~")
    cands += [
        os.path.join(home, "AppData", "Roaming", "npm", "node_modules"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "nodejs", "node_modules"),
        r"C:\npm\prefix\node_modules",
        os.path.join(home, "node_modules"),
        "/usr/lib/node_modules",
        "/usr/local/lib/node_modules",
    ]
    seen: set[str] = set()
    uniq = [c for c in cands if c and not (c in seen or seen.add(c))]
    # 优先"目录里真有 playwright"的那个 —— 只有 PATH 上第一个存在的目录
    # 是不够的（可能是个空的 node_modules）。
    for d in uniq:
        if os.path.isdir(os.path.join(d, "playwright")):
            return d
    for d in uniq:
        if os.path.isdir(d):
            return d
    return None


# playwright 装在哪：npm 全局目录（Node 的 require 解析不到，得靠 NODE_PATH）
_NPM_GLOBAL = _find_npm_global()
if _NPM_GLOBAL:
    os.environ["NODE_PATH"] = (_NPM_GLOBAL + os.pathsep
                               + os.environ.get("NODE_PATH", "")).rstrip(os.pathsep)

STEPS = [
    ("编译全部模块", [PY, "-m", "py_compile",
                  "config.py", "state.py", "keys.py", "session.py", "buttons.py",
                  "mixer.py", "main.py", "console_server.py", "tray_app.py",
                  "hidinfo.py", "hidwatch.py", "pairing.py", "remote_hid.py",
                  "frida_hid.py", "logsetup.py",
                  "tools/_utf8.py", "tools/_bat.py",
                  "tools/check_version.py", "tools/check_keymap.py",
                  "tools/smoke_console.py", "tools/test_recorder.py",
                  "tools/check_ble_callback_thread.py",
                  "tools/diag_remote.py", "tools/check_packaging.py",
                  "tools/check_levels.py", "tools/check_mix_persist.py",
                  "tools/watch_reports.py", "tools/check_hidinfo.py",
                  "tools/check_failure_visibility.py", "tools/check_pairing.py",
                  "tools/check_remote_hid.py", "tools/check_injection.py",
                  "tools/check_frida_tap.py",
                  "tools/check_audio_watchdog.py",
                  "tools/check_send_after_voice.py",
                  "tools/dump_report_descriptor.py",
                  "tools/probe_gatt_hid.py", "tools/probe_hid_claim.py",
                  "tools/watch_all_channels.py",
                  "tools/probe_devnode_binding.py",
                  "tools/probe_hogp_state.py",
                  "tools/takeover_hid_reports.py",
                  "tools/check_takeover_guard.py",
                  "tools/check_voice_session.py",
                  "tools/check_ci_workflow.py",
                  "tools/check_run_bat.py",
                  "tools/check_licenses.py",
                  "tools/check_logging.py",
                  "tools/check_audio_rate.py",
                  "tools/make_deps_lock.py",
                  "tools/check_all.py"]),
    ("版本号一致性", [PY, "tools/check_version.py"]),
    # spec / installer.iss 的一致性：诊断工具必须真的被打进安装包。
    # v1.0.5 就是漏了这一步 —— 工具写好了、也发了版，但安装版用户拿不到
    # （机器上没 Python，仓库里的 .bat 跑不起来）。纯静态，几毫秒。
    # v1.0.20 起还管按键旁路的两个文件（frida_tap.js / frida_hid）——
    # 漏收它们同样是"语音正常、按键全不灵"的静默洞。
    ("打包入口一致性", [PY, "tools/check_packaging.py"]),
    ("打包一致性闸的自检", [PY, "tools/check_packaging.py", "--selftest"]),
    # 三路电平/波形"有消费者、没有生产者"的洞：v1.0.7 真机上「遥控器麦克风」
    # 波形在动、状态却永远卡在「等待语音」，根因就是**没有任何产品代码喂**
    # state.remote_level_db。纯静态 + 几行真跑 state 模块，带反例自检。
    ("电平生产者一致性", [PY, "tools/check_levels.py"]),
    # 「界面上关了、后端还在用」—— 音频页的开关以前只改内存、不落盘，
    # 设置页一动或重连就被 config.json 悄悄回滚。这是最难查的一类 bug：
    # 一点声音都没有，用户只会说"它自己不听话"。沙箱 APPDATA，不碰真配置。
    ("混音开关落盘", [PY, "tools/check_mix_persist.py"]),
    # 配置读改写的并发保护与原子写（2026-09-29 审查报告 P1-5）。
    # 控制台是 ThreadingHTTPServer：多个请求各读各的旧快照再写回 → 丢配置；
    # save() 直接截断写 → 并发读拿到半截 JSON → 回退默认 → 用户设置一次全没。
    # 顺带钉住 Windows 上 os.replace 与并发读抢句柄的问题（必须退避重试）。
    ("配置原子写与并发", [PY, "tools/check_config_atomic.py"]),
    # 托盘「修复 / 诊断」菜单（2026-09-29 武哥的建议）。
    # 静态断言 + **真的 build 一次菜单**：菜单项指向的函数改名、工具文件名
    # 写错、传了个工具不认的开关、用 subprocess.run 把托盘线程卡死 ——
    # 这四种洞用户看到的都只是「点了没反应」，日志里一个字都没有。
    ("托盘修复菜单", [PY, "tools/check_tray_tools.py"]),
    # 控制台鉴权与请求边界（2026-09-29 审查报告 P1-1）。
    # 绑 127.0.0.1 只挡外网，挡不住同机进程和浏览器里的网页 —— 随机端口是概率、
    # 不是鉴权。裸 socket 验精确状态码（401/403/415/413）+ 反证。
    ("控制台鉴权", [PY, "tools/check_console_auth.py"]),
    # 托盘退出必须让桥线程自己走完 finally（2026-09-29 审查报告 P1-4）。
    # 原先只设了托盘自己的事件、主循环看不见 → daemon 线程被直接掐掉，
    # GattSession / Frida 注入 / 音频流没清理，现象是「退出再开就连不上」。
    # 其中「20 秒连接等待可被打断」是真跑一遍验的（假设备）。
    ("退出时优雅收尾", [PY, "tools/check_graceful_stop.py"]),
    # run_bridge 的**收尾范围**（2026-09-29 审查报告 P1-3）。
    # 原先 finally 只包住主循环，建立阶段那十来条 return False 全绕过它 ——
    # GattSession 还举着、注入还挂着、音频流没停，全靠 GC。现在收尾只有一处
    # （_BridgeResources.teardown），这道闸按句柄逐个核"登记了没有 / 释放了没有 /
    # 有没有 None 判断"，并真跑一遍"句柄全 None 的 teardown"。
    ("收尾范围与句柄登记", [PY, "tools/check_teardown_scope.py"]),
    ("按键映射表", [PY, "tools/check_keymap.py"]),
    ("控制台冒烟", [PY, "tools/smoke_console.py"]),
    ("录制器逻辑", [PY, "tools/test_recorder.py"]),
    # BLE 回调线程没有事件循环 —— v1.0.3 事故的回归闸。
    # 纯标准库、不需要真机，所以放 CI 里跑。
    ("BLE 回调线程", [PY, "tools/check_ble_callback_thread.py"]),
    # 诊断脚本的真机部分要人按键，没法自动化；但报告生成器是纯函数式的，
    # 用假数据把两条分支（能区分 / 不能区分）都验一遍 —— 不然那段代码
    # 第一次运行就是在用户机器上。不需要 keyboard 库。
    ("遥控器诊断报告", [PY, "tools/diag_remote.py", "--selftest"]),
    # 遥控器的 HID 判定链。v1.0.8 查「除语音键外所有按键都没反应」时，
    # 真机挖出一条以前没人写下来的硬事实：遥控器暴露了**两个厂商自定义
    # 用法页**（0xFF01/0xFF80，各 21 字节输入报告），Windows 对它们
    # 不做任何处理。这条事实直接决定"该修什么"，所以判定表 + 报告判读
    # 分支都用反例锁住。纯静态 + 假数据，CI 里可跑。
    ("HID 判定链", [PY, "tools/check_hidinfo.py"]),
    # 故障必须"看得见"且"说人话"。
    # v1.0.9 的真机事故：桥线程用 print 报异常，而主 exe 是 console=False ——
    # print 的流向是空的，于是桥每 3 秒崩一次、日志一行报错都没有、
    # 托盘还写着「按遥控器任意键唤醒」，把 OSError: E_INVALIDARG 捂了两小时。
    # 纯静态 + 反例，不需要真机。
    ("故障可见性", [PY, "tools/check_failure_visibility.py"]),
    # 蓝牙配对判定链（pairing.py）。
    # v1.0.10 加的这一关，治的是「换 USB 口 → 本地蓝牙地址变 → 配对记录作废」：
    # 程序显示「未连接」、Windows 显示「已配对」、设置里还删不掉。
    # 判定逻辑读的是真机注册表，CI 里没有蓝牙棒 —— 所以抽成纯函数 + 假数据，
    # 把真机形态（记录绑旧地址、AEP 节点也是旧地址）钉成 STALE_ADDR，
    # 再拿反例锁住两处最容易退化的地方（PnP 实例 ID 的 USB\ 前缀、
    # 搬家时保留注册表值类型）。
    ("蓝牙配对判定", [PY, "tools/check_pairing.py"]),
    # 厂商页按键解码（v1.0.11：按键映射全靠这一路）。
    # 解码表错一位就是「按上键出来的是返回」这种全串位的事故，
    # 而报告格式的假设（reportID=0x01、第 2 字节是用法码、0=松手）
    # 一旦错了，现象和"遥控器没连上"一模一样 —— 必须先用纯单测钉死，
    # 真机上按下键之后只要对焦"报告来没来"这一件事。
    ("厂商页按键解码", [PY, "tools/check_remote_hid.py"]),
    # 按键旁路（v1.0.20：注入 WUDFHost 从 HID 驱动内部抄报告）——
    # 这是真机上**唯一**能拿到遥控器按键的那一路（自开厂商页那条实测一直是 0 条）。
    # 它的失效全是静默的（注入失败 / 挂错宿主 / 报告格式不对，现象都是"按了没反应"），
    # 所以先把能脱离硬件验证的那一半钉死：消费类页 usage 表、两种报告格式的解码、
    # 抹除算法、.js 与 .py 的 IOCTL 常量不漂移。带反例自证。
    ("按键旁路解码", [PY, "tools/check_frida_tap.py"]),
    # 屏蔽表要**随配置实时更新**，并且下发后要**等钩子确认**（2026-09-29 审查报告 P1-6）。
    # 原先只在启动时 set_mapping 一次 ⇒ 运行中关掉映射 / 把键改成 native 之后，
    # Python 不映射了、JS 仍按旧表把 usage 原地写 0：界面写着"已停用"，
    # 实际那个键彻底失效，得重连。另一面：下发是异步的，控制台保存完就回
    # "已生效"，用户立刻按键时钩子可能还在用旧表。行为级（假 script 驱动真类）
    # + 反例自证（含"迟到 ack 不算数"那条最容易假绿的）。
    ("按键屏蔽表实时下发", [PY, "tools/check_frida_mapping.py"]),
    # Frida 旁路**只认准那一台设备**（2026-09-29 审查报告 P1-8）。
    # 原先两处太宽：Python 侧找不到精确 VID/PID 就自动回退到"任意 BLE HID 第一项"
    # （而且匹配用的是子串判断，REV/序列号/MAC 段里撞上短 VID 就误判）；JS 侧只按
    # IOCTL 过滤、没绑 FileHandle ⇒ 同一个 WUDFHost 里别的蓝牙键鼠的报告也会被
    # 原地改写。现象是"按键串台"，完全静默。行为级用**假 winreg** 驱动 hid_hosts()
    # 验证过滤真的生效，含"子串陷阱"两种名字 + 反例自证。
    ("按键旁路目标精确化", [PY, "tools/check_frida_target.py"]),
    # 配对修复的备份/还原必须是**可信事务**（2026-09-29 审查报告 P1-7）。
    # 修复工具以管理员身份动注册表，老版本的备份有三个洞：① 整棵
    # BTHPORT\Parameters\Keys + 整个 Devices 全抄（本机所有蓝牙设备的链路密钥
    # 都落盘）；② 备份落在 %APPDATA%（当前用户可写）—— 低权限进程塞一个"更新"的
    # .reg，等用户下次点「还原」被 UAC 提权导入，而里面装的是能解密链路的材料；
    # ③ 还原只 glob 最新的**一个** .reg（半个事务 + 挑哪个看 mtime）。
    # 现在：最小子树 + manifest（事务 ID / SHA-256）+ 解析 .reg 键路径限制前缀 +
    # ProgramData + DACL 禁继承 + 任一关键项失败即停。行为级用假 _run + 临时目录
    # 真跑（不碰注册表），并**逐条**反例自证。
    ("配对备份可信事务", [PY, "tools/check_pairing_backup.py"]),
    # 混音取数不许丢样本（2026-09-29 审查报告 P0-2）。
    # 采集块 1024 / 播放块 240，老写法"攒够 n 就整块返回" ⇒ 调用方只读前 240 个，
    # **每块静默丢 76%**（实测：4096 个采样只剩 960 个）。房间里那一路于是变成
    # 「5ms 有声 / 16ms 空白」的切片（约 67Hz 嗡嗡声），语音识别直接崩 ——
    # 而日志里一个字都没有。用户看到的是「面板弹了、也在收音，但一个字都识别不出来」。
    # 行为级验证（喂进去多少就该吐出来多少）+ 反例。
    ("混音取数不丢样本", [PY, "tools/check_mixer_read.py"]),
    # ATVV 解码器必须有 logger（2026-09-29 审查报告 P0-3）。
    # v0.4 分支里那句 logger.debug 原先指向一个**不存在**的 logger ⇒ NameError，
    # 发生在每帧都要过的解码路径上、被外层 try 吞掉 ⇒ 所有 v0.4 帧全解不出来，
    # 现象是"按了没声音"而日志里没有指向"帧长不符"的线索。
    # 本机协商的是 v1.0，所以一直没触发 —— 属于潜伏雷。行为级 + 反例。
    ("ATVV 解码健壮性", [PY, "tools/check_atvv_decode.py"]),
    # 电脑麦克风解析：**不许把本程序自己的输出当成麦克风**（2026-09-29 真机）。
    # 本程序往 CABLE Input 写、VB-CABLE 直通到 CABLE Output；而"电脑麦克风"的
    # 默认解析恰好落在 CABLE Output 上 ⇒ 自听自的闭环。实测 sys 那一路比遥控器
    # 响 24 dB，送到输入法的信号里约一半是延迟回声 —— 用户报「时好时坏」。
    # 守卫原先只加在下拉框（list_input_devices）上，两处真正的解析都绕过了它。
    # 用假 sounddevice 把每种情形跑一遍（含"默认设备就是回环"），7 条反例。
    ("电脑麦克风解析", [PY, "tools/check_input_device.py"]),
    # "波形在动、输入法却收不到声音，重启才好"（2026-09-17 真机）——
    # 输出流建一次就没人管 + 静音期间不消费队列导致 queue.Full 静默丢帧。
    # 纯静态，几毫秒。
    ("音频流自愈", [PY, "tools/check_audio_watchdog.py"]),
    # 采样率是**遥控器**在 CAPS 里给的（8k/16k），而 ATVVState 默认就是 16000。
    # 发完 GET_CAPS 就建流 = 8k 的遥控器被按 16k 播 —— **不报错**，只是变调变速。
    # 这道闸钉：先协商再建流、超时留痕、迟到的响应要能触发整条链重建
    # （含电脑麦克风那一路 —— 它按 out_rate 重采样，只换输出流同样不对）。
    ("采样率先协商后建流", [PY, "tools/check_audio_rate.py"]),
    # 依赖"未完全锁定"：requirements.txt 里传递依赖没写版本（pyinstaller 拉
    # altgraph/pefile/…、frida 拉 cffi/six/…）⇒ 同一份代码在不同时间构建出的
    # 产物**不一样**，而按键旁路依赖的 frida 原生扩展一变就是静默故障。
    # 锁文件把版本 + 每个 wheel 的 SHA-256 固定下来，CI 用 --require-hashes 装。
    # 这一步是**离线**静态校验（锁与 requirements 一致、每行都有哈希）；
    # 真正"锁能用"由 CI 的 `pip install --require-hashes` 证明。
    ("依赖锁一致性", [PY, "tools/make_deps_lock.py", "--check"]),
    # voice coding 的最后一环：说完 → 按语音键结束 → **替用户把消息发出去**。
    # 2026-09-17 武哥的原话是"说完还要去电脑上按鼠标点发送，完全没有
    # voice coding 的感觉"。这道闸钉住：默认开、延迟不能小到在文字落进
    # 输入框之前就打回车、开始新一段要取消待发送、零帧误触不发、
    # 三个字段进了 _get_cfg 白名单。带 5 条反例自证。
    ("语音结束自动发送", [PY, "tools/check_send_after_voice.py"]),
    # 注入自检会真的发按键，无桌面会话里自己会 SKIPPED（不算失败）。
    # 它是唯一能拦住"SendInput 静默失效"和"组合键顺序错"的一关。
    #
    # ⚠ 它是**唯一受机器负载影响**的一关（读的是系统实时按键状态，而低级键盘钩子
    #   有超时机制）。刚跑完前面几个会起进程/线程的步骤、机器还没静下来时跑它，
    #   就会出现"钩子没收到"这一类假 FAIL。所以：
    #     ① settle：先等 2 秒，让前一步的进程彻底退干净
    #     ② retries：整步再跑一次（脚本内部本身也已经重试 6 轮）
    #   settle/retries 只加在它身上，别的步骤该红就红。
    ("按键注入自检", [PY, "tools/check_injection.py"],
     {"settle": 2.0, "retries": 1, "retry_hint": "钩子没收到",
      # CI 不跑：它**真的往系统里注入按键**，需要交互桌面会话。
      # runner 上没有桌面会话 ⇒ 跑了只会 SKIP（旧版还会把 SKIP 当成功）。
      "hardware": True,
      "why": "真的注入按键，需要交互桌面会话（runner 上没有）"}),
    # 接管 0x1812 的那个工具是**本项目唯一会改系统设备状态**的东西。
    # 这道闸钉三件事：默认只读（不带 --go 一个字节都不改）、
    # 目标必须是 BTHLEDEVICE 父节点（0x1812 底下还有 5 个 HID 子集合，
    # 禁错那个就是白测一场 + 错判「软件到头了」）、禁用与恢复在同一条
    # PowerShell 命令里。带 2 条反例自证。
    ("接管 0x1812 的安全护栏", [PY, "tools/check_takeover_guard.py"]),
    # 语音会话状态机是最反复出问题的地方（v1.0.7 / v1.0.13 都在这儿翻过车）。
    # 这道闸钉「会话不许自己把自己关掉」：防自激窗口内要能吞下**多个**回响
    # （2026-09-22 真机：两个 audio_start 隔 19ms，第 2 个把会话关了）、
    # 松手不许结束会话、吞掉的事件不许静默。带 2 条反例自证。
    ("语音会话状态机", [PY, "tools/check_voice_session.py"]),
    # 「按键在 Windows 上全没反应」这件事的**判词**。
    # ⚠ 2026-09-23 二次改判：上一版这里写「查到根因：配对记录里没有 LE 密钥
    # 材料（没有 LTK）」—— **那是错的**。当时看的是 `BTHPORT\Parameters\
    # Devices\<远端>`，那是**元数据**键（名字/VID/PID/外观/时间戳），本来
    # 就不该有 LTK；真正的密钥在 `Parameters\Keys\<适配器MAC>\<设备MAC>\`，
    # 真机实测**读得到而且 LTK/IRK/CSRK/EDIV/ERand 都在**。
    # 所以这条判词现在钉的是**三态**：读到 / 存在但空 / 读不到；
    # 「读不到」只许报「无法判定」，不许报「没有密钥」。
    # 它是整份报告里唯一"会给出行动建议"的地方：哪天有人顺手把它改成
    # 永远打 ✅（或又拿元数据键下结论），就会把人引到错误的方向上。
    # 所以它脱离注册表单独可测，带 8 项自检（含"四种输入的判词必须两两不同"
    # 这条反例 —— 防止硬编码凑）。
    ("按键不通的判词", [PY, "tools/probe_hogp_state.py", "--selftest"]),
    # 仓库根目录 .bat 的两条一致性 —— 都是 2026-09-23 真机上踩到的，
    # 而且**都会让「用户双击一下」失败，而失败的往往正是他此刻最需要的那一步**。
    #   ① 四个 bat 各写各的 Python 探测：只有一部分去找 WorkBuddy 托管环境那个
    #      （本机唯一装齐 bleak/winsdk/keyboard 的解释器），而本机**没有** .venv
    #      ⇒ 同一台机器上「一个工具能跑、另一个报当前 Python 跑不起来」。
    #      这两件事看起来无关，其实在同一个用户路径上一前一后（先修复、再复测）。
    #   ② .bat 的字节编码和它自己声明的 chcp 打架：`diag-remote.bat` / `run.bat`
    #      是 UTF-8 字节却没写 chcp，中文 echo 在 936 控制台上必然是乱码。
    #      我还犯过反向的错：按「中文 Windows 一律 GBK」把一个 chcp 65001 的
    #      文件转了编码，框线字符直接编不出来 ⇒ **文件自己的 chcp 才是准的**。
    # 两条边界都按实测收窄过（只算非注释行／只算自己挑解释器的 bat），
    # 免得假红逼人乱改文件；`--selftest` 里 7 条反例专门验这个。
    ("bat 的 Python 与编码一致性", [PY, "tools/check_bat_env.py"]),
    ("bat 一致性闸的自检", [PY, "tools/check_bat_env.py", "--selftest"]),
    # 监听窗口**不许被某一路的成败绑住** —— 2026-09-23 真机翻车：
    # 窗口原先长在 `gatt_part()` 函数体里，而那个函数有一条「BLE 连不上就
    # return」的路。于是「遥控器连不上」这一个失败，把整个 90 秒窗口一起
    # 带走了 —— 武哥双击 bat 看到的是「跑一下就结束，都没等我按按钮」，
    # 而报告里只剩静态段（全是 0），读起来像「测过、0 条」，**其实没测**。
    # 这条闸**人为制造当初那个条件**（把 gatt_part 换成立刻返回的假货），
    # 再断言窗口照样跑满。反例实测过：掐掉窗口循环 → 立刻红。
    ("监听窗口不被 GATT 成败绑住", [PY, "tools/watch_all_channels.py", "--selftest"]),
    # 控制台前端「不拼 HTML」（2026-09-29 审查报告 P1-2）。
    # 原来是 renderStatusList 把 checklist[].value 拼进 innerHTML，而那个 value
    # 可以是 /api/config 写进去的设备名 —— 存储型 XSS。现在钉住：整个前端
    # 只剩 iconFor 那一处 innerHTML（赋的是本文件常量），且 HTML 里没有内联
    # 脚本/样式/事件、服务端真的发 CSP。
    ("控制台前端不拼 HTML", [PY, "tools/check_ui_xss.py"]),
    # CI 工作流与「项目声称的闸门」必须一致（2026-09-29 审查报告 P1-10）。
    # 老工作流只在 tag/手动触发（PR 不跑）、闸门是手工列的（和本地这份各自漂）、
    # 构建与发布同一个 job（发出去的包是又构建一次的）、没写 draft（靠 Action
    # 默认值，v1.0.21 事实上直接公开了）、权限全程 contents: write。
    # 外加 `--ci` 的判定机制本身：必需项 SKIP 必须算失败（否则环境一坏，
    # 整道闸静默变成永远绿的摆设）。行为级用**假 STEPS + 假 _run_step 真跑 main()**。
    ("CI 与声称的闸门一致", [PY, "tools/check_ci_workflow.py"]),
    # 源码用户唯一会双击的那个入口（2026-09-29 审查报告第六节 1~3）。
    # 它的三处错误**都不报错**：入口是 main.py（没托盘、只能关窗口硬杀进程，
    # P1-3/P1-4 做的优雅收尾根本到不了）／把系统默认麦克风写成 CABLE Input
    # （VB-CABLE 两端命名是反的，写成 Input 就是读"没人写的那只端点"，
    # 症状一点声音都没有）／pip 输出 `>nul` 全吞且不看退出码。
    ("run.bat 的入口与端点", [PY, "tools/check_run_bat.py"]),
    # 第三方许可证清单必须**覆盖实际依赖**，并真的装进安装包（第六节 9）。
    # 安装包里分发着 15 个第三方包，其中 pystray 是 LGPL-3.0、frida 是
    # wxWindows（LGPL 派生）—— 都要求随分发附上许可证；PyInstaller 的
    # GPL 特殊例外也得写出来。而 THIRD_PARTY_NOTICES.md 原先只写了三个
    # "移植来源"，实际依赖一个没列，安装包里也没有 licenses\。
    # 拿 requirements*.txt 逐项比对 + 反例自证（含"新增依赖忘了补"这个动作）。
    ("第三方许可证清单", [PY, "tools/check_licenses.py"]),
    # 日志三件事（P2-6）：**轮转** / **脱敏** / **raw HID 默认不记**。
    # 坏掉的现象分别是「bridge.log 涨到几十 MB 没人敢删」和「用户把日志贴到
    # 群里、里面是他完整的蓝牙地址」。前两件**行为级**真跑（真起 handler、
    # 真写满触发轮转、真格式化 args 里的地址），后一件查源码接线。
    # 含 5 条反例：改回 basicConfig / 先打日志后脱敏 / raw 改回裸打 /
    # 默认值改成脱敏关 / 把白名单那行删掉。
    ("日志轮转与脱敏", [PY, "tools/check_logging.py"]),
]

# 真实浏览器那一道要 node + playwright。没有就别硬塞进列表 —— 但**要出声**，
# 免得"没跑"被读成"跑过了、过了"。这里记进 `_UNRUN`，结尾会点名（`--ci` 时算失败：
# CI 上有 node，缺了就是环境没配好，不该悄悄降级）。
_UNRUN: list[tuple[str, str]] = []
if NODE:
    STEPS.append(("控制台 XSS 真实渲染", [NODE, "tools/check_ui_xss.js"]))
else:
    _UNRUN.append(("控制台 XSS 真实渲染",
                   "本机没有 node（CI 上自带 node，会装 playwright chromium）"))

STEP_DEFAULTS = {
    "settle": 0.0,
    "retries": 0,
    "retry_hint": "",
    # ── 下面三个是 2026-09-29 审查报告 P1-10 加的 ──────────────────────────
    # hardware=True  → 需要真机 / 交互桌面，`--ci` 时**显式摘掉**并在结尾点名
    "hardware": False,
    # required=False → 允许它 SKIP（只给"环境本来就可能没有"的闸用；
    #                  默认 True ⇒ SKIP 按 FAIL 计）
    "required": True,
    "why": "",
}

# 一条闸门"本次没验证任何东西"的写法：脚本自己打印以 `SKIPPED` 开头的行。
# 可能带一个符号前缀（例如 `  ⚠ SKIPPED（import main 失败：…）`）——
# 这是仓库里既有的约定（check_injection / smoke_console / diag_remote /
# check_send_after_voice 都这么写），这里把它升格成结构化结论。
_SKIP_RE = re.compile(r"^\s*(?:[^\w\s]{1,3}\s+)?SKIPPED\b", re.M)


def _verdict(ok: bool, out: str) -> str:
    """把一次运行归一成 PASS / FAIL / SKIP。"""
    if not ok:
        return "FAIL"
    return "SKIP" if _SKIP_RE.search(out) else "PASS"


def _run_step(cmd: list[str]) -> tuple[bool, str]:
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()


def _step_summary(counts: dict, excluded: list[tuple[str, str]]) -> str:
    """给 GitHub Actions 的步骤摘要用的 Markdown（结构化结果）。"""
    out = ["## 闸门结果（`tools/check_all.py --ci`）", "",
           "| 结论 | 数量 |", "|---|---|",
           f"| PASS | {counts['PASS']} |",
           f"| FAIL | {counts['FAIL']} |",
           f"| SKIP | {counts['SKIP']} |"]
    if excluded:
        out += ["", "### 本模式未运行的闸", ""]
        out += [f"- `{n}` —— {w}" for n, w in excluded]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑一遍全部校验（CI 用 --ci）")
    ap.add_argument("--ci", action="store_true",
                    help="CI 安全集：摘掉需要真机/交互桌面的闸，其余必跑，"
                         "必需项 SKIP 视为失败")
    args = ap.parse_args(argv)

    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    excluded: list[tuple[str, str]] = []

    # 工具缺失（没 node）也是"没跑"的一种，一并点名。
    for name, why in _UNRUN:
        if args.ci:
            # CI 上有 node；缺了说明环境没配好，不许悄悄降级成"跳过"。
            counts["FAIL"] += 1
            print(f"[FAIL] {name} —— {why}")
        else:
            excluded.append((name, why))
            print(f"[--  ] {name} —— {why}")

    for step in STEPS:
        name, cmd = step[0], step[1]
        opt = {**STEP_DEFAULTS, **(step[2] if len(step) > 2 else {})}

        # ── `--ci` 摘掉需要真机 / 人工操作的那几道（**显式**，不是悄悄跳过）──
        if args.ci and opt["hardware"]:
            excluded.append((name, opt["why"] or "需要真机 / 人工操作"))
            print(f"[--  ] {name} —— CI 不跑：{opt['why'] or '需要真机 / 人工操作'}")
            continue

        if opt["settle"]:
            time.sleep(opt["settle"])

        ok, out = _run_step(cmd)
        # 只在"失败原因是已知的环境噪声"时才重试 ——
        # 真回归（结果不对）重试也是浪费，而且掩盖不了任何东西。
        for _ in range(opt["retries"]):
            if ok:
                break
            if opt["retry_hint"] and opt["retry_hint"] not in out:
                break
            print(f"[.... ] {name}：疑似机器忙，重跑一次…")
            time.sleep(1.5)
            ok, out = _run_step(cmd)

        verdict = _verdict(ok, out)
        if verdict == "SKIP" and opt["required"]:
            # 必需项 SKIP = 失败。否则"环境一坏，闸门静默变成永远绿的摆设"。
            verdict = "FAIL"
            out = ((out + "\n" if out else "")
                   + "[必需项不许跳过] 这道闸本次没有真正验证任何东西。"
                     "要么把环境补齐，要么把它标成 required=False 并写清为什么。")
        counts[verdict] += 1
        print(f"[{verdict:4}] {name}")
        if out:
            for line in out.splitlines():
                print("       " + line)

    print()
    print("── 结果汇总 ──────────────────────────────────────────────────")
    print(f"   PASS {counts['PASS']}    FAIL {counts['FAIL']}    SKIP {counts['SKIP']}")
    if excluded:
        print(f"   本模式未运行 {len(excluded)} 道（**没跑 ≠ 过了**）：")
        for n, w in excluded:
            print(f"     · {n} —— {w}")
    print("ALL CHECKS PASSED" if not counts["FAIL"]
          else f"{counts['FAIL']} 项校验未通过")

    # 结构化结果也给一份给 CI 的步骤摘要（PR 页面上一眼能看）
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(_step_summary(counts, excluded))
        except OSError:
            pass

    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
