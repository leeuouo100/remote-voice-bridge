"""控制台前端的「不拼 HTML」静态闸 —— 2026-09-29 审查报告 P1-2。

背景
====
`ui/app.js` 原来把状态清单的 `name`/`value` **拼进 innerHTML**：

    $('#status-list').innerHTML = rows;     // rows 里有 ${r.value}

而 `r.value` 的来源包括 `system_mic_device` 这类**能由 /api/config 写入**的
字段 —— 往配置里塞一段 `<img src=x onerror=...>` 就是存储型 XSS。
拼字符串这条路只要**有一处忘了转义**就中招，而且不会有任何报错。

所以现在的规矩是：**自由文本一律走 textContent，整个前端不再拼 innerHTML**。
唯一例外是 `iconFor()` —— 它要建 SVG 命名空间下的节点，而那段字符串
100% 是本文件里的常量（`BTN_ICONS` 表 + 写死的 path），没有任何外部数据。

这道闸钉住那条规矩（每条都配反例）：

  A. `ui/app.js` 里 `innerHTML` 只出现在 `iconFor` 那一处，且赋的是常量函数
  B. `ui/index.html` 里没有内联 `style=`、没有内联事件 `on*=`、
     没有内联 `<script>`（只有 `<script src=`）
  C. 服务端真的发了 CSP，且没有 `unsafe-inline` / `unsafe-eval`，
     关键指令（default-src 'none' / object-src / base-uri / frame-ancestors）齐全
  D. `ui/app.js` 能通过 `node --check`（改完 JS 至少别有语法错）

⚠ 这道闸只证明"没有拼字符串"。**行为**层面的证据由 `tools/check_ui_xss.js`
   提供（真开浏览器，把 payload 塞进数据里看脚本会不会执行）。

用法： python tools/check_ui_xss.py
输出： UI XSS STATIC OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from _utf8 import setup as _setup_utf8

_setup_utf8()

REPO = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
PASSES = 0

# 唯一允许出现 innerHTML 的那一行（常量图标）
ALLOWED_INNERHTML = "tpl.innerHTML = iconSvg(id)"


def check(cond, msg) -> bool:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
    return bool(cond)


# ── 可被反例复用的检查器 ─────────────────────────────────────────────────────

def strip_js_comments(src: str) -> str:
    """剥掉 JS 的注释（保留字符串字面量里的内容）。

    ⚠ 为什么不能简单 `re.sub(r'//.*', '', src)`：字符串里的 `http://` 会被
      当成注释切掉，后面的内容就跟着错位 —— 检查器会开始"看错地方"。
      所以老老实实走一遍状态机（单引号 / 双引号 / 模板串 / 行注释 / 块注释）。
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote = ""          # 当前所在的字符串定界符
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\":                       # 转义：连下一个字符一起吃掉
                if i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
            elif c == quote:
                quote = ""
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def innerhtml_sites(src: str) -> list[str]:
    """返回源码里所有用到 innerHTML 的行（已剥注释）。"""
    return [ln.strip() for ln in strip_js_comments(src).splitlines()
            if "innerHTML" in ln]


def html_has_inline_script_or_style(src: str) -> list[str]:
    """返回 HTML 里的违规项：内联 style 属性 / 内联事件 / 内联 <script>。"""
    body = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    bad = []
    if re.search(r"\sstyle\s*=", body):
        bad.append("内联 style= 属性")
    for m in re.finditer(r"<[a-zA-Z][^>]*?\son[a-z]+\s*=", body):
        bad.append(f"内联事件处理器 {m.group(0)[:40]!r}")
    for m in re.finditer(r"<script\b[^>]*>", body, flags=re.I):
        if "src=" not in m.group(0).lower():
            bad.append("内联 <script> 块")
    return bad


def csp_problems(csp: str) -> list[str]:
    """检查 CSP 字符串是否够严。返回问题列表（空 = 合格）。"""
    bad = []
    if "unsafe-inline" in csp:
        bad.append("含 'unsafe-inline'（内联脚本/样式又能跑了）")
    if "unsafe-eval" in csp:
        bad.append("含 'unsafe-eval'")
    for need in ("default-src 'none'", "object-src 'none'",
                 "base-uri 'none'", "frame-ancestors 'none'",
                 "script-src 'self'"):
        if need not in csp:
            bad.append(f"缺 {need}")
    return bad


def main() -> int:
    app_js = (REPO / "ui" / "app.js").read_text(encoding="utf-8")
    index_html = (REPO / "ui" / "index.html").read_text(encoding="utf-8")
    console_src = (REPO / "console_server.py").read_text(encoding="utf-8")

    # ── A. app.js：innerHTML 只许出现在那一处 ───────────────────────────────
    sites = innerhtml_sites(app_js)
    check(len(sites) == 1,
          f"A1 app.js 里只有 1 处 innerHTML（实际 {len(sites)} 处）：{sites[:4]}")
    check(len(sites) == 1 and ALLOWED_INNERHTML in sites[0],
          f"A2 那一处必须是常量图标 {ALLOWED_INNERHTML!r}（实际 {sites[:2]}）")
    # 反例：老写法（把数据拼进 innerHTML）必须被这条规则抓出来
    old = "$('#status-list').innerHTML = state.checklist.map(r => r.value).join('');\n"
    check(len(innerhtml_sites(old)) == 1,
          "A0a 反例：老写法被数出来 1 处 → 说明 A1 测得出来")
    check(ALLOWED_INNERHTML not in (innerhtml_sites(old) or [""])[0],
          "A0b 反例：老写法不满足 A2 的白名单")
    # 反例：注释里的 innerHTML 不该被算进去（否则 A1 会假红）
    check(innerhtml_sites("// x.innerHTML = 'a'\n/* y.innerHTML = 'b' */\n") == [],
          "A0c 反例：注释里的 innerHTML 不算数（否则 A1 永远假红）")
    # 反例：字符串里的 // 不能被当成注释
    check("http://a" in strip_js_comments("const u = 'http://a';"),
          "A0d 反例：字符串里的 // 不被误当注释（否则检查器会看错地方）")

    # 渲染器本身不许再用 innerHTML（这两处就是 P1-2 的原始入口）
    # ⚠ 必须在**剥掉注释**的源码上查：文件里有一条注释专门在解释
    #   "以前用 innerHTML、现在不拼了"，直接查会把它当成真用法 → 永远假红。
    js_code = strip_js_comments(app_js)
    for fn in ("renderStatusList", "renderDevices", "renderMapping"):
        m = re.search(rf"function {fn}\(\)\s*\{{(.*?)\n\}}", js_code, flags=re.S)
        body = m.group(1) if m else ""
        check(bool(m), f"A3 找得到 {fn}()")
        check("innerHTML" not in body, f"A4 {fn}() 里不再有 innerHTML")
        check("textContent" in body or "replaceChildren" in body or "new Option" in body,
              f"A5 {fn}() 用 DOM 节点/textContent 落地（不是靠转义函数）")
    # A0e 反例：注释里的 innerHTML 不该让 A4 变红
    check("innerHTML" not in strip_js_comments(
              "function renderStatusList() {\n  // 以前用 innerHTML\n  const a = 1;\n}"),
          "A0e 反例：注释里的 innerHTML 不参与 A4 判定（否则永远假红）")

    # ── B. index.html：没有内联脚本/样式/事件 ───────────────────────────────
    bad = html_has_inline_script_or_style(index_html)
    check(not bad, f"B1 index.html 没有内联脚本/样式/事件（实际 {bad}）")
    check("<script src=" in index_html, "B2 脚本仍然只有外链 app.js")
    # 反例
    check(html_has_inline_script_or_style('<div style="width:7em"></div>'),
          "B0a 反例：内联 style= 被抓出来")
    check(html_has_inline_script_or_style('<button onclick="x()">a</button>'),
          "B0b 反例：内联事件被抓出来")
    check(html_has_inline_script_or_style("<script>alert(1)</script>"),
          "B0c 反例：内联 <script> 被抓出来")
    check(not html_has_inline_script_or_style('<script src="app.js"></script>'),
          "B0d 对照：外链 <script src> 判合格")

    # ── C. 服务端真的发 CSP ─────────────────────────────────────────────────
    m = re.search(r'_CSP\s*=\s*\((.*?)\)\n', console_src, flags=re.S)
    csp_src = m.group(1) if m else ""
    csp = " ".join(re.findall(r'"([^"]*)"', csp_src)).strip()
    check(bool(csp), "C1 console_server.py 里定义了 _CSP")
    problems = csp_problems(csp)
    check(not problems, f"C2 CSP 够严（实际问题：{problems}）")
    check('self.send_header("Content-Security-Policy", _CSP)' in console_src,
          "C3 _CSP 真的发给了浏览器（定义了不发等于没设）")
    # 反例
    check(csp_problems("default-src 'none'; script-src 'self' 'unsafe-inline'; "
                       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
          "C0a 反例：带 unsafe-inline 的 CSP 被判不合格")
    check(csp_problems("default-src *"),
          "C0b 反例：宽得没边的 CSP 被判不合格")
    check(not csp_problems(csp), "C0c 对照：真 CSP 判合格（与反例同一个判定函数）")

    # ── D. JS 语法 ─────────────────────────────────────────────────────────
    node = shutil.which("node")
    if not node:
        for cand in (Path(r"C:\Users\leeway\.workbuddy-ai\binaries\node\versions")
                     .glob("*/node.exe")):
            node = str(cand)
            break
    if node:
        r = subprocess.run([node, "--check", str(REPO / "ui" / "app.js")],
                           capture_output=True, text=True, errors="replace")
        check(r.returncode == 0,
              f"D1 ui/app.js 通过 node --check（实际：{r.stderr.strip()[:200]}）")
    else:
        print("  INFO 没找到 node，跳过 D1（语法检查）—— CI 上会跑")

    for m_ in FAILS:
        print(f"  FAIL {m_}")
    if FAILS:
        print(f"UI XSS STATIC FAILED（{len(FAILS)} 项）")
        print("  提示：自由文本进 DOM 只能走 textContent。拼 innerHTML 的路只要")
        print("        有一处忘了转义就中招，而且**不会有任何报错**。")
        return 1
    print(f"  OK   {PASSES} 项全过（不拼 innerHTML / 无内联脚本样式 / CSP / 语法）")
    print("UI XSS STATIC OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
