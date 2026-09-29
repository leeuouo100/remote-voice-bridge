"""Frida 旁路「只认准那一台设备」的回归闸 —— 2026-09-29 审查报告 P1-8。

背景
====
`RemoteHidTap` 原先的目标选择有两处太宽：

  ① Python 侧：找不到精确 VID/PID 时**自动**回退到「任意 BLE HID 的第一项」；
     而匹配用的是 `vid.lower() in 节点名` 这种**子串**判断 —— 节点名里还有
     REV / 序列号 / MAC 段，短 VID 会在别处偶然命中，「精确匹配」其实在撞运气。
  ② JS 侧：只按 IOCTL 号过滤，**没有绑定 FileHandle** —— 同一个 WUDFHost 里
     若还服务别的蓝牙键鼠，它们的报告也会被 `nullify()` 原地改写。

后果是「可能采集或改写同一宿主里其他蓝牙键盘/遥控器的报告」，而且**完全静默**
（现象只是「某个键乱跳 / 串台」，没人会想到是注入的脚本干的）。

这道闸钉三件事（每条都配反例）：

  A. `frida_tap.js`：目标 FileHandle 先观察确认再改写；未确认时一个字节都不动；
     句柄不符就放过并计数
  B. `frida_hid.py`：VID/PID 用**解析出来的段**精确比对（不是子串）；
     兜底默认关、只有显式打开才用；注入前核对 PID 真的是 WUDFHost；
     重挂路径也走同一套（不许再单独写一遍 any_hid=True）
  C. 行为级（真跑）：用**假 winreg** 驱动 `hid_hosts()`，验证过滤真的生效；
     `_parse_vidpid` / `HidHost.matches` / `.mac` 的边界；含「子串陷阱」那两种名字

用法： python tools/check_frida_target.py
输出： FRIDA TARGET OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import re
import sys
import threading
import types
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_checks: list[tuple[bool, str]] = []


def A(ok, msg) -> None:
    _checks.append((bool(ok), msg))


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# 本机实测的节点名（2026-09-29 只读查注册表得来）—— 用它当「真样本」。
_REAL_SVC = ("{00001812-0000-1000-8000-00805f9b34fb}"
             "_Dev_VID&0218d1_PID&9450_REV&011b_f196a263671c")


# ── A. JS 侧 ────────────────────────────────────────────────────────────────
def _onleave_block(js: str) -> str:
    """把 `onLeave(retval) { … }` 整段抠出来（按大括号配对）。

    ⚠ 不能用「从某条注释往后到第一个 `}`」这种正则：`onLeave` 里到处是嵌套的
      `{}`，取到的只会是第一个内层块的结尾 —— 而"改写前的两道判断"恰恰在
      更后面，于是断言会因为**取不到**而变红（或者更糟：某天变成假绿）。
    """
    m = re.search(r"onLeave\s*\([^)]*\)\s*\{", js)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    return ""


def case_js() -> None:
    js = _read("frida_tap.js")

    A(re.search(r"let\s+targetHandle\s*=\s*null", js) is not None,
      "A1 JS 里有 targetHandle，且初始是 null（＝还没确认过目标）")
    A(re.search(r"function\s+looksLikeRemote", js) is not None,
      "A2 靠**报文形状**确认目标（遥控器 3 字节 0x02 / 首字节 0x01）"
      "—— 别的蓝牙键鼠吐 9 字节键盘报告，永远匹配不上")
    A(re.search(r"if\s*\(targetHandle\s*===\s*null\s*&&\s*bytes\s*&&\s*looksLikeRemote",
                js) is not None,
      "A3 第一次见到遥控器形状的报文才锁定句柄")

    tail = _onleave_block(js)
    A(bool(tail), "A4 抠得出 onLeave 的函数体")
    A(re.search(r"if\s*\(targetHandle\s*===\s*null\)\s*\{", tail) is not None,
      "A5 **未确认目标时一个字节都不改**"
      "（宁可少抹一次原生动作，也不许动别人的报告）")
    A(re.search(r"if\s*\(!\s*this\.h\.equals\(targetHandle\)\)\s*\{", tail) is not None,
      "A6 改写前比对 FileHandle：不是目标句柄就放过")
    A("skippedOther++" in tail,
      "A7 因句柄不符而放过的次数要记（否则「串台」发生时是个黑洞）")
    A("handles: otherHandles" in js and "locked: targetHandle !== null" in js,
      "A8 心跳里带 handles / locked（自检能说出「锁没锁上、宿主里有几台设备」）")


def case_js_negative() -> None:
    """反例：把句柄比对拿掉 / 把「未确认就不改」拿掉，A5/A6 必须能发现。"""
    js = _read("frida_tap.js")
    b1 = js.replace("if (!this.h.equals(targetHandle)) {", "if (false) {")
    A(re.search(r"if\s*\(!\s*this\.h\.equals\(targetHandle\)\)\s*\{",
                _onleave_block(b1)) is None,
      "[反例] 去掉句柄比对（＝谁的报告都敢改）→ A6 判不合格")
    b2 = js.replace("if (targetHandle === null) {", "if (false) {")
    A(re.search(r"if\s*\(targetHandle\s*===\s*null\)\s*\{",
                _onleave_block(b2)) is None,
      "[反例] 去掉「未确认就不改」→ A5 判不合格")


# ── B. Python 侧（静态）─────────────────────────────────────────────────────
def case_py_static() -> None:
    src = _read("frida_hid.py")

    A(re.search(r"def _parse_vidpid\(", src) is not None,
      "B0 有 _parse_vidpid()（把 VID/PID 当**段**解析，而不是当子串找）")
    A("_VIDPID_RE" in src and "vid&([0-9a-f]+)_pid&([0-9a-f]+)" in src,
      "B1 用正则抠 VID&…_PID&… 两段")
    A(re.search(r"m\.group\(1\)\.lower\(\)\[-4:\]", src) is not None,
      "B2 VID 取**后 4 位**（Windows 的 VID 段带两位前缀：VID&0218d1）")
    A(re.search(r'vid\.lower\(\)\s+not\s+in', src) is None
      and re.search(r'pid\.lower\(\)\s+not\s+in', src) is None,
      "B3 不再用「VID 是不是节点名的子串」这种判断（B0 的正则解析取代了它）")

    m = re.search(r"def __init__\(\s*self,\s*on_button(.*?)\)\s*->", src, re.S)
    sig = m.group(1) if m else ""
    A("allow_any_hid: bool = False" in sig,
      "B4 RemoteHidTap 的 allow_any_hid **默认 False**（默认不许猜第一项）")
    A("expect_mac" in sig,
      "B5 构造参数里有 expect_mac（比 VID/PID 更硬的「就是这台」）")
    A("h.mac == self.expect_mac" in src,
      "B6 节点名里必须出现这个 MAC 才算对上")
    A(re.search(r"if not self\.allow_any_hid:\s*\n\s*return None", src) is not None,
      "B7 没显式打开兜底时，找不到就**返回 None**（不是「随便挑一台」）")

    A("def is_wudfhost(" in src and "if not is_wudfhost(pid):" in src,
      "B8 attach 之前核对 PID 真的是 WUDFHost.exe（PID 会被回收）")
    A("def process_image_path(" in src and "QueryFullProcessImageNameW" in src,
      "B9 身份校验读的是**进程映像路径**")
    A(re.search(r"def is_wudfhost\(.*?known = wudfhost_pids\(\)", src, re.S) is not None,
      "B10 读不到映像路径时退一步问 tasklist（两层校验，不是「读不到就放行」）")

    A(re.search(r"now, _dev = self\._next_host_pid\(\)", src) is not None,
      "B11 重挂路径也走 _next_host_pid()（否则 P1-8 在这里又被放开一次）")
    A(re.search(r"find_wudfhost_pid\(\*self\.vidpid,\s*any_hid=True\)", src) is None,
      "B11b 代码里不再有裸的 find_wudfhost_pid(..., any_hid=True) 调用")

    A("hid_frida_any_hid" in src,
      "B12 找不到目标时提示里带上 hid_frida_any_hid（用户知道下一步点哪）")


def case_py_negative() -> None:
    """反例：把精确解析换回子串、把默认改成 True，对应断言必须变红。"""
    src = _read("frida_hid.py")
    b1 = src.replace("if not any_hid and parsed != want:", "if False:")
    A("if not any_hid and parsed != want:" not in b1,
      "[反例] 去掉精确过滤 → B 组的过滤类断言判不合格")
    b2 = src.replace("allow_any_hid: bool = False", "allow_any_hid: bool = True")
    A("allow_any_hid: bool = False" not in b2,
      "[反例] 把兜底默认改成 True → B4 判不合格")
    b3 = src.replace("if not self.allow_any_hid:", "if False:")
    A(re.search(r"if not self\.allow_any_hid:\s*\n\s*return None", b3) is None,
      "[反例] 去掉「没显式打开就返回 None」→ B7 判不合格")


# ── C. 行为级 ───────────────────────────────────────────────────────────────
def _install_fake_winreg(tree: dict):
    """把假 winreg 塞进 sys.modules，让 `hid_hosts()` 里的 `import winreg` 拿到它。

    ⚠ 必须**换掉模块本身**（而不是打补丁）：`hid_hosts` 里写的是
      `import winreg`，函数内导入走的是 `sys.modules` 查找。
    """
    mod = types.ModuleType("winreg")
    mod.HKEY_LOCAL_MACHINE = object()

    class _K:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

    def _resolve(path):
        cur = tree
        for part in path.split("\\"):
            if part not in cur:
                raise OSError(2, "系统找不到指定的文件。")
            cur = cur[part]
        return cur

    def OpenKey(_root, path, *_a):
        if path.startswith("SYSTEM"):
            return _K(tree)
        return _K(_resolve(path))

    def EnumKey(key, i):
        try:
            return list(key._data)[i]
        except IndexError:
            raise OSError(259, "没有更多数据了。")

    def QueryValueEx(key, name):
        if name not in key._data:
            raise OSError(2, "系统找不到指定的文件。")
        return (key._data[name], 1)

    mod.OpenKey = OpenKey
    mod.EnumKey = EnumKey
    mod.QueryValueEx = QueryValueEx

    old = sys.modules.get("winreg")
    sys.modules["winreg"] = mod
    return old


def _restore_winreg(old) -> None:
    if old is None:
        sys.modules.pop("winreg", None)
    else:
        sys.modules["winreg"] = old


_UUID_HID = "00001812-0000-1000-8000-00805f9b34fb"


def _svc(vid: str, pid: str, mac: str, rev: str = "011b",
         uuid: str = _UUID_HID) -> str:
    return f"{{{uuid}}}_Dev_VID&02{vid}_PID&{pid}_REV&{rev}_{mac}"


def _fake_tree() -> dict:
    """三棵树：我们的遥控器 / **别的** BLE HID（VID、PID 都不同）/ 非 0x1812 服务。"""
    return {
        _svc("18d1", "9450", "f196a263671c"): {
            "9&1dc2feeb&2&0023": {"Device Parameters": {
                "WUDFDiagnosticInfo": {"HostPid": 13044}}},
            "9&3703180e&0&0023": {},          # 这个实例没有诊断信息（要能跳过）
        },
        _svc("abcd", "1234", "001122334455"): {
            "9&aaaa1111&0&0001": {"Device Parameters": {
                "WUDFDiagnosticInfo": {"HostPid": 999}}},
        },
        _svc("18d1", "9450", "f196a263671c",
             uuid="00001800-0000-1000-8000-00805f9b34fb"): {
            "9&1dc2feeb&2&0001": {},          # 不是 0x1812 服务（要能跳过）
        },
    }


def case_behavior() -> None:
    import frida_hid as fh

    A(fh._parse_vidpid(_REAL_SVC) == ("18d1", "9450"),
      f"C1 真节点名解析出 ('18d1','9450')（实际 {fh._parse_vidpid(_REAL_SVC)}）")
    A(fh._parse_vidpid("{00001812-0000-1000-8000-00805f9b34fb}") is None,
      "C2 没有 VID/PID 段的节点名 → None（不是「猜一个」）")

    h = fh.HidHost(host_pid=13044, svc=_REAL_SVC,
                   instance="9&1dc2feeb&2&0023", vid="18d1", dev_pid="9450")
    A(h.mac == "f196a263671c", f"C3 从节点名尾部解析出远端 MAC（实际 {h.mac}）")
    A(h.matches("18D1", "9450") and h.matches("18d1", "9450")
      and h.matches("0x18D1", "9450"),
      "C4 matches 对大小写 / 0x 前缀都成立")
    A(not h.matches("FFFF", "9450") and not h.matches("18d1", "FFFF"),
      "C5 任一段不对就不算匹配")

    # C6 ⚠ 子串陷阱：老的 `vid in 名字 and pid in 名字` 会怎么错
    trap1 = _svc("1234", "5678", "f00000000000", rev="18d1")
    A("18d1" in trap1.casefold() and "9450" not in trap1.casefold(),
      "C6a 构造出「VID 出现在 REV 段」的名字（老写法 vid in 名字 会误判）")
    A(fh._parse_vidpid(trap1) == ("1234", "5678"),
      "C6b 精确解析不被 REV 段里的 18d1 带跑（老写法会把它当成我们的遥控器）")
    trap2 = _svc("9450", "18d1", "f00000000000")
    A("18d1" in trap2.casefold() and "9450" in trap2.casefold(),
      "C6c 构造出「VID/PID 被对调」的名字（老写法两个子串都在 → 误判为匹配）")
    A(fh._parse_vidpid(trap2) == ("9450", "18d1"),
      "C6d 精确解析认出这是对调的（9450/18d1 ≠ 18d1/9450）")

    old = _install_fake_winreg(_fake_tree())
    try:
        exact = fh.hid_hosts("18D1", "9450")
        A([x.host_pid for x in exact] == [13044],
          f"C7 精确匹配只挑出我们的那台（实际 {[x.host_pid for x in exact]}）"
          "—— 没诊断信息的实例、非 0x1812 的服务、别的设备都要被排掉")
        none = fh.hid_hosts("FFFF", "FFFF")
        A(none == [],
          f"C8 VID/PID 对不上时**返回空**（实际 {len(none)} 个）"
          "—— 老写法会在这里自动回退到「任意第一台」")
        allh = fh.hid_hosts("18D1", "9450", any_hid=True)
        A(sorted(x.host_pid for x in allh) == [999, 13044],
          "C9 any_hid=True 才把别的 BLE HID 也带上（实际 "
          f"{sorted(x.host_pid for x in allh)}）")
        A([p for p, _svc_name in fh.hid_devices("18D1", "9450")] == [13044],
          "C10 兼容薄封装 hid_devices() 口径一致（仍然返回 (pid, 节点名) 二元组）")
        A(fh.find_wudfhost_pid("18D1", "9450") == 13044
          and fh.find_wudfhost_pid("FFFF", "FFFF") is None,
          "C11 find_wudfhost_pid 同样不再乱兜底")
    finally:
        _restore_winreg(old)

    # C12 反例：把精确过滤拿掉（＝ any_hid 恒真）→ C8 那条必须翻过来
    src = _read("frida_hid.py")
    broken = src.replace("if not any_hid and parsed != want:", "if False:")
    A(broken != src, "[反例] 能构造出「去掉精确过滤」的版本（锚点存在）")
    old2 = _install_fake_winreg(_fake_tree())
    # ⚠ 必须**注册成真模块**再 exec：`@dataclass` 会去
    #   `sys.modules[cls.__module__].__dict__` 里查字段类型，模块不在就
    #   `AttributeError: 'NoneType' object has no attribute '__dict__'`。
    mod = types.ModuleType("frida_hid_broken")
    sys.modules["frida_hid_broken"] = mod
    try:
        exec(compile(broken, "frida_hid_broken.py", "exec"), mod.__dict__)
        none_b = mod.hid_hosts("FFFF", "FFFF")
        A(len(none_b) > 0,
          f"[反例] 去掉精确过滤后，VID/PID 对不上也照样返回 {len(none_b)} 台"
          " ⇒ C8 抓的正是这个差别（不是「反正都是空」）")
    finally:
        sys.modules.pop("frida_hid_broken", None)
        _restore_winreg(old2)


# ── D. 接线 ─────────────────────────────────────────────────────────────────
def case_wiring() -> None:
    mn = _read("main.py")
    cfg = _read("config.py")
    cs = _read("console_server.py")

    m = re.search(r"frida_hid\.RemoteHidTap\((.*?)\)\n", mn, re.S)
    call = m.group(1) if m else ""
    A("allow_any_hid=" in call and "expect_mac=" in call,
      "D1 main 创建 RemoteHidTap 时传了 allow_any_hid 与 expect_mac")
    A('getattr(cfg, "hid_frida_any_hid", False)' in call,
      "D2 allow_any_hid 读配置且兜底是 **False**")
    A(re.search(r"_, _remote_addr = _addr_from_ble_id\(dev_info\.id\)", mn) is not None,
      "D3 远端 MAC 从 dev_info.id 解析出来")
    A(mn.index("_, _remote_addr = _addr_from_ble_id(dev_info.id)")
      < mn.index("frida_hid.RemoteHidTap("),
      "D4 远端 MAC 在创建 tap **之前**就算好（否则拿到的是 None，等于不校验）")

    A(re.search(r"hid_frida_any_hid:\s*bool\s*=\s*False", cfg) is not None,
      "D5 config.py 里 hid_frida_any_hid 默认 **False**")
    A('"hid_frida_any_hid": bool' in cs,
      "D6 控制台白名单含 hid_frida_any_hid（不进白名单＝改了没反应，静默）")


def case_wiring_negative() -> None:
    cfg = _read("config.py")
    b = cfg.replace("hid_frida_any_hid:  bool  = False",
                    "hid_frida_any_hid:  bool  = True")
    A(re.search(r"hid_frida_any_hid:\s*bool\s*=\s*False", b) is None,
      "[反例] 把配置默认改成 True → D5 判不合格")


# ── E. 停止必须是「可等待的收尾」（审查报告 P2）────────────────────────────
def _idle_tap(fh):
    """真 `RemoteHidTap` 的子类：`run()` 只等停止信号，完全不碰 frida。

    这样能**真起一个线程**再真调 `stop()`，验"它到底等不等线程走完"。
    """

    class _IdleTap(fh.RemoteHidTap):
        def __init__(self):
            super().__init__(lambda *a: None)
            self.ran = False
            self.entered = threading.Event()

        def run(self):                     # noqa: D102
            self.entered.set()
            # 正常实现里 stop() 会置位这个事件 ⇒ 立刻返回
            self._stopped.wait(10.0)
            self.ran = True

    return _IdleTap


def case_stop() -> None:
    import frida_hid as fh

    # E1 不许覆盖 threading.Thread._stop()（老写法 `self._stop = Event()`）
    t0 = _idle_tap(fh)()
    if hasattr(threading.Thread, "_stop"):
        A(callable(getattr(t0, "_stop", None)),
          "E1 `threading.Thread._stop()` 没被实例属性盖掉"
          "（老写法 `self._stop = threading.Event()` 会把它遮成 Event，"
          "线程内部一旦调 `_stop()` 就是 `TypeError: 'Event' object is not callable`）")
    else:
        A(True, "E1 本机 Python 没有 Thread._stop（跳过该断言）")
    A(isinstance(t0._stopped, threading.Event),
      "E1b 停止信号叫 `_stopped`，是独立的 Event")

    # E2 stop() 返回时线程必须已经真的退出
    Tap = _idle_tap(fh)
    t = Tap()
    t.start()
    A(t.entered.wait(2.0), "E2 替身的 run() 真的跑起来了（不是没起线程）")
    t.stop()
    A(not t.is_alive() and t.ran,
      "E2b `stop()` 返回时线程**已经退出**（它 join 过）—— 否则线程可能"
      "正卡在 frida.attach()，拆完又挂上一个没人收的会话")

    # E3 反例：把 stop() 改回「置位 → 直接 teardown」（老写法，不 join）→ 线程还活着
    class _NoJoin(Tap):
        def stop(self):                     # 老实现
            self._stopped.set()
            self._teardown()

    t2 = _NoJoin()
    t2.start()
    t2.entered.wait(2.0)
    t2.stop()
    A(t2.is_alive() or not t2.ran,
      "E3 [反例] 不 join 的老 stop() 返回时线程**还活着** —— "
      "证明 E2b 抓的正是「等线程走完」这件事")
    t2.join(10.0)

    # E4 attach 返回后要再查一次停止标志（迟到的会话得自己拆掉）
    src = _read("frida_hid.py")
    m = re.search(r"self\._session = frida\.attach\(pid\)(.*?)self\._script = "
                  r"self\._session\.create_script", src, re.S)
    seg = m.group(1) if m else ""
    A(bool(seg) and "_stopped.is_set()" in seg,
      "E4 `frida.attach()` 返回后**再查一次**停止标志（join 超时后拆掉的窗口里，"
      "这一行才刚返回 —— 不查就会留下没人收的注入会话）")

    # E5 反例：把那段检查删掉 → E4 判不合格
    b = re.sub(r"self\._session = frida\.attach\(pid\)(.*?)self\._script = "
               r"self\._session\.create_script",
               "self._session = frida.attach(pid)\n                    "
               "self._script = self._session.create_script", src, flags=re.S)
    m2 = re.search(r"self\._session = frida\.attach\(pid\)(.*?)self\._script = "
                   r"self\._session\.create_script", b, re.S)
    A(b != src and "_stopped.is_set()" not in (m2.group(1) if m2 else "x"),
      "E5 [反例] 删掉 attach 后的停止检查 → E4 判不合格")


def main() -> int:
    print("=" * 74)
    print(" 闸：Frida 旁路只认准那一台设备（P1-8）+ 停止是可等待的收尾（P2）")
    print("=" * 74)

    case_js()
    case_js_negative()
    case_py_static()
    case_py_negative()
    case_behavior()
    case_wiring()
    case_wiring_negative()
    case_stop()

    fails = 0
    for ok, msg in _checks:
        print(f"  {'OK  ' if ok else '❌  '} {msg}")
        if not ok:
            fails += 1

    print()
    if fails:
        print(f"FRIDA TARGET FAILED（{fails} 项）")
        print("  提示：这几条坏掉的现象是「按键串台 / 别的蓝牙键鼠被改写」，")
        print("        以及「把脚本注进了一个不相干的进程」—— 全都是静默的。")
        return 1
    print(f"FRIDA TARGET OK（{len(_checks)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
