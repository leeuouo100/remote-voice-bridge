"""遥控器按键旁路 —— 注入 WUDFHost.exe，从 HID 驱动内部把报告抄出来。

── 为什么需要这一路（先读这段，别急着改）────────────────────────────
在此之前，本项目读遥控器按键走的是 `remote_hid.py`：自己 CreateFile 打开
厂商页（0xFF01 / 0xFF80）读原始报告。**真机实测那一路一直是 0 条**，
于是有了「除语音键外所有按键都没反应」这个悬案。

2026-09-29 定案：**报告不是没发，是在 WUDFHost.exe 内部就被消费掉了。**
BLE HID（HOGP）在 Windows 上是 UMDF 驱动，跑在 WUDFHost.exe 里；它通过
`ntdll!NtDeviceIoControlFile` + IOCTL `0x80018483` 把 GATT 特征读上来，
报告就在那次调用的**输出缓冲区**里 —— 而这一层在用户态 HID 接口之前，
所以 `ReadFile` / Raw Input / 键盘钩子**全都看不到**。

唯一出路：用 Frida 注入 WUDFHost，在那次 IOCTL 的输出缓冲区上抄一份。
机制移植自 VibeMote（https://github.com/Tilkmilk/vibe-mote，MIT），
见 THIRD_PARTY_NOTICES.md。

── 与 remote_hid.py 的关系 ──────────────────────────────────────────
两条路**并行、互补**，解出来的 button_id 都喂给 main.py 同一个
`_on_hid_button` → 现有的 `resolve_button` 映射表 / 控制台 / config.json
**全都不用改**：
  · frida_hid（本模块）：注入驱动宿主读报告 —— 真机上唯一能拿到按键的那一路
  · remote_hid：自开厂商页读报告 —— 对某些驱动/适配器组合仍然有效，保留
任一路拿到按键都能用；两路都没拿到就是"报告真的没来"。

── 降级 ────────────────────────────────────────────────────────────
没装 frida、注入被安全软件拦、宿主找不到 —— 一律只记日志、不抛异常，
语音功能完全不受影响。这一路是"读不到"，不是"桥起不来"。

── 报告格式 ────────────────────────────────────────────────────────
同一个 WUDFHost 还服务别的蓝牙键鼠，它们吐 9 字节键盘报告，混在一起。
遥控器只发两种（见 decode_tap_report）：
  · 3 字节消费类页：[0x02][usage_lo][usage_hi]（16 位 usage）
  · N 字节厂商页：  [0x01][usage]…（8 位 usage）
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("rvb.frida")


def _tap_js_path() -> Path:
    """定位 frida_tap.js —— 源码环境在模块旁，打包后由 PyInstaller 放到 _MEIPASS。"""
    import sys
    cands: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(Path(base) / "frida_tap.js")
    cands.append(Path(__file__).resolve().parent / "frida_tap.js")
    for c in cands:
        if c.is_file():
            return c
    return cands[-1]

# ── 消费类页 usage → 按钮 id ─────────────────────────────────────────
#
# 遥控器在**消费类页**（0x000C）上发的是 16 位 usage；这张表来自 VibeMote
# 对同款遥控器（Google TV / Chromecast，VID 18D1 PID 9450）的受控采集
# （其自述 14/14 全命中）。与本项目 config.CHROMECAST_BUTTONS 的 button_id
# 一一对齐，所以两条路解出来的东西能喂进同一张映射表。
#
# ⚠ 语音键**不在**这张表里：它走 ATVV 的 BLE 数据通道（ATVV CTL op 0x04），
#   不经过 HID。混进来就是"按语音键触发两次"。
CC_USAGE_TO_BUTTON: dict[int, str] = {
    0x019E: "power",
    0x0189: "input",
    0x0042: "up",
    0x0043: "down",
    0x0044: "left",
    0x0045: "right",
    0x0041: "ok",
    0x0224: "back",
    0x0223: "home",
    0x00E9: "vol_up",
    0x00EA: "vol_down",
    0x00E2: "mute",
    0x0077: "youtube",
    0x0078: "netflix",
}
BUTTON_TO_CC_USAGE: dict[str, int] = {v: k for k, v in CC_USAGE_TO_BUTTON.items()}


def decode_tap_report(raw: bytes) -> tuple[str | None, bool]:
    """把一条从 WUDFHost 抄出来的原始报告解成 (按钮 id, 是否按下)。

    返回 (None, False) = 认不出（调用方忽略）。
    返回 ("<up>", False) = 松手帧（具体松开哪个键由调用方用「上一个按下的键」补）。
    抽成纯函数是为了能脱离硬件单测 —— 报告格式是本模块最容易写错、
    又最难靠"看起来能用"发现的地方（同 remote_hid.decode_report 的理由）。
    """
    if not raw or len(raw) < 2:
        return None, False

    if raw[0] == 0x02 and len(raw) >= 3:
        # 消费类页：16 位 usage
        usage = raw[1] | (raw[2] << 8)
        if usage == 0:
            return "<up>", False
        btn = CC_USAGE_TO_BUTTON.get(usage)
        return (btn, True) if btn else (None, False)

    if raw[0] == 0x01:
        # 厂商页：8 位 usage（与 remote_hid 同一套解码口径）
        usage = raw[1]
        if usage == 0:
            return "<up>", False
        from remote_hid import USAGE_TO_BUTTON
        btn = USAGE_TO_BUTTON.get(usage)
        return (btn, True) if btn else (None, False)

    return None, False


def block_usages_for(keymap: dict | None) -> tuple[list[int], list[int]]:
    """按当前 keymap 算出「要抹掉原生动作」的 usage 列表。

    只抹**已被映射**的键（空值 / "native" = 禁用，不抹 —— 那种情况用户
    就是想让这个键保持系统原生行为）。返回 (消费类页 usages, 厂商页 usages)。
    """
    km = keymap or {}
    cc: list[int] = []
    vp: list[int] = []
    try:
        from config import CHROMECAST_BUTTONS
    except Exception:                                  # noqa: BLE001
        CHROMECAST_BUTTONS = {}
    for btn, target in km.items():
        t = str(target or "").strip()
        if not t or t == "native":
            continue
        u = BUTTON_TO_CC_USAGE.get(btn)
        if u is not None:
            cc.append(u)
        v = (CHROMECAST_BUTTONS.get(btn) or {}).get("usage")
        if isinstance(v, int):
            vp.append(v)
    return sorted(cc), sorted(vp)


# ── 定位遥控器的 HID 驱动宿主（WUDFHost.exe）─────────────────────────
_BTHLE_ENUM = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
_HID_SVC_PREFIX = "{00001812-0000-1000-8000-00805f9b34fb}"
_WUDF_DIAG = r"Device Parameters\WUDFDiagnosticInfo"
READ_IOCTL = 0x80018483


def hid_devices(vid: str = "18D1", pid: str = "9450", any_hid: bool = False):
    """[(HostPid, 服务节点名)] —— BLE HID（0x1812）设备所在的驱动宿主。

    节点名里带着**这台设备是谁**（`..._Dev_VID&0218d1_PID&9450_...`），
    顺手返回出去，日志里就能写清"挂上的是哪台设备"。
    any_hid=True：不挑 VID/PID，任何 BLE HID 设备都要（兼容款兜底）。
    """
    import winreg
    out: list[tuple[int, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BTHLE_ENUM) as root:
            i = 0
            while True:
                try:
                    svc = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                low = svc.casefold()
                if not low.startswith(_HID_SVC_PREFIX.casefold()):
                    continue
                if not any_hid and (vid.lower() not in low or pid.lower() not in low):
                    continue
                with winreg.OpenKey(root, svc) as sk:
                    j = 0
                    while True:
                        try:
                            inst = winreg.EnumKey(sk, j)
                        except OSError:
                            break
                        j += 1
                        try:
                            with winreg.OpenKey(
                                    root, f"{svc}\\{inst}\\{_WUDF_DIAG}") as dk:
                                out.append((int(winreg.QueryValueEx(dk, "HostPid")[0]),
                                            svc))
                        except (OSError, TypeError, ValueError):
                            continue
    except OSError:
        pass
    return out


def find_wudfhost_pid(vid: str = "18D1", pid: str = "9450",
                      any_hid: bool = False) -> int | None:
    """遥控器 HID 服务（0x1812）所在 WUDFHost 进程的 PID（找不到返回 None）。"""
    devs = hid_devices(vid, pid, any_hid)
    return devs[0][0] if devs else None


def wudfhost_pids() -> list[int]:
    """列出机器上所有 WUDFHost.exe 的 PID（诊断用）。

    一台机器上常有**好几个** WUDFHost（蓝牙、指纹、摄像头各一个），
    而"遥控器的按键读调用"只出现在服务它的那个里面。
    """
    import subprocess
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WUDFHost.exe", "/NH", "/FO", "CSV"],
            capture_output=True, timeout=15,
            creationflags=0x08000000).stdout or b""
    except Exception:                                  # noqa: BLE001
        return pids
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            text = out.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = out.decode("utf-8", "replace")
    for line in text.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower().startswith("wudfhost") and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


class RemoteHidTap(threading.Thread):
    """后台线程：注入 WUDFHost → 收报告 → 解 usage → 回调 on_button(btn, down)。

    与 remote_hid.RemoteHidButtons 的接口保持一致（on_button(button_id, is_down)），
    所以 main.py 里两条路喂的是同一个回调、同一张映射表。
    """

    def __init__(self, on_button, vidpid: tuple[str, str] = ("18D1", "9450")) -> None:
        super().__init__(daemon=True, name="frida-hid-tap")
        self._on = on_button
        self.vidpid = vidpid
        self._stop = threading.Event()
        self._script = None
        self._session = None
        self._attached_pid: int | None = None
        self._attached_at = 0.0
        self._mapping: dict = {}
        self._held: set[int] = set()          # 当前按住的 usage（过滤长按连发）
        self._last_down: str | None = None    # 松手时要补上的键
        self._recent: dict[tuple[str, bool], float] = {}
        self._hb: dict = {}
        self.ready = False
        self.note = "未启动"
        self.reports = 0
        self._compat = False
        self._av_hinted = False
        self._lock = threading.Lock()

    # ── 对外 ─────────────────────────────────────────────────────────
    def set_mapping(self, mapping: dict | None) -> None:
        """更新 keymap，并把「要抹掉原生动作的 usage」下发给钩子。"""
        with self._lock:
            self._mapping = dict(mapping or {})
        cc, vp = block_usages_for(self._mapping)
        if self._script:
            try:
                self._script.post({"type": "block", "cc": cc, "vendor": vp})
            except Exception:                          # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()
        self._teardown()

    def _teardown(self) -> None:
        try:
            if self._script:
                self._script.unload()
        except Exception:                              # noqa: BLE001
            pass
        try:
            if self._session:
                self._session.detach()
        except Exception:                              # noqa: BLE001
            pass
        self._script = None
        self._session = None
        self._attached_pid = None
        self.ready = False

    # ── 主循环 ───────────────────────────────────────────────────────
    def run(self) -> None:
        try:
            import frida                                # noqa: F401
        except ImportError:
            self.note = "缺 frida（按键旁路不可用，语音与 remote_hid 不受影响）"
            logger.info("🔓 按键旁路：未安装 frida，跳过（语音键仍可用；"
                        "pip install frida 可启用）")
            return

        try:
            source = _tap_js_path().read_text(encoding="utf-8")
        except OSError as e:
            self.note = f"读不到 frida_tap.js：{e}"
            logger.warning("🔓 按键旁路：%s", self.note)
            return

        # 重试节奏：Frida 第一次注入要**一次提权**（frida-helper）。失败就猛重试
        # 会把授权框反复弹出来（用户看到的是"怎么一直弹"）。所以指数退避，
        # 权限类失败连撞 3 次就停 5 分钟，只提示一次。
        delay = 2.0
        perm_fails = 0
        hinted = False
        while not self._stop.is_set():
            try:
                pid, dev = self._next_host_pid()
                if not pid:
                    self.note = "没找到遥控器的 HID 驱动宿主（遥控器配对了吗？）"
                    if self._stop.wait(min(delay, 10.0)):
                        break
                    delay = min(delay * 2, 30.0)
                    continue

                if self._session is None:
                    logger.info("🔓 按键旁路：挂上蓝牙驱动宿主 WUDFHost (PID %d)%s",
                                pid, f" ← {dev}" if dev else "")
                    self._session = frida.attach(pid)
                    self._script = self._session.create_script(source)
                    self._script.on("message", self._on_message)
                    # ⚠ PID 必须在 load() **之前**记上：脚本一 load 就同步发出
                    #   `ready` 消息，_on_message 里那行日志要用它；晚一步就会
                    #   打出「就绪（注入 WUDFHost PID None）」（踩过一次）。
                    self._attached_pid = pid
                    self._attached_at = time.time()
                    self._script.load()
                    self.set_mapping(self._mapping)
                    self._hb = {}
                    self._av_hinted = False
                    delay = 2.0
                    perm_fails = 0
                else:
                    # 宿主换了/没了就得重新挂：蓝牙重置、驱动重载都会让 WUDFHost
                    # 换 PID，而我们可能还挂在已经死掉的进程上。
                    now = (find_wudfhost_pid(*self.vidpid)
                           or find_wudfhost_pid(*self.vidpid, any_hid=True)
                           or self._attached_pid)
                    if self._attached_pid and (now != self._attached_pid
                                               or not self._pid_alive(self._attached_pid)):
                        logger.info("🔓 按键旁路：宿主换了或没了（PID %s → %s），重新挂",
                                    self._attached_pid, now)
                        self._teardown()
                        continue
                    time.sleep(1.0)
            except Exception as e:                      # noqa: BLE001
                self._teardown()
                name, text = type(e).__name__, str(e)
                perm = ("PermissionDenied" in name or "AccessDenied" in name
                        or "unable to access" in text)
                if perm:
                    perm_fails += 1
                    if perm_fails >= 3:
                        delay = 300.0
                        self.note = ("按键旁路需要一次管理员授权（已暂停自动重试，"
                                     "5 分钟后再试一次）")
                        if not hinted:
                            hinted = True
                            logger.warning(
                                "🔓 按键旁路：注入需要提权，但授权没给 —— 已停下不再反复"
                                "弹框。想用按键映射请让程序以管理员身份跑一次。")
                    else:
                        self.note = f"挂载失败（{perm_fails}/3）：需要一次提权授权"
                        delay = min(delay * 2, 30.0)
                else:
                    self.note = f"挂载失败：{name}: {text}"
                    if ("TransportError" in name or "connection is closed" in text) \
                            and not self._av_hinted:
                        self._av_hinted = True
                        logger.warning(
                            "🔓 按键旁路：注入被中断了（TransportError）—— 装了杀软/EDR 的"
                            "机器（公司电脑尤其常见）几乎都是安全软件拦的。正规解法是请 IT"
                            "把程序目录加进白名单（我们不改系统设置、不关 Defender）。"
                            "放行之前语音功能不受影响，只是方向键/OK/音量用不了。")
                    delay = min(delay * 2, 60.0)
                logger.info("🔓 按键旁路：%s（%.0f 秒后重试）", self.note, delay)
                if self._stop.wait(delay):
                    break

    def _next_host_pid(self):
        devs = hid_devices(*self.vidpid)
        if not devs:
            devs = hid_devices(*self.vidpid, any_hid=True)
            if devs:
                low = devs[0][1].casefold()
                v, p = (self.vidpid or ("", ""))
                self._compat = not (str(v).lower() in low and str(p).lower() in low)
        if not devs:
            return None, ""
        return devs[0][0], devs[0][1]

    def _pid_alive(self, pid: int) -> bool:
        """进程还在不在。⚠ 两个坑：OpenProcess 失败不一定是"进程不存在"
        （权限不足也会失败），那种必须当"活着"，否则会疯狂重挂。"""
        if not pid:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if h:
                k32.CloseHandle(h)
                return True
            return ctypes.get_last_error() != 87           # 87 = 进程不存在
        except Exception:                                  # noqa: BLE001
            return True

    # ── 消息处理 ─────────────────────────────────────────────────────
    def _on_message(self, message, data) -> None:
        if message.get("type") == "error":
            logger.warning("🔓 按键旁路脚本错误：%s",
                           str(message.get("description"))[:200])
            return
        payload = message.get("payload") or {}
        kind = payload.get("kind")
        if kind == "ready":
            self.ready = True
            self.note = "运行中"
            logger.info("✅ 按键旁路：就绪（注入 WUDFHost PID %s）", self._attached_pid)
        elif kind == "report":
            try:
                raw = bytes.fromhex(str(payload.get("raw") or "").replace(" ", ""))
            except ValueError:
                return
            self._handle_report(raw)
        elif kind == "block_ack":
            logger.info("🔓 按键旁路：已屏蔽 %s 个键的原生动作",
                        payload.get("count", 0))
        elif kind == "hb":
            self._hb = payload
        elif kind == "error":
            logger.warning("🔓 按键旁路：%s", payload.get("message"))

    def _handle_report(self, raw: bytes) -> None:
        # ⚠ 方法名不能叫 `_handle` —— Python 3.13 的 threading.Thread 自带一个
        #   同名属性（OS 线程句柄），子类一覆盖它，`start()` 就会报
        #   "'_thread._ThreadHandle' object is not callable"。踩过一次。
        self.reports += 1
        btn, is_down = decode_tap_report(raw)
        if btn == "<up>":
            btn = self._last_down
            is_down = False
        if not btn:
            return
        now = time.time()
        key = (btn, is_down)
        if now - self._recent.get(key, 0.0) < 0.06:
            return                                     # 去抖 / 两路重复
        self._recent[key] = now
        if is_down:
            self._last_down = btn
        else:
            self._last_down = None if self._last_down == btn else self._last_down
        logger.info("🔘 按键旁路 → 按钮「%s」%s（raw=%s）",
                    btn, "按下" if is_down else "松开", raw.hex(" "))
        try:
            self._on(btn, is_down)
        except Exception as e:                          # noqa: BLE001
            logger.exception("按键旁路回调异常：%s", e)

    # ── 诊断 ─────────────────────────────────────────────────────────
    def selfcheck(self) -> str:
        """按需输出「钩子到底看到了什么」（供控制台/命令行排查）。

        真机上出现过"钩子挂上了、显示就绪、按键却完全没反应"，只看成功/失败
        分不清是"挂错了宿主"还是"这款遥控器报文格式不同"。这几个原始计数能分辨：
          · 读调用 0 次 + 收到 0 条   → 宿主不对
          · 读调用 N 次、长度里没有 3 → 报文格式不同（另一款遥控器）
          · 长度里有 3 / 收到过报文   → 这条路是通的
        """
        hb = self._hb or {}
        lens = hb.get("lens") or {}
        dist = "、".join(f"{k}字节×{v}" for k, v in sorted(lens.items())) or "无"
        seen = hb.get("seen") or {}
        names = []
        for k, n in sorted(seen.items(), key=lambda kv: -int(kv[1]))[:14]:
            try:
                kind, code = str(k).split(":", 1)
                code = int(code)
            except (ValueError, TypeError):
                continue
            if kind == "cc":
                nm = CC_USAGE_TO_BUTTON.get(code, "未知")
                names.append(f"{nm}(cc:0x{code:04X})×{n}")
            else:
                names.append(f"(vp:0x{code:02X})×{n}")
        return (f"按键旁路自检：宿主 PID {self._attached_pid}"
                f"（本机共 {len(wudfhost_pids())} 个 WUDFHost），"
                f"读调用 {int(hb.get('ioctl') or 0)} 次，输出长度：{dist}；"
                f"已收到 {self.reports} 条按键报告，已屏蔽 {int(hb.get('blocked') or 0)} 次。"
                + (" 已见按键：" + "、".join(names) if names else ""))
