"""日志的三件事：**轮转** / **脱敏** / **raw HID 开关**（2026-09-29 审查报告 P2）。

① 轮转
------
`bridge.log` 以前用 `logging.FileHandler`，只涨不换 —— 真机上到过十几 MB
（`tools/` 里那几个读日志的脚本都得 seek 着读尾巴，就是因为太大）。
长期跑的用户不会去清，而排错时又必须看它。改成 **5 MB × 3 份**。

② 脱敏
------
日志里到处是蓝牙地址（本机适配器地址、远端 MAC），而用户排错时**第一件事
就是把日志发出来**。所以默认把地址中间几位打掉，只留**前 2 / 后 2 字节** ——
既能分辨"是哪个设备"，又不至于把完整的链路标识贴到聊天窗口里。
（只留 2 个字节也认得出：同机只有一根蓝牙棒、一只遥控器。）
想关掉：`config.json` 里 `"log_redact": false`。

③ raw HID 默认不记
------------------
`raw=02 42 00` 这类原始报告字节只对开发有用，长期记在盘上没必要
（也是"能反推用户按了什么"的材料）。要看的时候把 `config.json` 的
`"log_raw_hid"` 打开。
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 5 MB × 3 份 ≈ 20 MB 上限。够放下"最近一次出问题的全过程"，
# 又不会让用户目录悄悄涨到几百 MB。
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

# 脱敏规则。⚠ 带分隔符的那种必须排在前面 —— 虽然 12 位裸 hex 的模式
# 匹配不到带冒号的串（冒号不是 hex 字符），但顺序写清楚更不容易被人改坏。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # f1:96:a2:63:67:1c  /  f1-96-a2-63-67-1c
    (re.compile(r"(?i)\b([0-9a-f]{2})[-:](?:[0-9a-f]{2}[-:]){4}([0-9a-f]{2})\b"),
     r"\1:xx:xx:xx:xx:\2"),
    # f196a263671c —— `DeviceAddressCache=…` / 节点名尾段那种 12 位裸 hex
    (re.compile(r"(?i)\b([0-9a-f]{2})[0-9a-f]{8}([0-9a-f]{2})\b"),
     r"\1…\2"),
)

_redact = True
_raw_hid = False


def set_redact(on: bool) -> None:
    global _redact
    _redact = bool(on)


def set_raw_hid(on: bool) -> None:
    global _raw_hid
    _raw_hid = bool(on)


def raw_hid_enabled() -> bool:
    return _raw_hid


def redact(text: str) -> str:
    """把文本里的蓝牙地址打掉中间几位。

    ⚠ **受 `set_redact` 控制**：关掉时原样返回。这个判断只放在**这里**一处，
    调用方（`RedactingFormatter`）不再自己判 —— 两处各判一次的话，早晚会
    只改其中一处，变成"关了但还在打码"或者反过来的静默错误。
    """
    if not _redact:
        return text
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


def raw_suffix(raw) -> str:
    """按键日志末尾那段 `（raw=…）`：默认**不出现**。

    `raw` 是 bytes；给 None 时返回空串（调用方不用自己判）。
    """
    if not _raw_hid or raw is None:
        return ""
    try:
        return f"（raw={raw.hex(' ')}）"
    except AttributeError:                      # 传进来的不是 bytes
        return f"（raw={raw}）"


class RedactingFormatter(logging.Formatter):
    """先正常格式化（这样 `%s` 的参数已经填好），再对**整行**做脱敏。

    为什么不用 `Filter`：Filter 拿到的是 `record.msg` 和 `record.args` 分开的
    两半，参数里的地址根本不在 `msg` 上；只有格式化之后才是一整行。
    """

    def format(self, record: logging.LogRecord) -> str:
        # `redact()` 自己会看开关，这里不再判一次（单一闸门，见 redact 的说明）。
        return redact(super().format(record))


def install(log_file: str | Path, level: int = logging.INFO,
            stream: bool = True) -> None:
    """把 root logger 换成「轮转文件 + 可选控制台」。

    显式替换 root 的 handlers（而不是 `basicConfig`）—— `basicConfig` 在
    root 已经有 handler 时**什么都不做**，那样"轮转"就悄悄没生效，
    而现象是"日志又涨到几十 MB 了"，根本不会有人往这里想。
    """
    # 开关声明在最前面，读起来就知道这个函数会动它们（见函数末尾的赋值）。
    global _redact, _raw_hid

    handlers: list[logging.Handler] = [
        RotatingFileHandler(str(log_file), maxBytes=MAX_BYTES,
                            backupCount=BACKUPS, encoding="utf-8",
                            errors="replace"),
    ]
    if stream:
        sh = logging.StreamHandler()
        # 中文日志在 cp936/cp1252 控制台上会抛 UnicodeEncodeError ——
        # 那是"日志把程序搞崩"，比乱码糟糕得多。
        try:
            sh.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                       # noqa: BLE001
            pass
        handlers.append(sh)

    fmt = RedactingFormatter("%(asctime)s %(message)s")
    for h in handlers:
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:                       # noqa: BLE001
            pass
    for h in handlers:
        root.addHandler(h)

    # 装 handler 的同时把两个开关**重置成安全默认**（脱敏开、raw 关）。
    # 为什么：调用方（main.py）读配置失败时走的是 `except` 分支、**不会**调
    # set_redact —— 那一瞬间最不该发生的事，就是把完整地址写进日志。
    # 把默认值放在这里，等于"读不到配置"也仍然是脱敏的。
    _redact = True
    _raw_hid = False
