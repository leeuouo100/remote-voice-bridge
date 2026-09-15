"""
遥控器真机诊断 —— 让**程序**把现象测出来，不用人来描述。

解决三个一直没有答案的问题：

  ① 遥控器的各个按键在 Windows 上到底发出什么？（"有些按键没作用"到底是
     没发事件、还是发了但我们没映射对？）
  ② 遥控器按键与物理键盘按键，在低级钩子里的 (名称, 扫描码) 是否一样？
     —— 这一条决定了**能不能**做到"只拦遥控器、不误伤物理键盘"。
       如果两者扫描码不同，就能实现真正的设备级区分；相同则只能二选一。
  ③ 配合控制台一起看，可以确认"映射写完到底有没有生效"。

产物：`%APPDATA%\\remote-voice-bridge\\remote-diag.txt`
      直接把这个文件发出来即可。

⚠ 跑之前**先把桥接程序退干净**（托盘右键 → 退出）：
   它还开着的话，它注入的按键也会被本脚本记进去，报告就脏了。

用法： · 安装版：开始菜单 →「遥控器诊断」（等价于安装目录里的
         RemoteVoiceBridgeDiag.exe）
       · 源码版： python tools/diag_remote.py ，或双击仓库根目录的 diag-remote.bat
       · **不用按键、只查硬件**： python tools/diag_remote.py --hid
         （2 秒出结果，**不需要退出桥接程序**。遥控器"连上了但按键没反应"
           先跑这个，能一眼分清是"Windows 没认成键盘"还是"程序这层的事"）
       · 只验报告生成器、不采集： python tools/diag_remote.py --selftest
中断： 随时按 Ctrl+C

## 报告里的四段（结论 0 最关键）

【结论 0】HID 硬件身份 —— 不用按任何键。看遥控器的 5 个 HID 集合里，
          键盘那一路有没有被 Windows 绑上（Service=kbdhid）。
          没绑上 / 根本没键盘集合 → 改映射表没有任何用，先修驱动/配对。
【结论 1/2】遥控器与物理键盘的扫描码是否可区分（决定能不能只拦遥控器）
【结论 3】各键实际发出的键盘事件（"按了没反应"若在这一段是空的，看结论 4）
【结论 4】按键落点 —— 各 HID 集合的**原始报告**计数。遥控器有两个
          **厂商自定义集合**（0x01FF01 / 0x01FF80），Windows 对它们不做任何事。
          若按键报告只落在那里，任何映射都够不到它 —— 必须自己读那一路。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

if not getattr(sys, "frozen", False):
    # 源码环境下要能从仓库根 import config / _utf8。
    # 打包后（PyInstaller）__file__ 指向包里那个不存在的路径，插入只会添乱，
    # 而 config / _utf8 已经在包内可直接 import。
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402  （下面是中文输出，先钉住编码）

_setup_utf8()

import ctypes  # noqa: E402
import subprocess  # noqa: E402

from config import APP_VERSION, CONFIG_DIR  # noqa: E402

# ── HID 硬件身份 + 集合报告监听 ────────────────────────────────────────────
# 2026-09-15 加的。起因：武哥报「遥控器除了语音键，其他所有按键都没有作用」。
# 只记键盘事件答不了这一问 —— 因为**按键可能压根没变成键盘事件**：
# 遥控器还暴露了两个厂商自定义集合（0x01FF01 / 0x01FF80），Windows 对它们
# 不做任何事。报告里必须同时有"设备长什么样"和"按键落在哪一路"。
# 用 try/except 包住：打包时万一漏了模块，老功能（7 段按键采集）仍要能用。
try:
    import hidinfo      # noqa: E402
    import hidwatch     # noqa: E402
except ImportError:                                     # noqa: BLE001
    hidinfo = None                                      # type: ignore[assignment]
    hidwatch = None                                     # type: ignore[assignment]

# ⚠ `keyboard` 只在真正要监听时才 import —— `--selftest` 只验报告生成器，
#   不需要键盘库，这样它在 CI / 没装依赖的机器上也能跑。
_kb = None

REPORT = CONFIG_DIR / "remote-diag.txt"

# (阶段标题, 屏幕提示, 至少按几下就自动进入下一段, 最多等几秒)
STAGES = [
    ("物理键盘 Enter",
     "请敲【物理键盘】的 Enter 键 3 次（这是基准，用来和遥控器对比）", 3, 14),
    ("遥控器 OK",
     "请按【遥控器】的确认键 / OK 键 3 次", 3, 14),
    ("物理键盘 Esc",
     "请敲【物理键盘】的 Esc 键 3 次（第二个基准）", 3, 14),
    ("遥控器 返回",
     "请按【遥控器】的返回键 3 次", 3, 14),
    ("遥控器 音量",
     "请按【遥控器】的音量＋ 和 音量－ 各 2 次", 4, 16),
    ("遥控器 方向",
     "请按【遥控器】的上 / 下 / 左 / 右 各 1 次", 4, 16),
    ("遥控器 其它键",
     "请按【遥控器】剩下没测过的键：Home / Home 长按 / 语音键 / 电源 / 输入源 等，"
     "每个按一下（有几个按几个）", 3, 20),
]

# 每段之间留一点空隙，免得上一段的尾巴掉进下一段
_GAP = 1.5


def _bridge_running() -> str:
    """桥接程序还在跑吗？返回非空 = 检测到（附证据）。

    ⚠ 这不是"贴心提醒"，是**必须挡住**的一件事，理由见 main() 里的说明。
    2026-09-15 的真机报告就是这么废掉的：程序没退，遥控器的原生键全被它的
    拦截钩子吞掉，报告写成「遥控器 OK / 返回 / 音量 / 方向 各 0 次」，
    看着像遥控器坏了，其实是**一个都没测到**；同时还混进了它自己注入的按键。

    判据用进程名（安装版就是 RemoteVoiceBridge.exe）。
    源码版跑的是 python.exe，名字区分不出来 —— 那种情况只能靠人看提示。
    """
    try:
        p = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RemoteVoiceBridge.exe", "/NH"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000,      # CREATE_NO_WINDOW：别闪黑框
        )
        if "RemoteVoiceBridge.exe" in (p.stdout or ""):
            return "进程 RemoteVoiceBridge.exe 正在运行"
    except Exception:                       # noqa: BLE001
        pass
    return ""


def _pad(s: str, width: int) -> str:
    """按**显示宽度**补空格（中文占 2 列）。

    直接用 f"{x:<22}" 是按字符数补的 —— 中文名字的表格永远对不齐，
    而这份报告的读者就是靠对齐扫一眼看结论的。
    """
    w = sum(2 if ord(c) > 0x2E7F else 1 for c in s)
    return s + " " * max(1, width - w)


def _fmt(name: str, scan: int, keypad: bool) -> str:
    # keyboard 库在"这个键没有硬件扫描码"时会塞一个 **负数**，值为 -vkCode。
    # 老老实实分开显示：扫描码才是判断"来源设备"的关键，VK 不是。
    if scan < 0:
        return (f"name={name!r:<18} 无扫描码(vk=0x{-scan:02X})   "
                f"keypad={keypad}")
    return f"name={name!r:<18} scan=0x{scan:02X}            keypad={keypad}"


def main() -> int:
    global _kb
    if not hasattr(ctypes, "WinDLL"):
        print("SKIPPED（非 Windows）")
        return 0

    # --hid：只出"不用按任何键"的硬件身份报告，立刻返回。
    # 特意放在下面那道"必须先退出桥接程序"的闸**之前** ——
    # 它不监听按键，桥接程序开着也不影响结论，用户不用先去关程序。
    if "--hid" in sys.argv:
        if hidinfo is None:
            print("⚠ 本版本没带上 hidinfo 模块，无法探测 HID 设备。")
            return 1
        print(hidinfo.format_report(hidinfo.probe(with_descriptor_len=True), indent=""))
        return 0
    try:
        import keyboard as _kb_mod
    except ImportError:
        print("SKIPPED（没装 keyboard 库）")
        print("  修复：pip install -r requirements.txt")
        return 1
    _kb = _kb_mod

    # 在跑就先拦下来：带着它测出来的报告一定是错的。
    running = _bridge_running()
    if running and "--force" not in sys.argv:
        print()
        print("  ⛔ 桥接程序还在运行（%s）。" % running)
        print()
        print("     为什么必须先退出它：")
        print("       · 它的拦截钩子会把遥控器原生按键**吞掉** —— 本脚本一个都收不到，")
        print("         报告会显示「遥控器 OK / 返回 / 音量 / 方向 各 0 次」，")
        print("         看着像遥控器坏了，其实是一个都没测到；")
        print("       · 它自己注入的按键又会被本脚本记进来 → 报告里混进假数据。")
        print("     两种污染都会让人得出错误结论，所以这里直接停下，不出一份脏报告。")
        print()
        print("     做法：托盘图标右键 → 退出，然后重新跑本诊断。")
        print("     （确实要带着它测、并且接受报告作废：加 --force）")
        return 2

    print("=" * 66)
    print(f" 遥控器真机诊断  ·  remote-voice-bridge v{APP_VERSION}")
    print("=" * 66)
    print()
    print("  ⚠ 请先确认桥接程序**已经退出**（托盘右键 → 退出）。")
    print("     它开着的话，它注入的按键也会被记进来，报告就不准了。")
    print()
    print("  接下来会有 7 段，每段按提示按键即可 —— 按够了会自动跳到下一段。")
    print("  全程大约 1.5 分钟，中途不想继续就按 Ctrl+C。")
    print()
    input("  准备好了按【回车】开始…")

    # ── 先出硬件身份（不用按键）──
    snap = hidinfo.probe() if hidinfo is not None else None
    if snap is not None:
        print()
        print(hidinfo.format_report(snap))
        print()

    # ── 同时开一路"HID 集合原始报告"监听 ──
    # with_hooks=False：本脚本自己的 on_key 已经在记键盘事件了，再挂一个
    # 会把同一个按键记两遍。这里只要"哪一路集合收到了原始报告"这半边。
    watch = hidwatch.ReportWatcher(only_google=True) if hidwatch is not None else None
    if watch is not None:
        n_open = watch.start(with_hooks=False)
        print(f"  ℹ 已同时开始读 HID 集合的原始报告（打开 {n_open} 路）。")
        print("     若按键报告落在『厂商自定义』那一路，说明 Windows 根本看不见它 ——")
        print("     那种情况下改映射表没有任何用，报告末尾会直接给出判读。")
        print()

    rec: list[tuple[float, int, str, int, bool]] = []
    seen_in_stage = 0
    stage_idx = 0

    def on_key(e) -> None:
        nonlocal seen_in_stage
        if e.event_type != "down":
            return
        # 排除我们自己（本脚本不注入，但保险起见标一下）
        rec.append((time.time(), stage_idx, str(e.name), int(e.scan_code or 0),
                    bool(getattr(e, "is_keypad", False))))
        seen_in_stage += 1

    hook = _kb.hook(on_key, suppress=False)

    try:
        for i, (title, hint, need, budget) in enumerate(STAGES):
            stage_idx = i
            seen_in_stage = 0
            print()
            print(f"── [{i + 1}/{len(STAGES)}] {title} " + "─" * max(0, 40 - len(title)))
            print(f"   {hint}")
            deadline = time.time() + budget
            while time.time() < deadline and seen_in_stage < need:
                time.sleep(0.1)
            took = seen_in_stage
            print(f"   ✓ 本段记录到 {took} 次按键"
                  + ("" if took else "（一次都没有 —— 这个键可能压根不发键盘事件）"))
            time.sleep(_GAP)
    except KeyboardInterrupt:
        print("\n已中断，用已记录的部分出报告。")
    finally:
        try:
            _kb.unhook(hook)
        except Exception:  # noqa: BLE001
            pass
        if watch is not None:
            watch.stop()

    return _write_report(rec, snap=snap, watcher=watch)


def _stage_lines(rec, i: int) -> list[str]:
    t0 = rec[0][0] if rec else time.time()
    return [f"    {datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')[:-3]}  {_fmt(n, s, k)}"
            for (t, si, n, s, k) in rec if si == i]


def _pairs(rec, i: int) -> set[tuple[str, int]]:
    return {(n, s) for (_t, si, n, s, _k) in rec if si == i}


def _write_report(rec, quiet: bool = False, snap: dict | None = None,
                  watcher=None) -> int:
    lines: list[str] = []
    A = lines.append

    A("=" * 66)
    A(" remote-voice-bridge 遥控器诊断报告")
    A(f" 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    A(f" 程序版本：v{APP_VERSION}")
    A(f" 记录到按键总数：{len(rec)}")
    A("=" * 66)

    # ── 结论 0：不用按任何键就能拿到的硬件身份 ──
    # 放最前面是因为它能一票否决后面所有分析：若 Windows 压根没把遥控器的
    # 键盘集合绑上、或按键发在厂商自定义页，那"按了没反应"与映射表无关，
    # 后面那 7 段按键记录只会是空的 —— 先看这里能省掉一整轮返工。
    A("")
    if snap is not None and hidinfo is not None:
        A(hidinfo.format_report(snap, indent=""))
        A("")

    fp_enter = _pairs(rec, 0)
    rm_ok = _pairs(rec, 1)
    fp_esc = _pairs(rec, 2)
    rm_back = _pairs(rec, 3)

    A("")
    A("【结论 1】遥控器 vs 物理键盘 —— 底层能不能区分？")
    A("")
    A(f"  物理键盘 Enter 的 (名称, 扫描码)：{sorted(fp_enter) or '（没记录到）'}")
    A(f"  遥控器   OK   的 (名称, 扫描码)：{sorted(rm_ok) or '（没记录到）'}")
    A("")
    if fp_enter and rm_ok:
        if fp_enter & rm_ok:
            A("  → ❌ 两者**完全相同**：Windows 低级键盘钩子无法区分来源设备。")
            A("     含义：只能靠「吞掉」来拦截原生按键，代价是物理键盘的同一个键")
            A("           也会被一起吞。想要精确拦截，只能上 Interception 驱动")
            A("           这类**按设备**过滤的方案（需要装内核驱动）。")
        else:
            A("  → ✅ 两者**扫描码不同**：可以实现设备级精确区分！")
            A("     含义：按扫描码判断来源，只拦遥控器的键、完全不动物理键盘。")
            A("           这是一条明确的可优化项，请把本报告发出来。")
    else:
        A("  → ⚠ 有一段没记录到按键，无法给出结论。")
        A("     物理键盘那段都没记录到，说明脚本没收到键盘事件（权限/会话问题）；")
        A("     只有遥控器那段是空的，说明这个键**根本不发键盘事件**。")

    A("")
    A("【结论 2】遥控器返回键 vs 物理键盘 Esc")
    A("")
    A(f"  物理键盘 Esc 的 (名称, 扫描码)：{sorted(fp_esc) or '（没记录到）'}")
    A(f"  遥控器   返回 的 (名称, 扫描码)：{sorted(rm_back) or '（没记录到）'}")
    if not rm_back:
        A("  → ⚠ 遥控器的返回键**一次都没记录到**：它不发标准键盘事件。")
        A("     含义：这个键在 Windows 侧根本不产生按键，映射表对它无能为力；")
        A("           要支持它只能走 ATVV 控制通道，或彻底放弃。")
    elif not fp_esc:
        A("  → ⚠ 物理键盘那段没记录到，拿不到基准，无法对比（脚本没收到键盘事件？）")
    elif fp_esc & rm_back:
        A("  → ❌ 相同：与结论 1 一样，底层分不出这两个键。")
    else:
        A("  → ✅ 不同 —— 说明遥控器在这些键上用的是**独立的扫描码**，")
        A("     按扫描码区分来源的方案同样能覆盖返回键。")

    A("")
    A("【结论 3】遥控器各键实际发出什么（'有些按键没作用'的答案在这里）")
    A("")
    A("  " + _pad("阶段", 22) + _pad("次数", 6) + "实际发出的 (名称, 扫描码)")
    A("  " + "-" * 66)
    for i, (title, _h, _n, _b) in enumerate(STAGES):
        ps = sorted(_pairs(rec, i))
        cnt = len([1 for r in rec if r[1] == i])
        A("  " + _pad(title, 22) + _pad(str(cnt), 6)
          + (str(ps) if ps else "—（一次都没有：这个键不发键盘事件）"))

    # ── 结论 4：按键落点（HID 集合原始报告）──
    # 结论 3 只看得到"变成了键盘事件"的按键。落在厂商自定义页 / 鼠标集合上的
    # 按键，在结论 3 里一律表现为"一次都没有"，看不出原因。这一段补上另一半。
    if watcher is not None:
        A("")
        for ln in watcher.summary_lines():
            A(ln)

    A("")
    A("【全部记录明细】")
    A("")
    for i, (title, _h, _n, _b) in enumerate(STAGES):
        A(f"  [{i + 1}] {title}")
        body = _stage_lines(rec, i)
        A("\n".join(body) if body else "    （无）")
        A("")

    A("")
    A("【去重总表】本报告里出现过的所有 (名称, 扫描码)")
    A("")
    for n, s in sorted({(n, s) for (_t, _i, n, s, _k) in rec}):
        A(f"  {_fmt(n, s, False)}")

    text = "\n".join(lines) + "\n"
    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        if not quiet:
            print()
            print("=" * 66)
            print(f"报告已写出：{REPORT}")
            print("把这个文件发出来即可（可以直接拖进对话）。")
            print("=" * 66)
    except Exception as e:  # noqa: BLE001
        print(f"\n⚠ 写文件失败（{e}），下面是报告全文：\n")
        print(text)
    return 0


def _selftest() -> int:
    """不出报告文件，只跑一遍报告生成器。

    这个脚本的真机部分必须有人按键，没法自动化；但"报告生成"是纯函数式的，
    可以用假数据验 —— 否则它就是一段从没跑过的代码，第一次运行
    就是在用户机器上。
    """
    global REPORT
    now = time.time()

    # 假造一份 HID 快照 + 一份监听结果。
    # 为什么要造假：真机上"键盘集合绑定"和"厂商页收到报告"这两种状态
    # 都**必须真的插上遥控器才会出现**，CI 里永远复现不了。而这两段文字
    # 恰恰是整份报告最关键的判读 —— 不验就等于第一次运行在用户机器上。
    fake_snap = {
        "cols": [
            {"hwid": "{00001812-…}_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c&Col01",
             "instance": "a&1&0&0000", "col": "COL01", "hogp": True,
             "service": "kbdhid", "desc": "HID Keyboard Device", "config_flags": 0,
             "guid": "{4d36e96b-…}", "bt_addr": "F1:96:A2:63:67:1C",
             "is_keyboard": True, "is_mouse": False, "enabled": True},
            {"hwid": "{00001812-…}_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c&Col04",
             "instance": "a&1&0&0003", "col": "COL04", "hogp": True,
             "service": "", "desc": "HID-compliant vendor-defined device",
             "config_flags": 0, "guid": "{745a17a0-…}", "bt_addr": "F1:96:A2:63:67:1C",
             "is_keyboard": False, "is_mouse": False, "enabled": True},
        ],
        "live": [
            {"path": r"\\?\hid#…col01#a&1&0&0000", "vid": 0x18D1, "pid": 0x9450,
             "usage_page": 0x01, "usage": 0x06, "in_len": 9, "is_google": True},
            {"path": r"\\?\hid#…col04#a&1&0&0003", "vid": 0x18D1, "pid": 0x9450,
             "usage_page": 0xFF01, "usage": 0x01, "in_len": 21, "is_google": True},
        ],
        "ble": [{"kind": "低功耗 BLE", "key": "…", "friendly": "Chromecast Remote",
                 "desc": "", "enabled": True}],
    }

    def _fake_watch(kb_n: int, vendor_n: int):
        """假监听器：只填计数，不碰真实句柄。"""
        if hidwatch is None:
            return None
        w = hidwatch.ReportWatcher()
        w.key_events = [(now, "keyboard", "enter", 0x1C)] * kb_n
        c = hidwatch.HidCollection(r"\\?\hid#…col04#a&1&0&0003",
                                   0x18D1, 0x9450, 0xFF01, 0x01, 21)
        c.opened = True
        c.reports = [(now, bytes([0x01, 0x00, 0x00] + [0] * 18))] * vendor_n
        w.collections = [c]
        return w

    def run(*, ok_scan: int, rm_scan: int,
            kb_n: int = 1, vendor_n: int = 0) -> tuple[int, str]:
        """同一批数据、只换扫描码，分别走「无法区分」和「可以区分」两条分支。"""
        fake = [
            # 阶段 0：物理键盘 Enter
            (now, 0, "enter", ok_scan, False),
            (now, 0, "enter", ok_scan, False),
            # 阶段 1：遥控器 OK
            (now, 1, "enter", rm_scan, False),
            (now, 1, "enter", rm_scan, False),
            # 阶段 2/3：Esc，扫描码不同 → 结论 2 走"可以区分"
            (now, 2, "escape", 0x01, False),
            (now, 3, "escape", 0x0E, False),
            # 阶段 4 故意留空 → 验"一次都没有"提示
            # 阶段 5/6：普通键 + 非预期键名
            (now, 5, "up", 0x48, True),
            (now, 6, "media play pause", 0x22, False),
        ]
        rc = _write_report(fake, quiet=True, snap=fake_snap,
                           watcher=_fake_watch(kb_n, vendor_n))
        return rc, REPORT.read_text(encoding="utf-8")

    old, REPORT = REPORT, Path(os.environ.get("TEMP", ".")) / "_rvb_diag_selftest.txt"
    try:
        rc_same, text_same = run(ok_scan=0x1C, rm_scan=0x1C)
        rc_diff, text_diff = run(ok_scan=0x1C, rm_scan=0x2C)
        # 键盘有事件 → 判读应落到"按键能到达 Windows"
        _rc_k, text_kbd = run(ok_scan=0x1C, rm_scan=0x1C, kb_n=2, vendor_n=0)
        # 键盘 0 事件、厂商页有报告 → 判读应落到"Windows 看不见它"
        _rc_v, text_vendor = run(ok_scan=0x1C, rm_scan=0x1C, kb_n=0, vendor_n=1)
    finally:
        REPORT.unlink(missing_ok=True)
        REPORT = old

    checks = [
        (rc_same == 0 and rc_diff == 0, "返回值"),
        ("无法区分" in text_same, "两端扫描码相同 → 判定为无法区分"),
        ("可以实现设备级精确区分" in text_diff, "两端扫描码不同 → 判定为可以区分"),
        ("一次都没有" in text_same, "空阶段有提示"),
        ("media play pause" in text_same, "非预期键名照样进总表"),
        ("0x48" in text_same and "keypad=True" in text_same, "扫描码/小键盘标志有输出"),
        # 新增：HID 硬件身份（结论 0）与按键落点（结论 4）
        ("结论 0" in text_same and "HID Keyboard Device" in text_same,
         "报告含 HID 硬件身份（结论 0）"),
        ("厂商自定义" in text_same, "厂商自定义集合被点名（Windows 不处理）"),
        ("按键落点" in text_same, "报告含按键落点（结论 4）"),
        ("键盘事件收到了" in text_kbd,
         "键盘有事件 → 判读指向程序层"),
        ("只出现在厂商自定义页" in text_vendor,
         "只有厂商页有报告 → 判读指向「要自己解报告」（关键分支）"),
    ]
    bad = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {msg}")
    if bad:
        print(f"SELFTEST FAILED（{len(bad)} 项）")
        return 1
    print("DIAG SELFTEST OK")
    return 0


def _pause_if_frozen() -> None:
    """打包版双击运行：报告打完窗口会立刻关掉，用户根本来不及看。

    源码环境下不暂停 —— diag-remote.bat 末尾已经有 pause 了，
    这里再来一次只会让人多按一次回车。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        input("\n按【回车】键关闭本窗口…")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    _rc = main()
    _pause_if_frozen()
    raise SystemExit(_rc)
