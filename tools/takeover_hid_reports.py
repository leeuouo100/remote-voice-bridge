"""临时接管遥控器的 HID 服务（0x1812），自己订 Report —— 纯软件，可逆。

## 为什么只剩这一条路

已经确证的事实：

  · 5 天日志里出现过的键名**全是物理键盘敲的字**（字母/数字/enter/backspace…）
    加上程序自己注入的语音热键；`volume` / `media` / `browser` / `home` 一个都没有，
    连「键到了但解析不出名字」（`🔘 HID 按键【无名】`）都是 0 条。
  · 遥控器的 5 路 HID 集合打得开的全 0 条；两个私有服务（`AE40/AE42`、`D343BFC0/C5`）
    订上了也是 0 条。
  · `0x1812` 被 Windows 的 HOGP 栈（`mshidumdf`）独占：我们自己订不上，
    而它也没把报告转成键盘事件交给系统。

所以还剩**唯一一个没测过的面**：`0x1812` 本身。
把 Windows 的那一个设备节点临时停掉 → 服务空出来 → 我们自己订阅 Report 特征，
直接看遥控器到底发不发按键报告。

## 这一步能把话说死

  收到 Report      → 🎯 按键走 0x1812，本项目可以自己解码（**纯软件就能修**）
  一条都没有       → 遥控器确实不发按键，软件层没有可做的事（不会再有无谓的尝试）

## 安全设计（三条，都是硬性的）

1. **默认只读**：不带 `--go` 只做体检 + 打印"如果执行会动什么"，一个字节都不改。
2. **恢复写在 PowerShell 那一侧**：禁用 → 睡 N 秒 → 启用，是**同一条命令**。
   Python 这边就算崩了、被 Ctrl+C 了，PowerShell 照样会把设备恢复回来。
3. 结束时**回读设备状态**确认恢复成功，没恢复就大声报出来。

用法：

    # 先体检（不改任何东西）
    python tools\\takeover_hid_reports.py

    # 真做（需要管理员；建议先退出桥程序）
    python tools\\takeover_hid_reports.py --go --seconds 25
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402

_setup_utf8()

from config import APP_VERSION, CONFIG_DIR  # noqa: E402

OUT = CONFIG_DIR / "takeover-hid-reports.txt"

# 中文 Windows 的 PowerShell 往管道写的是 CP936，Python 按 UTF-8 解就成乱码。
# 必须在每条命令前面强制它输出 UTF-8 —— 只对带中文的输出有影响，但必须有。
PS_ENC = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"

HOGP = "00001812-0000-1000-8000-00805f9b34fb"
HID_PROTOCOL_MODE = "00002a4e-0000-1000-8000-00805f9b34fb"
HID_CONTROL_POINT = "00002a4c-0000-1000-8000-00805f9b34fb"
HID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"

# 禁用 → 睡 → 启用，一条命令。放 PowerShell 里跑，Python 崩了也能恢复。
#
# ⚠ 两条路都要走：`Disable-PnpDevice`（SetupAPI / `DICS_DISABLE`）和
#   `pnputil /disable-device`（另一套入口）。2026-09-22 真机实测第一条回
#   `DISABLE_FAIL: 不支持`（elevated 下也一样）—— 只写一条的话，这个实验会永远
#   卡在第一步，然后被记成"没测成"（好在没被记成"遥控器不发按键"）。
PS_CYCLE = r"""
$ErrorActionPreference = 'Stop'
$id = $env:RVB_DEV_ID

$how = 'none'
try {
    Disable-PnpDevice -InstanceId $id -Confirm:$false
    $how = 'pnpdevice'
    Write-Output 'DISABLED'
} catch {
    Write-Output ("DISABLE_FAIL: " + $_.Exception.Message)
    try {
        $r  = (& pnputil /disable-device "$id" 2>&1 | Out-String).Trim()
        $rc = $LASTEXITCODE
        Write-Output ("PNPUTIL_DISABLE: rc=" + $rc + " | " + $r)
        if ($rc -eq 0) { $how = 'pnputil'; Write-Output 'DISABLED' }
    } catch {
        Write-Output ("PNPUTIL_DISABLE_FAIL: " + $_.Exception.Message)
    }
}
Write-Output ("DISABLE_HOW=" + $how)

Start-Sleep -Seconds $([int]$env:RVB_HOLD_SEC)

# 恢复：两条路都试一遍。**不能**按"刚才用哪条禁的"挑恢复方式 ——
# 真没恢复回来的代价是用户设备哑掉，多试一条是免费的。
try {
    Enable-PnpDevice -InstanceId $id -Confirm:$false
    Write-Output 'ENABLED'
} catch {
    Write-Output ("ENABLE_FAIL: pnpdevice -> " + $_.Exception.Message)
}
try {
    $r2 = (& pnputil /enable-device "$id" 2>&1 | Out-String).Trim()
    Write-Output ("PNPUTIL_ENABLE: rc=" + $LASTEXITCODE + " | " + $r2)
} catch {
    Write-Output ("ENABLE_FAIL: pnputil -> " + $_.Exception.Message)
}
"""

PS_FIND = r"""
$ErrorActionPreference = 'SilentlyContinue'
$all = @(Get-PnpDevice -PresentOnly |
         Where-Object { $_.InstanceId -like '*00001812*' })
if ($all.Count -eq 0) { Write-Output 'NONE'; exit 0 }

# 同一个 0x1812 底下挂着一父 + 若干子：
#   BTHLEDEVICE\...\9&XXXX&N&YYYY   服务 mshidumdf  ← 真正握着 BLE 的 ATT/GATT 会话
#   HID\...&COL01\...               Keyboard / kbdhid
#   HID\...&COL03\...               Mouse   / mouhid
#   HID\...&COL02/04/05\...         厂商自定义集合（无服务）
# 全部列出来，免得又「测的不是你以为的那个节点」。
foreach ($n in $all) {
    $sv  = Get-PnpDeviceProperty -InstanceId $n.InstanceId -KeyName 'DEVPKEY_Device_Service'
    $svc = if ($null -eq $sv.Data) { '(none)' } else { $sv.Data }
    Write-Output ("NODE|" + $n.Class + "|" + $n.Status + "|" + $n.Problem + "|" +
                  $svc + "|" + $n.InstanceId)
}

# 要禁用的**必须**是 BTHLEDEVICE 那一个：只有它握着 0x1812 的 GATT 会话。
# 禁 HID 子集合（COLxx）完全没用 —— 会话还在父节点手里，
# 到时候一条报告都收不到，会得出「软件到头了」的**错误**结论。
$d = $all | Where-Object { $_.InstanceId -like 'BTHLEDEVICE*' } | Select-Object -First 1
if (-not $d) {
    Write-Output 'TARGETKIND=NONE'
    exit 0
}
Write-Output 'TARGETKIND=BTHLEDEVICE'
Write-Output ("ID=" + $d.InstanceId)
$p  = Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverDesc'
$s  = Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_Service'
$sv = Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverVersion'
Write-Output ("STATUS=" + $d.Status)
Write-Output ("PROBLEM=" + $d.Problem)
# PS 5.1 的 Get-PnpDevice 对象不一定带 Service 字段 → 必须显式取属性，
# 否则会输出一个「看起来是空」的值，分不清「真的没服务」还是「没读到」。
if ($null -eq $s.Data) { Write-Output 'SERVICE=(读不到这个属性)' }
else                   { Write-Output ("SERVICE=" + $s.Data) }
Write-Output ("DRIVER=" + $p.Data)
Write-Output ("DRVVER=" + $sv.Data)
"""


def ps(cmd: str, env_extra: dict | None = None, timeout: int = 120,
       capture: bool = True) -> subprocess.CompletedProcess:
    """跑一段 PowerShell。环境变量传参 —— 实例号里有 `\\` 和 `&`，
    拼进命令行是自找麻烦。"""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_ENC + cmd],
        capture_output=capture, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env, creationflags=0x08000000)


def parse_kv(text: str, key: str) -> str:
    for line in (text or "").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                            # noqa: BLE001
        return False


# ── GATT 那一半 ──────────────────────────────────────────────────────────

async def gatt_probe(seconds: int, say) -> tuple[int, str]:
    """连遥控器、找 0x1812、尽量订上 Report。返回 (收到报告条数, 结论标签)。"""
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.bluetooth import (
        BluetoothLEDevice, BluetoothConnectionStatus)
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattCommunicationStatus, GattClientCharacteristicConfigurationDescriptorValue)

    sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
    devs = await DeviceInformation.find_all_async_aqs_filter(sel)

    # 先把已配对的 BLE 设备**全列出来**，再逐个试着打开。
    # 2026-09-22 真机在这里翻过车：旧写法挑了第一个名字像遥控器的就直接
    # `from_id_async`，报 `E_INVALIDARG（提供的设备 ID 不是有效的 BluetoothLEDevice
    # 对象）`，而且**一个设备名都没打出来** —— 连"它挑的是谁"都查不到，
    # 整个实验只能记成"没测成"。全列 + 逐个试，把"挑错"从可能性里去掉。
    say(f"\n【已配对的 BLE 设备】共 {len(devs)} 个")
    named: list = []
    others: list = []
    for d in devs:
        nm = d.name or ""
        hit = "remote" in nm.lower() or "chromecast" in nm.lower()
        say(f"   {'→' if hit else ' '} {nm!r}  id={d.id[:78]}")
        (named if hit else others).append(d)

    ble = None
    used = None
    # 名字像遥控器的优先；一个都打不开再退到前 5 个别的（只是多开个句柄，
    # 没有 0x1812 会被下面的服务枚举筛掉，不会误判）。
    for d in (named + others[:5]):
        try:
            cand = await BluetoothLEDevice.from_id_async(d.id)
        except OSError as e:
            say(f"   ⚠ from_id_async({d.name!r}) 失败：{e}")
            continue
        if cand is None:
            say(f"   ⚠ from_id_async({d.name!r}) 返回 None（配对记录可能已失效）")
            continue
        ble, used = cand, d
        break
    if ble is None:
        if not devs:
            return 0, "连不上遥控器：没枚举到任何已配对的 BLE 设备"
        return 0, "连不上遥控器：所有已配对 BLE 设备都打不开（逐条原因见上）"
    say(f"   ✔ 用上了：{used.name!r}")
    if ble.connection_status != BluetoothConnectionStatus.CONNECTED:
        say("  BLE 未连接，等它醒来（按一下遥控器）最多 10 秒…")
        for _ in range(20):
            await asyncio.sleep(0.5)
            if ble.connection_status == BluetoothConnectionStatus.CONNECTED:
                break

    res = await ble.get_gatt_services_async()
    hogp = None
    for svc in res.services:
        if str(svc.uuid).lower() == HOGP:
            hogp = svc
            break
    if hogp is None:
        return 0, "遥控器上没枚举到 0x1812 服务"

    cr = await hogp.get_characteristics_async()
    if cr.status != GattCommunicationStatus.SUCCESS:
        why = ("ACCESS_DENIED（还是被 Windows 独占着）"
               if int(cr.status.value) == 3 else str(cr.status))
        say(f"  ⚠ 枚举 0x1812 的特征仍被拒：{why}")
        say("    → 说明禁用那一下没真正让 Windows 放手（可能没生效，或又被抢回去了）")
        return 0, f"特征枚举被拒：{why}"

    say(f"  ✅ 枚举成功！0x1812 下 {len(cr.characteristics)} 个特征：")
    reports, others = [], []
    for ch in cr.characteristics:
        v = int(getattr(ch.characteristic_properties, "value",
                        ch.characteristic_properties))
        cu = str(ch.uuid).lower()
        say(f"     · {cu}  props=0x{v:02X}"
            f"{'  [可 notify]' if v & 0x10 else ''}"
            f"{'  [可 write]' if (v & 0x08 or v & 0x04) else ''}")
        (reports if cu == HID_REPORT else others).append(ch)

    hits: list[tuple[float, bytes]] = []

    # 两个"叫醒"写入，都是尽力而为：失败不算致命。
    for ch in others:
        cu = str(ch.uuid).lower()
        blob = None
        what = ""
        if cu == HID_PROTOCOL_MODE:
            blob, what = bytes([0x01]), "Protocol Mode = Report(1)"
        elif cu == HID_CONTROL_POINT:
            blob, what = bytes([0x01]), "HID Control Point = Exit Suspend(1)"
        if blob is None:
            continue
        try:
            st = await ch.write_value_with_result_async(blob)
            ok = int(st.status.value) == 0
            say(f"  {'✅' if ok else '⚠'} 写 {what} → {'成功' if ok else st.status}")
        except Exception as e:                                   # noqa: BLE001
            say(f"  ⚠ 写 {what} 失败：{e.__class__.__name__}: {e}")

    if not reports:
        return 0, "枚举成功但一个 Report(0x2A4D) 特征都没有"

    def mk(ch):
        def cb(sender, args):
            try:
                data = bytes(args.characteristic_value)
            except Exception:                                    # noqa: BLE001
                data = b""
            hits.append((time.time(), data))
            print(f"  🔵 HID Report  {data.hex(' ')}", flush=True)
        return cb

    subs = 0
    for i, ch in enumerate(reports):
        try:
            ch.add_value_changed(mk(ch))
            await ch.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
            subs += 1
            say(f"  ✅ 已订阅 Report #{i}")
        except Exception as e:                                   # noqa: BLE001
            say(f"  ❌ 订阅 Report #{i} 失败：{e.__class__.__name__}: {e}")

    if not subs:
        return 0, "Report 特征都在，但一个都没订上"

    say(f"\n  开始听 {seconds} 秒 —— 请**依次按遥控器的每个键**（手别碰键盘）：")
    say("   方向上下左右 → 确认 → 返回 → 主页 → 音量＋ → 音量－ → 静音")
    t0 = time.time()
    while time.time() - t0 < seconds:
        await asyncio.sleep(0.25)

    return len(hits), "订上了" if hits else "订上了但 0 条"


# ── 主流程 ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="临时接管 0x1812 读遥控器按键报告")
    ap.add_argument("--go", action="store_true",
                    help="真的执行（默认只体检、不改任何东西）")
    ap.add_argument("--seconds", type=int, default=25, help="监听秒数（默认 25）")
    a = ap.parse_args()

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say("=" * 76)
    say(" 临时接管 HID 服务（0x1812）自己订 Report —— 看遥控器到底发不发按键")
    say(f" 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}   版本：v{APP_VERSION}")
    say("=" * 76)

    if not hasattr(sys, "getwindowsversion"):
        say("❌ 这个工具只能在 Windows 上跑")
        return 2

    admin = is_admin()
    say(f"\n管理员：{'是' if admin else '❌ 不是（--go 需要管理员）'}")

    # ── 静态：找到那个节点 ────────────────────────────────────────────
    r = ps(PS_FIND)
    text = (r.stdout or "").strip()
    kind = parse_kv(text, "TARGETKIND")
    dev_id = parse_kv(text, "ID")

    nodes = [ln.split("|") for ln in text.splitlines() if ln.startswith("NODE|")]
    if nodes:
        say(f"\n【静态】0x1812 底下一共 {len(nodes)} 个 PnP 节点（只读列出）")
        for parts in nodes:
            if len(parts) < 6:
                continue
            _cls, st, prob, svc, iid = parts[1], parts[2], parts[3], parts[4], parts[5]
            mark = "  ← 目标" if iid == dev_id else ""
            tail = iid.rsplit("\\", 1)[-1]
            say(f"   {_cls:<9} {st:<4} {svc:<12} {tail}{mark}")

    if kind == "NONE":
        say("\n❌ 没找到 0x1812 的 **BTHLEDEVICE** 父节点。")
        say("   （HID 子集合可能还在，但握着 GATT 会话的是父节点 —— 拿不到它就没法测）")
        say("   请确认遥控器已连上，然后重跑。")
        _write(say, lines, "没找到父节点")
        return 3
    if not dev_id:
        say("\n❌ 没找到 0x1812 的设备节点。可能遥控器没连上。")
        say(f"   （PowerShell 原始输出：{text[:200]!r}）")
        _write(say, lines, "没找到节点")
        return 3

    say(f"\n【目标】要临时禁用的是 {kind} 父节点（握着 0x1812 的那个）")
    for k in ("STATUS", "PROBLEM", "SERVICE", "DRIVER", "DRVVER"):
        v = parse_kv(text, k)
        # 「空」必须能跟「没读到」分开：读不到要说读不到，不能印个空白让人猜。
        say(f"   {k:<9} {v if v else '(读不到)'}")
    say(f"   ID        {dev_id}")
    if parse_kv(text, "SERVICE") != "mshidumdf":
        say("   ⚠ 服务不是 mshidumdf —— 上面这条结论的前提可能变了，"
            "请把报告发出来再决定要不要跑。")

    if not a.go:
        say("\n" + "─" * 76)
        say("【只读模式】如果加上 --go，接下来会发生这些事：")
        say(f"   1. 把上面这个节点 **临时禁用** {a.seconds + 15} 秒左右")
        say("      （`Disable-PnpDevice`，需要管理员）")
        say("   2. 禁用后 Windows 的 HOGP 栈就放开了 0x1812，我们再连上去枚举特征")
        say("   3. 订阅 Report(0x2A4D)，并按需写 Protocol Mode / HID Control Point")
        say(f"   4. 听 {a.seconds} 秒，请依次按遥控器的每个键")
        say("   5. **自动恢复**：禁用＋恢复是同一条 PowerShell 命令，"
            "就算这边崩了也会恢复")
        say("\n   现在什么都没改。要真做就加 --go。")
        _write(say, lines, "只读体检")
        return 0

    if not admin:
        say("\n❌ --go 需要管理员权限。请用「以管理员身份运行」重开。")
        _write(say, lines, "缺管理员")
        return 2

    hold = a.seconds + 15
    say(f"\n【执行】禁用节点 → 听 {a.seconds} 秒 → 自动恢复"
        f"（PowerShell 侧窗口 {hold} 秒）")

    # ⚠ 用 Popen 而不是 run：PowerShell 那条命令里睡着 hold 秒，
    #   阻塞等它的话监听就没时间做了。恢复动作在**它自己**手里，
    #   所以这边就算崩了/被 Ctrl+C，设备照样会回来。
    env = dict(os.environ)
    env.update({"RVB_DEV_ID": dev_id, "RVB_HOLD_SEC": str(hold)})
    p = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_ENC + PS_CYCLE],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", env=env,
        creationflags=0x08000000)

    say("  等 Windows 放手…")
    disabled = False
    ps_all: list[str] = []
    t0 = time.time()
    while time.time() - t0 < 25:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                break
            time.sleep(0.1)
            continue
        line = line.strip()
        if line:
            say("  PS> " + line)
            ps_all.append(line)
        if "DISABLED" in line:
            disabled = True
            break
        if "FAIL" in line:
            break
    if not disabled:
        say("  ⚠ 没等到 DISABLED —— 可能没禁用成功（下面照样会试一次 GATT，"
            "结果会被记成「没测成」或「被 Windows 封死」）")
    time.sleep(2.0)                      # 给 PnP 一点稳定时间

    n, tag = asyncio.run(gatt_probe(a.seconds, say))

    say("\n  ── 等 PowerShell 把设备恢复回来 ──")
    try:
        rest = p.stdout.read() or ""
    except Exception:                                            # noqa: BLE001
        rest = ""
    for ln in rest.splitlines():
        if ln.strip():
            say("  PS> " + ln.strip())
            ps_all.append(ln.strip())

    # 「禁不掉」和「没测成」必须分开 —— 它们指向完全相反的下一步。
    # 2026-09-22 真机：两个 API 都拒（`Disable-PnpDevice` 回「不支持」、
    # `pnputil` 回「Cannot disable critical system device」），只读查得到那个
    # devnode 的 DevNodeStatus 里**没有 `DN_DISABLEABLE` 位**（0x0180000A）。
    # 这是**结论性**的：不是我们没测成，是 Windows 不许任何人让它放手。
    ps_low = "\n".join(ps_all).lower()
    disable_blocked = (not disabled) and (
        "critical system device" in ps_low or "disable_fail" in ps_low
        or "disable_how=none" in ps_low)
    if disable_blocked:
        say("  🚫 两个入口都拒绝禁用这个节点（见上面的 DISABLE_FAIL / PNPUTIL_DISABLE）")
    try:
        p.wait(timeout=hold + 40)
    except subprocess.TimeoutExpired:
        p.kill()
        say("  ❌ PowerShell 超时已强杀 —— **请手动确认这个设备已启用**！")

    r2 = ps(PS_FIND)
    st2 = parse_kv(r2.stdout or "", "STATUS")
    say(f"\n【恢复确认】节点状态 = {st2 or '(读不到)'}")
    if st2.upper() != "OK":
        say("  ❌ 没恢复成 OK —— 请到「设备管理器」里把这个设备手动启用！")
    else:
        say("  ✅ 已恢复（Windows 又把它当键盘了，行为跟测之前一样）")

    say("\n" + "=" * 76)
    say("【判读】")
    if n:
        say(f"  → 🎯 收到 {n} 条 HID 报告（{tag}）。")
        say("     遥控器的按键确实走 0x1812 —— 本项目可以自己订这个服务、")
        say("     按报告字节解按键：**纯软件就能把按键映射做出来，不用加硬件。**")
        say("     下一步是把这套订阅搬进主程序，并设计「什么时候让 Windows 让位」。")
    elif disable_blocked:
        say("  → 🚫 这条路被 **Windows 自己**封了 —— 不是「没测成」，"
            "也不是「遥控器不发按键」：")
        say("     `Disable-PnpDevice` 回「不支持」（＝ `ERROR_NOT_SUPPORTED`），")
        say("     `pnputil /disable-device` 回「Cannot disable critical system device」。")
        say("     只读查一下就知道为什么：这个 devnode 的 `DevNodeStatus`")
        say("     **没有 `DN_DISABLEABLE` 位**（2026-09-22 真机 = `0x0180000A`，")
        say("     设备管理器里「禁用设备」也是灰的）⇒")
        say("     Windows 不许可用户态让它放手，`0x1812` 就一直被 HOGP 栈占着，")
        say("     所以下面看到的是 `ACCESS_DENIED`。")
        say("     ⇒ **用户态读遥控器 HID 报告这条路到此为止。**")
        say("       再往下只剩内核过滤驱动或换硬件 —— 两条都不是这个项目要走的。")
    elif tag.startswith("特征枚举被拒") or "连不上" in tag or "没找到" in tag \
            or "没枚举到" in tag:
        say(f"  → ⚠ 没测成（{tag}）—— 这一轮**不能**下结论，请把报告发出来。")
    else:
        say(f"  → ⚠ 服务订上了，但 {a.seconds} 秒里**一条报告都没有**（{tag}）。")
        say("     说明遥控器**根本不往 HID 送按键**（不是 Windows 拦的）。")
        say("     ⇒ 软件层没有可做的事了：这个遥控器除语音键外的按键，")
        say("       在任何主机上都不发 HID 报告。到此为止，不用再加硬件去试。")
    _write(say, lines, "完成")
    return 0


def _write(say, lines: list[str], tag: str) -> None:
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        Path(OUT).write_text("\n".join(lines) + "\n", encoding="utf-8")
        say(f"\n报告已写出：{OUT}  （{tag}）")
    except Exception as e:                                       # noqa: BLE001
        say(f"\n⚠ 写报告失败：{e}")


if __name__ == "__main__":
    sys.exit(main())
