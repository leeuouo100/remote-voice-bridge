"""配对判定链的回归闸 —— 用假数据把 pairing.classify 的各条分支都钉住。

为什么需要：`pairing.py` 治的是「换 USB 口之后程序**完全连不上**」，
而它的判定逻辑读的是真机注册表 —— CI 里没有蓝牙棒，那段代码
**第一次运行就是在用户机器上**。这个项目在这一点上栽过好几次
（v1.0.5 的诊断工具、v1.0.9 的静默异常），所以关键判定一律抽成纯函数 + 假数据锁死。

覆盖：
  · 地址格式转换（hex12 / pretty 往返，输入带不带冒号都要吃）
  · 六种真实状态：OK / STALE_ADDR（真机那种）/ NODE_PHANTOM（新形态）/
    RECORD_OK_NO_NODE / RECORD_NO_ADDRESS / NO_RECORD
  · **行为级反例**：把 classify 抠出来改坏（去掉 STALE_ADDR 分支 / 把「幽灵」
    的口径收窄回"只看活地址"）→ exec 起来必须复现出错误的判定
  · **源码级断言**：搬家保留值类型、提权走 runas、有回滚入口、默认阶梯不碰地址、
    「删不动」的结论只在管理员下给、ACL 接管开了一组特权且循环到不动点、
    提权时把全部开关带给子进程（漏一个就少做一步）

用法： python tools/check_pairing.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _utf8 import setup as _setup_utf8  # noqa: E402  （下面是中文输出，先钉住编码）

_setup_utf8()

import pairing  # noqa: E402

# ── 真机数据（2026-09-15 武哥这台机器上抄下来的）────────────────────────────
OLD = "047f0e901101"          # 配对记录绑的地址（蓝牙棒在旧 USB 口时的地址）
LIVE = "047f0ef2d294"         # 现在生效的地址（换到 Port2 之后）
REMOTE = "f196a263671c"


def _rec(addr: str, remote: str = REMOTE) -> dict:
    return {"remote": remote, "name": "Chromecast Remote",
            "services_for": ([{"addr": addr, "value_count": 6}] if addr else [])}


def _rec2(addrs: list[str], remote: str = REMOTE) -> dict:
    """一条记录里绑了**多个**本地地址（migrate 跑过之后的形态）。"""
    return {"remote": remote, "name": "Chromecast Remote",
            "services_for": [{"addr": a, "value_count": 6} for a in addrs]}


def _load_classify(src: str):
    """把 pairing.py 里的 classify 抠出来单独 exec —— 好把它"改坏"再验行为。

    为什么要真 exec 而不是只 grep 源码：这条闸守的是**判定顺序**（STALE_ADDR
    必须排在 NODE_PHANTOM 前面）。顺序对不对是一种行为，不是一串字符，
    只查字符串是查不出来的。
    """
    import re as _re
    m = _re.search(r"^def classify\(", src, _re.M)
    assert m, "pairing.py 里找不到 classify"
    rest = src[m.start():]
    nxt = _re.search(r"^def ", rest[1:], _re.M)
    body = rest[: nxt.start() + 1] if nxt else rest
    ns: dict = {}
    exec(compile(body, "<classify>", "exec"), ns)  # noqa: S102
    return ns["classify"]


def _node(local: str, remote: str = REMOTE, key: str = "") -> dict:
    return {"key": key or f"Enum\\BTHLE\\Dev_{remote}\\8&1&0&{remote}",
            "devkey": f"Dev_{remote}", "instance": f"8&1&0&{remote}",
            "unique_id": f"Dev_{local}_{remote}", "local": local,
            "remote": remote, "friendly": "Chromecast Remote"}


def main() -> int:
    checks: list[tuple[bool, str]] = []

    def A(ok: bool, msg: str) -> None:
        checks.append((bool(ok), msg))

    # ── 地址格式 ──
    A(pairing.hex12("04:7F:0E:F2:D2:94") == LIVE, "hex12 吃带冒号的大写写法")
    A(pairing.hex12(LIVE) == LIVE, "hex12 幂等")
    A(pairing.pretty(LIVE) == "04:7F:0E:F2:D2:94", "pretty 输出大写冒号格式")
    A(pairing.pretty(pairing.hex12("04:7f:0e:f2:d2:94")) == "04:7F:0E:F2:D2:94",
      "两种写法往返一致")
    A(pairing.hex12(None) == "" and pairing.pretty("") == "", "空值不炸")

    # ── 真机那种：记录绑旧地址，AEP 节点也是旧地址 → STALE_ADDR ──
    t = pairing.classify(LIVE, [_rec(OLD)], [_node(OLD), _node(OLD, key="k2")])
    A(len(t) == 1 and t[0]["status"] == "STALE_ADDR",
      "★ 真机形态 → STALE_ADDR（这是唯一能把 v1.0.9 事故认出来的分支）")
    A(t[0]["record_addr"] == OLD, "把记录绑的地址原样带出来（restore 要用）")
    A(len(t[0]["stale_nodes"]) == 2 and not t[0]["live_nodes"],
      "两个过期关联节点都点名了")
    A(pairing.overall_status([_rec(OLD)], t) == "STALE_ADDR", "总状态跟随")

    # ── 修好之后：地址对上、节点也对上 → OK ──
    t = pairing.classify(OLD, [_rec(OLD)], [_node(OLD)])
    A(t[0]["status"] == "OK", "地址一致 + 节点一致 → OK")
    A(pairing.overall_status([_rec(OLD)], t) == "OK", "OK 时总状态也是 OK")

    # ── 地址对、但关联节点丢了 → RECORD_OK_NO_NODE（重启蓝牙服务就能重建）──
    t = pairing.classify(OLD, [_rec(OLD)], [])
    A(t[0]["status"] == "RECORD_OK_NO_NODE", "有记录没节点 → RECORD_OK_NO_NODE")

    # ── 地址对、但关联节点是"幽灵"（父适配器实例已不存在）──
    # 2026-09-15 实测：这种情况诊断一度判成 OK，而设备照样 E_INVALIDARG。
    # 地址**对**不等于能用 —— 这是整件事里最容易自欺欺人的一格。
    t = pairing.classify(LIVE, [_rec(LIVE)], [{**_node(LIVE), "present": False}])
    A(t[0]["status"] == "NODE_PHANTOM",
      "★ 地址对但节点是幽灵 → NODE_PHANTOM（不能因为地址对就判 OK）")

    # ── ★ 2026-09-15 真机挖出的第 6 种形态：migrate 之后 ──
    # 记录已经补成「两个地址都绑」，但关联节点还挂在**旧地址**上、并且是幽灵。
    # 旧口径只看"活地址上的幽灵"→ 这里两个列表都是空的 → 掉进 UNKNOWN
    # （报告里显示"看不出来"），把"节点要重建"这个清楚结论说糊了。
    t = pairing.classify(LIVE, [_rec2([OLD, LIVE])],
                         [{**_node(OLD), "present": False},
                          {**_node(OLD, key="k2"), "present": False}])
    A(t[0]["status"] == "NODE_PHANTOM",
      "★ migrate 后的形态（记录绑两个地址、节点还是旧的幽灵）→ NODE_PHANTOM"
      "（不是『看不出来』）")
    A(len(t[0]["phantom_nodes"]) == 2 and len(t[0]["stale_nodes"]) == 2,
      "两个节点既算过期也算幽灵（报告里要说明重叠，别让用户以为有 4 个）")

    # ── 记录里没有 ServicesFor（被清过一半）──
    t = pairing.classify(LIVE, [_rec("")], [])
    A(t[0]["status"] == "RECORD_NO_ADDRESS", "没有 ServicesFor → RECORD_NO_ADDRESS")

    # ── 一条记录都没有 ──
    A(pairing.overall_status([], []) == "NO_RECORD", "空记录 → NO_RECORD")

    # ── 多设备里只要有一个 stale，总状态就报 stale（不能漏修）──
    t = pairing.classify(LIVE, [_rec(LIVE, "aa" * 6), _rec(OLD, "bb" * 6)],
                         [_node(LIVE, "aa" * 6), _node(OLD, "bb" * 6)])
    A(pairing.overall_status([1, 2], t) == "STALE_ADDR", "多设备取最严重的那条")

    # ── PnP 实例 ID 必须带 USB\ 枚举器前缀 ──
    # 少这个前缀 → CM_Locate_DevNodeW 对所有设备都返回 CR_NO_SUCH_DEVINST(13)
    # → **在用的那颗蓝牙棒被标成历史残留**（不报错，只给反结论，最难发现）。
    # 真机上验一次：拿到适配器列表时逐个检查前缀。
    ads = pairing.usb_bt_adapters()
    if ads:
        A(all(a["instance_id"].upper().startswith("USB\\") for a in ads),
          f"适配器 instance_id 带 USB\\ 前缀（真机抽查 {len(ads)} 个）")
        A(all(not a["instance_id"].count("\\USB\\\\") for a in ads),
          "注册表路径与 PnP 实例 ID 没有互相污染")
    else:
        print("  （本机没枚举到蓝牙适配器，跳过实例 ID 真机抽查）")

    # ── 源码级反例：别把"搬家时保留类型"这件事悄悄退化掉 ──
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "pairing.py"), encoding="utf-8").read()
    migrate = src.split("def fix_migrate")[1].split("def fix_purge")[0]
    A("_values_typed(" in migrate,
      "migrate 用 _values_typed 保留值类型（REG_QWORD 照 DWORD 写会静默截断）")
    A("REG_DWORD," not in migrate,
      "migrate 里没有把类型写死成 REG_DWORD")
    A("Bluetooth_UniqueID" in migrate,
      "migrate 同步了 AEP 节点的 Bluetooth_UniqueID（改它才真的换地址）")
    A("_copy_key_tree(" in migrate,
      "migrate 也搬了链接密钥（只搬 ServicesFor 属于半搬，握手照样失败）")

    # 提权与备份这两件事是"改坏了要能救回来"的保证，缺一不可
    A('"reg", "export"' in src, "备份走 reg export（真能落成 .reg 文件）")
    A("ShellExecuteExW" in src and '"runas"' in src, "提权走 ShellExecuteExW runas")

    # 默认阶梯必须**不碰地址、不需重新配对**：
    # restore 实测改不动（驱动会把 DeviceAddressCache 写回去），所以它不在默认里；
    # 真正干活的是 migrate（补记录）+ rebuild（重建幽灵关联节点）。
    A("def fix_rebuild" in src, "有 rebuild 修法（只改 UniqueID 实测不够）")
    A('["migrate", "rebuild"]' in src,
      "默认阶梯 = migrate → rebuild（都不需要重新配对）")
    A("_del_tree(" in src.split("def fix_rebuild")[1].split("def fix_purge")[0],
      "rebuild 真的删掉了关联节点树（不删就重建不了）")
    A("read_aep_nodes()" in src.split("def fix_rebuild")[1].split("def fix_purge")[0],
      "rebuild 会轮询等节点重建（不是删完就宣布成功）")

    # 「半删」是 2026-09-15 真机踩到的新坑：`Properties` 的 ACL 只放 SYSTEM，
    # 管理员也删不掉，于是删除动作只删掉一半，而调用方还当成功。
    rebuild_src = src.split("def fix_rebuild")[1].split("def fix_purge")[0]
    purge_src = src.split("def fix_purge")[1].split("def _del_tree")[0]
    A("_undeletable_in_tree(" in rebuild_src,
      "rebuild 删之前先探能不能删干净（避免半删）")
    A("_undeletable_in_tree(" in purge_src, "purge 同样先探再删")
    A("present" in rebuild_src.split("fresh =")[-1],
      "rebuild 的成功判据含 present（不是『有节点就算成功』）")
    A("failed" in src.split("def _del_tree")[1].split("def _undeletable_in_tree")[0],
      "_del_tree 会把删不掉的键记账（不再吞掉失败）")

    # ── 「删不动」这个结论必须在**管理员**下才敢下（2026-09-15 自测挖出）────
    # 非管理员跑探针时，普通用户连 Enum\BTHLE 整棵树都写不动 → 探出来必然
    # "全都删不动"，而报告把它归因成「ACL 只放 SYSTEM，管理员也删不掉」，
    # 等于把用户直接劝退到"没救"。结论错比不给结论更糟。
    probe = src.split("def _undeletable_in_tree")[1].split("def _need_admin_hint")[0]
    A("-> tuple[list[str], bool]" in probe,
      "探针返回 (删不动的键, 结论是否有效) —— 把「判不了」和「删不动」分开")
    A("if not is_admin():" in probe and "return [], False" in probe,
      "★ 非管理员时拒绝下结论（不然会把权限问题说成 ACL 问题）")
    A("DELETE_ACCESS" in probe,
      "★ 用 DELETE 权限探（删键要的就是它；KEY_SET_VALUE 会漏判「能写不能删」）")
    A("DELETE_ACCESS = 0x00010000" in src,
      "DELETE 权限位有定义（winreg 没导出这个常量，KEY_WRITE 里也不含它）")
    A(rebuild_src.count("judged") >= 2 and "if not judged:" in rebuild_src,
      "rebuild 认 verdict：判不了就不动手（半删比不删更难收拾）")
    A(purge_src.count("judged") >= 2 and "if not judged:" in purge_src,
      "purge 认 verdict：判不了就只清记录，不拿半删去赌")
    A("_need_admin_hint" in rebuild_src,
      "判不了时告诉用户怎么提权（而不是让他以为没救）")
    A("已在管理员下确认" in src,
      "_acl_hint 的结论标明「已在管理员下确认」，不再让人以为是权限问题")

    # ── ACL 接管：把"删不动"变成"删得动"（2026-09-15 真机验证通过）──────────
    # 这台机器上 `Enum\BTHLE\Dev_<远端>\<实例>\Properties` 连 READ_CONTROL
    # 都 rc=5，普通手段删不掉 —— 这也是 Windows 设置里「删除设备」卡死的根。
    A("def take_ownership" in src, "有 ACL 接管（拿所有权 → 改 DACL → 再删）")
    own_src = src.split("def take_ownership")[1].split("def take_ownership_tree")[0]
    A("OWNER_SECURITY_INFORMATION" in own_src and "SetSecurityInfo" in own_src,
      "接管走 SetSecurityInfo 改所有者")
    A("DACL_SECURITY_INFORMATION" in own_src,
      "接管还把 DACL 也给上（只拿所有权不改 DACL 仍然删不动）")

    # ⚠️ 这条是实测教训：只开 SeTakeOwnershipPrivilege → RegOpenKeyExW(WRITE_OWNER)
    # 照样 rc=5。绕开 DACL 的正主是 SeRestorePrivilege。少一个都不行。
    A("SeTakeOwnershipPrivilege" in src and "SeRestorePrivilege" in src,
      "★ 特权开了一组（只用 SeTakeOwnershipPrivilege 实测仍被拒）")
    A("SeBackupPrivilege" in src, "也开了 SeBackupPrivilege（越权读取那一半）")

    # ⚠️ 第二条实测教训：不声明 restype，GetCurrentProcess 的伪句柄 -1 会被
    # ctypes 截成 32 位 → OpenProcessToken 报"句柄无效"，而错误信息完全看不出根因。
    en = src.split("def _enable_privilege")[1].split("def _admin_sid")[0]
    A("GetCurrentProcess.restype" in en,
      "★ 声明了 GetCurrentProcess.restype（不声明伪句柄 -1 会被截断）")
    A("OpenProcessToken.argtypes" in en, "OpenProcessToken 也声明了 argtypes")
    A("get_last_error()" in en,
      "把 WinError 带进错误信息（不然只知道「失败」，不知道为什么）")

    # ⚠️ 第三条实测教训：Properties 底下的 {GUID} 子键各有独立且**拒绝继承**的
    # DACL，父键的继承 ACE 传不下去 → 一轮接管只解一层。
    tt = src.split("def take_ownership_tree")[1].split("def _undeletable_in_tree")[0]
    A("for i in range(rounds)" in tt,
      "★ 接管循环到不动点（一轮只解一层：Properties → {GUID} → …）")
    A("_undeletable_in_tree(" in tt, "每轮都重新探，而不是「接管完就当成功」")

    # 显式开关，不进默认阶梯 —— 它会改系统键的 ACL，不能悄悄做
    A("--take-ownership" in src, "接管是显式开关（--take-ownership）")
    A('has("--take-ownership")' in src,
      "默认阶梯不会自动接管（改系统键 ACL 这种事不能悄悄做）")

    # 诊断工具：rc=5 只知道"被拒"，不知道"被谁拒"，得能看到 ACE
    A("def dump_acl" in src and "def _sid_str" in src,
      "有 --acl-dump：把所有者 + 每条 ACE 打成人话")
    A("ConvertSidToStringSidW" in src, "SID 转成 S-1-5-32-544 这种可读串")
    A('"--acl-dump"' in src and "--acl-probe" in src,
      "两个排查模式都对外开了（--acl-probe 只探不删）")
    A("def _keypath" in src,
      "_keypath 把「路径 ← 原因」拆回干净路径（报告要好看，操作要干净）")

    # ── 密钥树：决定"等重建"还是"必须重配"（2026-09-15 真机）───────────────
    # `Parameters\Keys` 与 `Properties` 同病：提权后连 READ_CONTROL 都 rc=5。
    # 所以"读不到"是有歧义的 —— 真的空 vs 被 ACL 拒，结论**完全相反**。
    # 必须把权限探测 + ACE 一起打出来，别让调用方去猜。
    keys_src = src.split('has("--keys")')[1].split('has("--acl-probe")')[0] \
        if 'has("--keys")' in src else ""
    A(bool(keys_src), "有 --keys（提权只读，列链路密钥树）")
    A("dump_acl(" in keys_src,
      "★ 「读不到密钥树」时必须同时给权限探测 + ACL —— 「空」和「被拒」结论相反")
    A("purge" in keys_src and "take-ownership" in keys_src,
      "结论里直接给出「必须重新配对」那条命令（不是让用户自己拼）")
    A("Keys" in src and "BTHPORT_KEYS" in src, "密钥树路径有常量")

    # ── 提权必须把**所有**开关带过去 ─────────────────────────────────────────
    # 2026-09-15 实测：只传 "--acl-probe"，`--take-ownership` 在提权那一刻丢掉，
    # 子进程静悄悄少做一步 —— 而症状和"接管不生效"长得一模一样。
    A('child = [a for a in argv if a != "--dry-run"] + ["--elevated"]' in src,
      "★ 提权时把所有开关原样带给子进程（漏一个就少做一步，症状还一样）")
    A(src.count('child = [a for a in argv if a != "--dry-run"]') >= 3,
      "acl-probe / acl-dump / keys 三条路都这么传（不是只改了一两条）")

    # ── 判定文案：准确 > 笼统 ────────────────────────────────────────────────
    diag2 = src.split("def diagnose")[1].split("def STATUS_TEXT")[0]
    A('"RECORD_OK_NO_NODE"' not in diag2.split("if d[")[-1].split(":")[0],
      "RECORD_OK_NO_NODE 不被降级成 UNRESOLVABLE（节点清空后打不开是必然的）")
    rep = src.split("def format_report")[1].split("def backup")[0]
    A("设备 ID 丢了" in rep,
      "★ local 读不到时标签说「设备 ID 丢了」，不是「地址过期」（查错方向）")

    # 有备份就必须有回滚入口 —— 只会"导得出"不会"导得回"的工具不该发出去
    A("def restore_backup" in src and '"reg", "import"' in src,
      "有 --restore-backup（把备份 .reg 导回去）")

    # 诊断必须"真的打一次"才敢说 OK
    diag_src = src.split("def diagnose")[1].split("def format_report")[0]
    A("verify_open(" in diag_src, "诊断带终审：真的 from_id_async 打开一次")
    A("UNRESOLVABLE" in diag_src, "打不开时状态降级成 UNRESOLVABLE（不再报 OK）")
    A("present" in src.split("def read_aep_nodes")[1].split("def read_key_material")[0],
      "关联节点会判『幽灵』（devnode 状态），不只是看 Bluetooth_UniqueID 里的地址")

    # ── 行为级反例 1：去掉 STALE_ADDR 分支 → 真机形态必须判错 ───────────────
    # 这条钉的是"分支顺序"：节点也是过期幽灵，所以一旦少了 STALE_ADDR 这一格，
    # 真机形态就会掉进 NODE_PHANTOM —— 症状对得上，但根因（记录作废）被盖掉，
    # 于是工具会去"重建节点"而不是"搬记录"，用户白忙一场。
    cls_src = src.split("def classify(")[1].split("\ndef overall_status")[0]
    cls_src = "def classify(" + cls_src
    # 注意锚点要连**前导缩进和换行**一起吃 —— 只吃 "elif …" 的话，那行前面的
    # 8 个空格会留下来，跟下一行的 8 个空格叠成 16 → IndentationError。
    # （这个坑自己踩过一次：反例"应当还能跑起来"，结果它是语法错，不是判定错。）
    mut_src = re.sub(r'\n\s*elif bound and addr and addr not in bound:.*?status = "STALE_ADDR"',
                     "", cls_src, flags=re.S)
    A(mut_src != cls_src, "[反例] 能改坏 classify（锚点没漂）")
    try:
        mut_cls = _load_classify(mut_src)
        mut_t = mut_cls(LIVE, [_rec(OLD)], [_node(OLD), _node(OLD, key="k2")])
        A(mut_t[0]["status"] != "STALE_ADDR",
          "[反例] 去掉 STALE_ADDR 分支后，真机形态确实判错了（说明这项检查是有效的）")
    except AssertionError:
        raise
    except Exception as e:                      # noqa: BLE001
        A(False, f"[反例] 改坏后的 classify 应当还能跑起来，实际炸了：{e!r}")

    # ── 行为级反例 2：把「幽灵」口径收窄回"只看活地址" → 新形态必须判不出 ──
    s_cls = cls_src.replace(
        'ghost = [n for n in mine if not n.get("present", True)]',
        'ghost = [n for n in mine if n["local"] == addr and not n.get("present", True)]')
    A(s_cls != cls_src, "[反例] 能改坏 classify 的幽灵口径（锚点没漂）")
    s_cls = s_cls.replace(
        "elif bound and mine and not live_nodes:",
        "elif ghost and n is None:")            # 让新分支失效，模拟旧写法
    try:
        s_cls_f = _load_classify(s_cls)
        s_t = s_cls_f(LIVE, [_rec2([OLD, LIVE])],
                      [{**_node(OLD), "present": False}])
        A(s_t[0]["status"] == "UNKNOWN",
          "[反例] 幽灵口径收窄回旧写法后，新形态掉回 UNKNOWN"
          "（这正是真机上显示『看不出来』的那次）")
    except AssertionError:
        raise
    except Exception as e:                      # noqa: BLE001
        A(False, f"[反例] 收窄口径后的 classify 应当还能跑，实际炸了：{e!r}")

    bad = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {msg}")
    if bad:
        print(f"PAIRING CHECK FAILED（{len(bad)} 项）")
        return 1
    print("PAIRING OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
