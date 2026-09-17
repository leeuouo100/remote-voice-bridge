"""直接读遥控器的「厂商自定义页」原始报告，把按键解码出来。

── 为什么要这个模块 ────────────────────────────────────────────────
遥控器（VID 18D1 / PID 9450）暴露 5 路 HID 集合：键盘 / 消费类 / 鼠标 /
厂商页 0xFF01 / 厂商页 0xFF80。前 3 路 Windows 认，厂商页**完全不理**——
按键若发在厂商页，不产生任何键盘/鼠标/媒体事件，改映射表永远无效。

本项目的按键（config.CHROMECAST_BUTTONS 的 usage：up=0x03、back=0x0B…）
正是发在厂商页上，所以必须自己开这两路集合、按下面格式解出来。

── 报告格式（来自参考实现 VincentKingHsu/vRemoter 的
   ChromecastRemoteHIDBridge.swift，与本机实测的 21 字节报告一致）────
    reportID == 0x01
    data[0]  = 报告 ID(1)
    data[1]  = 用法码   ← 就是 CHROMECAST_BUTTONS 的 usage
    usage==0 表示「松手」；非 0 表示「按下某个键」

复用 hidwatch.HidCollection 做打开/读线程（它已处理 CreateFileW 的标志位），
这里只做「轮询取新报告 + 解码 + 去重」，不重复实现打开逻辑。
"""
from __future__ import annotations

import logging
import threading
import time

import hidinfo
import hidwatch

from config import CHROMECAST_BUTTONS

logger = logging.getLogger(__name__)

# usage(int) → 按钮 id。语音键的 usage 是字符串 "voice"，这里自然被排除，
# 不会和 ATVV 语音通道抢。
USAGE_TO_BUTTON: dict[int, str] = {
    int(v["usage"]): k
    for k, v in CHROMECAST_BUTTONS.items()
    if isinstance(v["usage"], int)
}

_REPORT_ID = 0x01
# 同一按键的重复上报（两路集合都发、或去抖）在这个窗口内只算一次
_DEDUPE_SEC = 0.06
# 已消费的报告超过这个条数就把缓冲清掉（见 _poll）
_TRIM_AT = 200
# 隔多久重扫一次 HID 集合（遥控器重连后要重新挂载，见 _recover）
_RESCAN_SEC = 10.0
# 隔多久往日志里打一次「各路集合到底收到过几条报告」。
#
# 为什么非要有这一行：在这个审计行之前，"按键没反应"在日志里的样子是
# **一片空白** —— 和"程序没在跑""遥控器没连上""集合没打开"长得一模一样。
# 用户按了十几次键、把日志发回来，里面什么线索都没有，只能靠猜。
# 有了它，一次按键就能定案：
#   · 审计行里厂商页计数在涨 → 报告到了，问题在解码或映射表
#   · 审计行里全是 0      → 报告根本没来，问题在蓝牙/HID 那一层
_AUDIT_SEC = 20.0
# ⚠ 但审计行本身不能每 20 秒无条件刷一条：桥是要连着跑几天的，
#   按键一直不来时一天就是 4300+ 行重复警告 —— 把日志淹掉，
#   真正有用的那几行反而找不到了（这就是"体检报告刷屏"式的新坑）。
#   规矩：前 _AUDIT_LOUD_TIMES 次照常报（刚启动/刚排查时一眼就看得到），
#         之后就压到这个静默窗口，按键后来翻日志仍然看得到。
_AUDIT_QUIET_SEC = 300.0
_AUDIT_LOUD_TIMES = 3


def decode_report(raw: bytes) -> tuple[str | None, bool]:
    """把一条原始报告解成 (按钮 id, 是否按下)。

    返回 (None, False) 表示这条报告不是按键（无法识别），调用方应忽略。
    抽成纯函数是为了能脱离硬件直接单测 —— 报告格式是本模块最容易写错、
    又最难靠"看起来能用"发现的地方。
    """
    if not raw:
        return None, False
    payload = raw[1:] if raw[0] == _REPORT_ID else raw
    if not payload:
        return None, False
    usage = payload[0]
    if usage == 0:
        # 松手：具体松开哪个键由调用方用「上一个按下的键」补上
        return "<up>", False
    btn = USAGE_TO_BUTTON.get(usage)
    if not btn:
        return None, False
    return btn, True


class RemoteHidButtons:
    """后台读厂商页报告 → 回调 on_button(button_id, is_down)。"""

    def __init__(self, on_button) -> None:
        self._on = on_button
        self._cols: list[hidwatch.HidCollection] = []
        self._cursor: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_down: str | None = None          # 松手时要补上的键
        self._recent: dict[tuple[str, bool], float] = {}
        self._last_scan = 0.0
        # 各路集合累计收到多少条原始报告（col.key → 条数）。
        # 这是"按键到底有没有到本程序"的唯一硬证据，见 _AUDIT_SEC 的注释。
        self._counts: dict[str, int] = {}
        self._audit_at = 0.0
        self._zero_warned = 0            # 「0 条」告警报过几次（前几次不压）
        self._zero_warn_at = 0.0         # 上次「0 条」告警的时间
        self._audit_logged_total = -1    # 上次打明细时的累计条数
        self._audit_detail_at = 0.0      # 上次打明细的时间
        # 已报过的"奇怪报告"，(来源, 前缀) → 只报一次，别刷屏
        self._unknown_seen: set[tuple[str, bytes]] = set()

    # ── 生命周期 ─────────────────────────────────────────────────────
    def start(self) -> int:
        """打开遥控器的所有 HID 集合并开始后台解码。

        返回**厂商页**打开了几路 —— 按键映射能不能生效只看这个数。
        （非厂商页也会打开，但只用于记录"按键落在哪一路"的证据，不派发动作。）
        """
        self._stop.clear()
        self._open_all()
        n_vendor = len([c for c in self._cols if c.is_vendor])
        if self._cols:
            self._thread = threading.Thread(target=self._poll, daemon=True,
                                            name="remote-hid")
            self._thread.start()
        return n_vendor

    def stop(self) -> None:
        self._stop.set()
        for c in self._cols:
            try:
                c.close()
            except Exception:                       # noqa: BLE001
                pass
        self._cols.clear()
        self._cursor.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ── 打开集合 ─────────────────────────────────────────────────────
    def _open_all(self) -> int:
        """打开所有还没打开的 Google 厂商页集合。返回本次新打开的路数。"""
        try:
            cols = hidinfo.live_hid_collections()
        except Exception as e:                       # noqa: BLE001
            logger.warning("枚举 HID 集合失败：%s", e.__class__.__name__)
            return 0
        have = {c.path for c in self._cols}
        opened = 0
        for d in cols:
            if not d.get("is_google"):
                continue
            if d["path"] in have:
                continue
            # ⚠ 不只厂商页 —— 遥控器的**每一路**都开。
            #   「按键到底落在哪一路」目前还没有被证实过：诊断报告里键盘/消费类/
            #   鼠标/两个厂商页**全是 0 条**，无法区分"按键没来"和"来了但我们没听那一路"。
            #   非厂商页这里只解+记日志、不派发动作（Windows 自己会处理它们，
            #   我们再发一次就是双份），但**落点证据**必须留下来 ——
            #   下一次排查靠的就是这一行。
            #   键盘页/鼠标页通常被系统独占（err=5 打不开，属正常）。
            c = hidwatch.HidCollection(d["path"], d["vid"], d["pid"],
                                       d["usage_page"], d["usage"], d["in_len"])
            if c.open():
                self._cols.append(c)
                self._cursor[id(c)] = 0
                opened += 1
                kind = "厂商页（接管按键）" if c.is_vendor else "非厂商页（只记录）"
                logger.info("📡 已打开集合 %s in_len=%s %s", c.key, d["in_len"], kind)
            else:
                logger.info("ℹ️ 集合打不开（系统独占，属正常）：%s（%s）",
                            c.key, c.open_error)
        return opened

    def _recover(self) -> None:
        """遥控器重连后重新挂载集合。

        ⚠ 不这么做就是「重连一次，按键又不灵了」：
        设备断开时 hidwatch 的读线程 ReadFile 失败会直接 break 退出，
        而 HID 设备路径在重连后**可能整条都换了** —— 死掉的线程不会自己回来，
        旧的 path 也未必还能打开。所以这里定期：丢掉死掉的 → 重扫 → 补开新的。
        """
        dead = [c for c in self._cols
                if not (getattr(c, "_thread", None) and c._thread.is_alive())]
        if not dead and self._cols:
            return
        for c in dead:
            logger.info("🔌 集合读线程已退出（%s）→ 重新扫描", c.key)
            try:
                c.close()
            except Exception:                       # noqa: BLE001
                pass
        if dead:
            self._cols = [c for c in self._cols if c not in dead]
            self._cursor = {id(c): self._cursor.get(id(c), 0) for c in self._cols}
        self._open_all()

    # ── 内部 ─────────────────────────────────────────────────────────
    def _poll(self) -> None:
        while not self._stop.is_set():
            for c in list(self._cols):
                try:
                    buf = c.reports
                    i = self._cursor.get(id(c), 0)
                    n = len(buf)
                    if i < n:
                        for _ts, raw in buf[i:n]:
                            self._handle(raw, c)
                        self._cursor[id(c)] = n
                        # hidwatch 的 reports 是一直追加的普通 list —— 桥程序是要
                        # 连着跑几天的，不裁剪就会把每条原始报告永远留在内存里。
                        # 消费完就丢掉已读的部分，游标归零。
                        if n > _TRIM_AT:
                            try:
                                del buf[:n]
                            except Exception:         # noqa: BLE001
                                pass
                            self._cursor[id(c)] = 0
                except Exception as e:               # noqa: BLE001
                    logger.warning("读厂商页报告异常：%s", e.__class__.__name__)

            now = time.time()
            if now - self._last_scan > _RESCAN_SEC:
                self._last_scan = now
                try:
                    self._recover()
                except Exception as e:               # noqa: BLE001
                    logger.warning("重扫厂商页集合异常：%s", e.__class__.__name__)

            if now - self._audit_at > _AUDIT_SEC:
                self._audit_at = now
                try:
                    self._audit(now)
                except Exception as e:               # noqa: BLE001
                    logger.warning("通道审计异常：%s", e.__class__.__name__)
            time.sleep(0.02)

    def _audit(self, now: float | None = None) -> None:
        """定期把「哪一路收到过几条报告」打进日志。

        这一个方法就是"按键没反应"这个问题的判决书 —— 在它出现之前，
        日志对 HID 按键是一片空白，和"遥控器没连上"完全无法区分：
          · 厂商页计数在涨      → 报告到了，问题在我们的解码或映射表
          · 非厂商页涨、厂商页 0 → 按键落在 Windows 认的那几路，得改读法
          · 一路都没涨          → 报告根本没到本程序（蓝牙/HID 层的问题）

        ⚠ 判决书不等于要一直念：见 _AUDIT_QUIET_SEC。这里已经按"前几次照报、
        之后压到静默窗口"限流；**判决能力一分没少，噪音降两个数量级**。
        now 可注入，是为了能脱离真实时钟单测限流逻辑（见 check_remote_hid.py）。
        """
        now = time.time() if now is None else now
        total = sum(self._counts.values())

        if total == 0:
            self._zero_warned += 1
            if (self._zero_warned > _AUDIT_LOUD_TIMES
                    and now - self._zero_warn_at < _AUDIT_QUIET_SEC):
                return                               # 静默期：能力还在，只是不再刷屏
            self._zero_warn_at = now
            heads = "、".join(c.key for c in self._cols) or "（一路都没打开）"
            tail = ("" if self._zero_warned > 1
                    else "（这条不会再每 20 秒刷：前几次照报，之后每 5 分钟提醒一次）")
            logger.warning(
                "📊 HID 通道审计：已挂 %d 路集合、累计 **0 条**原始报告｜%s｜"
                "此刻按遥控器的方向/确认/返回/音量键都**不会**产生任何日志 —— "
                "说明按键没有到达本程序，问题在蓝牙/HID 那一层，"
                "不在按键映射表上。（语音键不走 HID，它照常工作）%s",
                len(self._cols), heads, tail,
            )
            return

        # 有报告：只在"条数变了"或"过一个心跳周期"时打明细，同样防刷屏。
        if (total == self._audit_logged_total
                and now - self._audit_detail_at < _AUDIT_QUIET_SEC):
            return
        self._audit_logged_total = total
        self._audit_detail_at = now
        detail = "、".join(f"{k}={v}" for k, v in sorted(self._counts.items()))
        logger.info("📊 HID 通道审计：累计 %d 条｜%s", total, detail)

    def _handle(self, raw: bytes, col=None) -> None:
        # 计数先记：无论这条报告认不认识，**它来过**这件事本身才是证据。
        # （认不认识决定后面派不派发动作，见下面的分支。）
        if col is not None:
            self._counts[col.key] = self._counts.get(col.key, 0) + 1
        else:
            self._counts["<未知集合>"] = self._counts.get("<未知集合>", 0) + 1

        # 非厂商页（键盘/消费类/鼠标）：Windows 自己会处理，我们再发一次就是双份。
        # 但**必须记下来** —— "按键到底落在哪一路"这个问题的答案就在这一行里。
        # 前几种不同的报告各记一次，之后静默，避免刷屏。
        if col is not None and not col.is_vendor:
            sig = ("nonvendor:" + col.key, raw[:4])
            if sig not in self._unknown_seen:
                self._unknown_seen.add(sig)
                logger.info("📥 集合 %s 收到原始报告 %s（非厂商页，只记录不派发）",
                            col.key, raw.hex(" "))
            return

        btn, is_down = decode_report(raw)
        if btn == "<up>":
            btn = self._last_down
            is_down = False
        if not btn:
            # 认不出的用法码只报一次 —— 这正是"静默丢弃"的老坑：
            # 不记日志的话，用户和排障的人都不知道遥控器其实发了东西。
            sig = ("vendor", raw[:2])
            if raw and sig not in self._unknown_seen:
                self._unknown_seen.add(sig)
                logger.info("🔘 厂商页报告 %s → 用法码不认识，已忽略", raw.hex(" "))
            return

        now = time.time()
        key = (btn, is_down)
        if now - self._recent.get(key, 0.0) < _DEDUPE_SEC:
            return                                  # 两路集合重复上报 / 抖动
        self._recent[key] = now

        if is_down:
            self._last_down = btn
        else:
            self._last_down = None if self._last_down == btn else self._last_down

        logger.info("🔘 厂商页按键 → 按钮「%s」%s（raw=%s）",
                    btn, "按下" if is_down else "松开", raw.hex(" "))
        try:
            self._on(btn, is_down)
        except Exception as e:                       # noqa: BLE001
            logger.exception("按键回调异常：%s", e)
