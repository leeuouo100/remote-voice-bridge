"""日志：**轮转** / **脱敏** / **raw HID 默认不记**（审查报告 P2-6）。

背景
====
`bridge.log` 以前是 `logging.FileHandler`，**只涨不换** —— 真机上到过十几 MB
（`tools/` 里那几个读日志的脚本都得 seek 着读尾巴，就是因为太大）。而日志里
到处是蓝牙地址（适配器地址、远端 MAC），用户排错时**第一件事就是把日志发出来**；
`raw=02 42 00` 这类原始报告字节还能反推"用户按了什么"。三件事都要改。

这道闸钉两类东西
================
A 组 **行为级**（真起 handler、真写盘、真格式化）：
  A1  install() 之后 root 的 handler 是 `RotatingFileHandler`（不是 `FileHandler`）
  A2  **轮转真的会发生** —— 把 `MAX_BYTES` 调小、写满，必须出现 `bridge.log.1`
  A3  落盘内容**已经脱敏**（不是只在内存里脱）
  A4  脱敏规则：带分隔符的 MAC、12 位裸 hex、`DeviceAddressCache=…` 都要打掉
  A5  开关真的能关（关掉后原文照出）
  A6  raw 默认不出；打开后才出；传 None 恒不出
  A7  格式化器端到端：地址在 **args** 里（不是 msg 里）时也必须被打掉 ——
      这正是"用 Filter 做不到"的那一点（Filter 拿到的是 msg/args 分开的两半）
  A8  install() 会把开关重置成安全默认（读配置失败时靠它兜底）

B 组 **接线级**（源码文本，剥注释后再判）：
  B1  main.py 走 `logsetup.install(`，且**不再**有 `logging.basicConfig(`
  B2  main.py 在打第一行日志之前就把配置应用上了
  B3  按键日志不再裸打 raw（`frida_hid.py` 1 处 + `remote_hid.py` 3 处，
      全部换成 `logsetup.raw_suffix(`）
  B4  config.py 有 `log_redact=True` / `log_raw_hid=False` 两个字段
  B5  console_server.py 的白名单里有这两个字段（不进白名单 = 改了静默无效）

边界（**故意不管**的 hex 转储，别以为是漏了）
============================================
`log_raw_hid` 管的是**遥控器发上来的 HID 报告**。下面这些不是，所以不受它控制：

  · `main.py` 的 `📤 {tag} [{cmd.hex()}]` —— 是**本程序自己发出去**的 ATVV
    控制命令（MIC_OPEN 之类）。排"麦克风到底开没开"全靠它，不能关。
  · `main.py` 的 `收到未处理的控制指令 … 原始={data.hex()}` —— ATVV **控制
    通道**的负载（不是 HID 报告），而且**按 opcode 去重只报一次**。当初
    「静音键按了没反应」就是卡在这里查不出来，不能再把它变哑。
  · `hidwatch.py` 的 selfcheck 样本 —— 只在用户**主动**跑自检时打印一次，
    是"报告长什么样"的唯一证据，不是常驻日志。

反例自证：把 basicConfig 改回来、把白名单那行删掉、把 raw 改回裸打、
把默认值改成"脱敏关" —— 都必须报红。

用法： python tools/check_logging.py
输出： LOGGING OK  /  LOGGING FAILED（附具体哪几项）
"""
from __future__ import annotations

import io
import logging
import re
import sys
import tempfile
import tokenize
from logging.handlers import RotatingFileHandler
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logsetup  # noqa: E402

_checks: list[tuple[bool, str]] = []


def A(ok, msg) -> None:
    _checks.append((bool(ok), msg))


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# ── 源码判据（抽成纯函数，好让反例跑**同一条**判据）──────────────────────────
def _nocomment(src: str) -> str:
    """只剥**注释**，保留字符串字面量。

    ⚠ 为什么不能连字符串一起剥：`"log_redact": bool` 这种**白名单键**就在
    字符串里；剥掉它，B5 就永远看不出"白名单漏了字段"（假绿）。
    """
    return _strip(src, keep_strings=True)


def _code(src: str) -> str:
    """剥掉注释**和**字符串字面量，只留真代码。

    ⚠ 为什么需要：本项目到处在注释/文档字符串里**故意**引用"老写法"作对照
    —— `logsetup.py` 的 docstring 里就写着 `basicConfig`。拿整份文件判会假红。
    """
    return _strip(src, keep_strings=False)


def _strip(src: str, keep_strings: bool) -> str:
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and not keep_strings:
                # 用等长的占位符代替，保持"这里有东西"但不泄漏内容
                out.append('""')
                continue
            if tok.type in (tokenize.NEWLINE, tokenize.NL):
                out.append("\n")
                continue
            if tok.type in (tokenize.INDENT, tokenize.DEDENT,
                            tokenize.ENDMARKER):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src
    return "".join(out)


def _uses_logsetup_install(src: str) -> bool:
    return "logsetup.install(" in _code(src)


def _no_basic_config(src: str) -> bool:
    return "logging.basicConfig(" not in _code(src)


def _applies_config_before_first_log(src: str) -> bool:
    """install → set_redact → set_raw_hid 必须**都在**第一行日志之前。

    只查"这三个调用都存在且有序"是不够的 —— 真正的错误是"先打了一行日志
    （里面正好有适配器地址），后面才想起来脱敏"。所以比的是**位置**。
    """
    code = _code(src)
    i_install = code.find("logsetup.install(")
    i_redact = code.find("logsetup.set_redact(")
    i_raw = code.find("logsetup.set_raw_hid(")
    if min(i_install, i_redact, i_raw) < 0:
        return False
    if not (i_install < i_redact and i_install < i_raw):
        return False
    # 第一处"真的打日志"：logger.<level>( 或 logging.<level>( 的调用
    first_log = min(
        [p for p in (code.find("logger.info("), code.find("logger.warning("),
                     code.find("logger.error("), code.find("logger.debug("))
         if p >= 0] or [len(code) + 1])
    return max(i_install, i_redact, i_raw) < first_log


def _raw_not_logged_bare(src: str) -> bool:
    """按键日志必须走 `logsetup.raw_suffix(`，不能裸打 `raw.hex(`。"""
    text = _nocomment(src)
    return "logsetup.raw_suffix(" in text and "raw.hex(" not in text


def _config_has_log_fields(src: str) -> bool:
    code = _code(src)
    return (re_search(r"log_redact\s*:\s*bool\s*=\s*True", code)
            and re_search(r"log_raw_hid\s*:\s*bool\s*=\s*False", code))


def _allowed_block(src: str) -> str:
    """把 `/api/config` 里那个 `allowed = { … }` 字典抠出来。

    ⚠ 为什么不能直接在整份文件里搜 `"log_redact": bool`：`/api/config` 的
    **快照**里也有 `"log_redact": bool(getattr(cfg, …))`，一样能匹配上 ——
    于是"白名单被删掉"这件事**判不出来**（假绿，反例会自证成红的）。
    必须限定在 `allowed` 那个字典里。
    """
    text = _nocomment(src)
    m = re.search(r"allowed\s*=\s*\{", text)
    if m is None:
        return ""
    j = text.index("{", m.start())
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j:k + 1]
    return ""


def _whitelists_log_fields(src: str) -> bool:
    """白名单（`allowed` 字典）里必须有这两个键。

    ⚠ 判据要容忍空白：`_nocomment()` 是按 token 拼回来的，`"log_redact": bool`
    会变成 `"log_redact":bool`（冒号后没有空格）。用 `\\s*` 兜住，
    否则会因为"格式化风格变了"假红 —— 而假红会让人开始怀疑闸门本身。
    """
    block = _allowed_block(src)
    return bool(block) and (
        re_search(r'"log_redact"\s*:\s*bool', block)
        and re_search(r'"log_raw_hid"\s*:\s*bool', block))


def re_search(pat: str, text: str) -> bool:
    return re.search(pat, text) is not None


# ── A 组：行为级 ────────────────────────────────────────────────────────────
def _with_temp_log(fn, *, max_bytes: int, backups: int):
    """在临时目录里起一套日志，跑 fn(log_file, dir)，最后**恢复 root handlers**。

    ⚠ 必须恢复：install() 会替换 root 的 handlers，不恢复的话这道闸后面
    再有任何 logger 调用都会写进临时目录（而临时目录会被删掉）。
    """
    old_handlers = list(logging.getLogger().handlers)
    old_level = logging.getLogger().level
    old_bytes, old_backups = logsetup.MAX_BYTES, logsetup.BACKUPS
    tmp = Path(tempfile.mkdtemp(prefix="rvb-logcheck-"))
    logsetup.MAX_BYTES = max_bytes
    logsetup.BACKUPS = backups
    try:
        return fn(tmp / "bridge.log", tmp)
    finally:
        for h in list(logging.getLogger().handlers):
            try:
                h.flush()
                h.close()
            except Exception:                   # noqa: BLE001
                pass
            logging.getLogger().removeHandler(h)
        for h in old_handlers:
            logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(old_level)
        logsetup.MAX_BYTES, logsetup.BACKUPS = old_bytes, old_backups


def case_rotation() -> None:
    def run(log_file, tmp):
        logsetup.install(log_file, stream=False)
        handlers = logging.getLogger().handlers
        A(any(isinstance(h, RotatingFileHandler) for h in handlers)
          and not any(type(h) is logging.FileHandler for h in handlers),
          "A1 install() 之后 root 挂的是 RotatingFileHandler，"
          "不是只会涨的 FileHandler")
        A(len(handlers) == 1 and isinstance(handlers[0], RotatingFileHandler),
          "A1b stream=False 时只挂文件那一个（控制台那路是可选的）")

        log = logging.getLogger("rvb.logcheck")
        # 每行 ~90 字节、写 300 行 ≈ 27 KB，maxBytes 才 1200 ⇒ 必然轮转
        for i in range(300):
            log.info("第 %d 行 地址 f1:96:a2:63:67:1c 填充填充填充填充填充", i)
        for h in handlers:
            h.flush()

        files = sorted(p.name for p in tmp.iterdir())
        A("bridge.log.1" in files,
          f"A2 **轮转真的发生了**（写满 {logsetup.MAX_BYTES} 字节后出现 "
          f"bridge.log.1）—— 实际文件：{files}")
        A("bridge.log.2" in files,
          f"A2b 按 backupCount={logsetup.BACKUPS} 保留多份 —— 实际：{files}")
        A(not (tmp / "bridge.log.3").exists(),
          "A2c 备份份数**封顶**（不会无限涨）—— 有上限才叫「轮转」")

        # 落盘内容必须已经脱敏：把"只在内存里脱"这种假修法挡住
        body = (tmp / "bridge.log").read_text(encoding="utf-8", errors="replace")
        body += (tmp / "bridge.log.1").read_text(encoding="utf-8",
                                                errors="replace")
        A("f1:xx:xx:xx:xx:1c" in body,
          "A3 **落盘的文件里**地址已经打码（不是只在内存里脱）")
        A("f1:96:a2:63:67:1c" not in body,
          "A3b 文件里再也搜不到完整地址 —— 用户直接贴日志也不会漏")

    _with_temp_log(run, max_bytes=1200, backups=2)


def case_redaction() -> None:
    saved = logsetup._redact
    try:
        logsetup.set_redact(True)
        cases = [
            ("f1:96:a2:63:67:1c", "f1:xx:xx:xx:xx:1c", "带冒号的 MAC"),
            ("F1-96-A2-63-67-1C", "F1:xx:xx:xx:xx:1C", "带连字符的大写 MAC"),
            ("047f0ef2d294", "04…94", "12 位裸 hex（适配器地址）"),
            ("DeviceAddressCache=047f0ef2d294", "DeviceAddressCache=04…94",
             "注册表值那种写法"),
            ("3800254a2bc5", "38…c5", "另一台适配器"),
        ]
        for src, want, why in cases:
            got = logsetup.redact(src)
            A(got == want, f"A4 脱敏 {why}：{src} → {got}（期望 {want}）")

        # 不该误伤的东西
        untouched = [
            "version=1.0.22",            # 点分版本号
            "PID 13044",                 # 进程号
            "端口 51234",
            "CABLE Output",
        ]
        A(all(logsetup.redact(t) == t for t in untouched),
          "A4b 不误伤版本号 / PID / 端口 / 设备名（脱敏只认「地址形状」）")

        # A5 开关真的能关
        logsetup.set_redact(False)
        raw = "f1:96:a2:63:67:1c"
        A(logsetup.redact(raw) == raw,
          "A5 set_redact(False) 后原文照出（开关是真的，不是装饰）")
        logsetup.set_redact(True)
        A(logsetup.redact(raw) == "f1:xx:xx:xx:xx:1c",
          "A5b 再打开又恢复打码")
    finally:
        logsetup._redact = saved


def case_raw_hid() -> None:
    saved = logsetup._raw_hid
    try:
        logsetup.set_raw_hid(False)
        A(logsetup.raw_suffix(b"\x02\x42\x00") == "",
          "A6 raw HID **默认不出**（`raw=02 42 00` 不写进日志）")
        A(logsetup.raw_suffix(None) == "",
          "A6b 传 None 也返回空串（调用方不用自己判）")
        logsetup.set_raw_hid(True)
        A("02 42 00" in logsetup.raw_suffix(b"\x02\x42\x00"),
          "A6c 打开后才出（开发时要看得到）")
        logsetup.set_raw_hid(False)
        A(logsetup.raw_suffix(b"\x02\x42\x00") == "",
          "A6d 关掉后立刻不出")
    finally:
        logsetup._raw_hid = saved


def case_formatter() -> None:
    """A7：地址在 **args** 里时也必须被打掉。

    这是"用 Filter 做不到"的那一点 —— Filter 看到的是 `record.msg`（`适配器 %s`）
    和 `record.args`（`("f1:96:…",)`）**分开的两半**，地址根本不在 msg 上。
    所以格式化器必须**先 format 再 redact**。
    """
    rec = logging.LogRecord("rvb", logging.INFO, __file__, 1,
                            "适配器 %s 已连接", ("f1:96:a2:63:67:1c",), None)
    line = logsetup.RedactingFormatter("%(message)s").format(rec)
    A("f1:xx:xx:xx:xx:1c" in line and "f1:96:a2:63:67:1c" not in line,
      f"A7 格式化之后整行脱敏：args 里的地址也被打掉（{line!r}）")

    # 反例：模拟"只脱 record.msg"的 Filter 式实现 —— 必须**打不掉**
    rec2 = logging.LogRecord("rvb", logging.INFO, __file__, 1,
                             "适配器 %s 已连接", ("f1:96:a2:63:67:1c",), None)
    rec2.msg = logsetup.redact(str(rec2.msg))          # Filter 能碰到的只有这里
    filtered = logging.Formatter("%(message)s").format(rec2)
    A("f1:96:a2:63:67:1c" in filtered,
      "A7b [反例] 只脱 record.msg 的 Filter 式实现**打不掉** args 里的地址"
      "（这就是不能用 Filter 的原因）")


def case_safe_defaults() -> None:
    def run(log_file, tmp):
        logsetup.set_redact(False)
        logsetup.set_raw_hid(True)
        logsetup.install(log_file, stream=False)
        A(logsetup._redact is True and logsetup.raw_hid_enabled() is False,
          "A8 install() 把两个开关重置成安全默认（脱敏开、raw 关）——"
          "读配置失败时靠它兜底")

    _with_temp_log(run, max_bytes=1024, backups=1)


# ── B 组：接线级 ────────────────────────────────────────────────────────────
def case_wiring() -> None:
    main_src = _read("main.py")
    A(_uses_logsetup_install(main_src),
      "B1 main.py 走 `logsetup.install(` 装日志")
    A(_no_basic_config(main_src),
      "B1b main.py 里**没有** `logging.basicConfig(` —— basicConfig 在 root "
      "已有 handler 时什么都不做，「轮转」会悄悄失效")
    A(_applies_config_before_first_log(main_src),
      "B2 install → set_redact → set_raw_hid 全都在**第一行日志之前**"
      "（否则启动那几行正好含地址，脱敏就成了心理安慰）")

    for name in ("frida_hid.py", "remote_hid.py"):
        src = _read(name)
        A(_raw_not_logged_bare(src),
          f"B3 {name} 的按键日志走 `logsetup.raw_suffix(`，不再裸打 `raw.hex(`")

    A(_config_has_log_fields(_read("config.py")),
      "B4 config.py 有 `log_redact: bool = True` / `log_raw_hid: bool = False`")

    cs_src = _read("console_server.py")
    A(_whitelists_log_fields(cs_src),
      "B5 console_server.py 的白名单里有这两个字段（不进白名单 = 改了静默无效）")
    # 判据自身有效性：`allowed` 字典必须真的被抠出来（抠不到时上面那条会假红，
    # 而假红和真红在输出里长得一样 —— 所以单独钉一条）。
    block = _allowed_block(cs_src)
    A(bool(block) and '"gain"' in block,
      f"B5b 判据真的定位到了 `allowed` 字典（{len(block)} 字符），"
      "不是靠快照里的同名字段蒙过去的")


# ── C：反例自证 ─────────────────────────────────────────────────────────────
def case_negative() -> None:
    main_src = _read("main.py")
    cfg_src = _read("config.py")
    cs_src = _read("console_server.py")
    fh_src = _read("frida_hid.py")

    # ① 把 install 换回 basicConfig → B1 报红（B1b 一起红：新代码里就有 basicConfig）
    n1 = main_src.replace("logsetup.install(CONFIG_DIR / \"bridge.log\")",
                          "logging.basicConfig(level=logging.INFO)")
    A(n1 != main_src and not _uses_logsetup_install(n1)
      and not _no_basic_config(n1),
      "C1 [反例] 把 install() 换回 basicConfig → B1 / B1b 判不合格")

    # ② 把脱敏调用挪到第一行日志之后 → B2 报红
    n2 = main_src.replace("    logsetup.set_redact(", "    logger.info('x')\n    logsetup.set_redact(", 1)
    A(n2 != main_src and not _applies_config_before_first_log(n2),
      "C2 [反例] 先打一行日志、再应用配置 → B2 判不合格")

    # ③ 把按键日志改回裸打 raw → B3 报红
    n3 = fh_src.replace("logsetup.raw_suffix(raw)", "raw.hex(' ')")
    A(n3 != fh_src and not _raw_not_logged_bare(n3),
      "C3 [反例] 把 raw_suffix 换回 `raw.hex(' ')` → B3 判不合格")

    # ④ 把默认值改成"脱敏关" → B4 报红
    n4 = cfg_src.replace("log_redact:         bool  = True",
                         "log_redact:         bool  = False")
    A(n4 != cfg_src and not _config_has_log_fields(n4),
      "C4 [反例] 把 log_redact 默认值改成 False → B4 判不合格")

    # ⑤ 把白名单那行删掉 → B5 报红
    n5 = cs_src.replace('"log_redact": bool, "log_raw_hid": bool,', "")
    A(n5 != cs_src and not _whitelists_log_fields(n5),
      "C5 [反例] 把白名单里那两个字段删掉 → B5 判不合格"
      "（这就是「改了 config.json 没反应」的来源）")


def main() -> int:
    print("=" * 74)
    print(" 闸：日志轮转 / 脱敏 / raw HID 默认不记（P2-6）")
    print("=" * 74)

    case_rotation()
    case_redaction()
    case_raw_hid()
    case_formatter()
    case_safe_defaults()
    case_wiring()
    case_negative()

    fails = 0
    for ok, msg in _checks:
        print(f"  {'OK  ' if ok else '❌  '} {msg}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"LOGGING FAILED（{fails} 项）")
        print("  提示：坏掉的现象是「bridge.log 涨到几十 MB 没人敢删」、")
        print("        以及「用户把日志贴到群里，里面是他完整的蓝牙地址」。")
        return 1
    print(f"LOGGING OK（{len(_checks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
