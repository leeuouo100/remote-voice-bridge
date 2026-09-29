"""控制台鉴权与请求边界的回归闸 —— 2026-09-29 审查报告 P1-1。

背景
====
控制台原先**完全无鉴权**。它绑在 `127.0.0.1` 上，看着"外网进不来"就安全了 ——
但同机的任何进程、以及用户浏览器里打开的任何网页，都能直接打这些接口：

    POST /api/config        改配置（增益、设备、触发键……）
    POST /api/test_hotkey   注入全局快捷键
    POST /api/reconnect     踢掉 BLE 连接
    POST /api/autostart     改开机启动
    POST /api/log/clear     清日志（毁掉唯一的线索）

而 `_body()` 既不要求 `application/json`，也没有体积上限。随机端口只是把
命中概率压低，**它不是鉴权**。

现在的做法（令牌走 HttpOnly + SameSite=Strict Cookie）见 `console_server._TOKEN`
那段注释。这道闸钉住验收标准：

  A. 无令牌 / 错令牌 → 401（GET 与 POST 都要）
  B. 带令牌 → 200（**反证**：不是无差别拒绝，挡的就是"没令牌"这一条）
  C. 错 Origin → 403；错 Host → 403（防跨站 + 防 DNS rebinding）
  D. `text/plain` → 415（简单请求不触发预检，是最省事的跨站写入口）
  E. 超大请求体 → 413
  F. 静态资源**不需要**令牌（否则页面拿不到令牌 = 鸡生蛋）
  G. 令牌本身：足够长、且**不进 URL / 不进日志**
  H. Cookie 属性：HttpOnly + SameSite=Strict

⚠ 沙箱：APPDATA 指向临时目录，不碰用户真实配置。
   用**裸 socket** 发请求 —— urllib 会自作主张补 Host、跟随重定向、把
   非 2xx 变异常，验"精确 Host / 精确状态码"时不够用。

用法： python tools/check_console_auth.py
输出： CONSOLE AUTH OK  /  FAIL（附具体哪几项）
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile

from _utf8 import setup as _setup_utf8

_setup_utf8()

_SANDBOX = tempfile.mkdtemp(prefix="rvb-auth-")
os.environ["APPDATA"] = _SANDBOX
os.environ["RVB_NO_AUTO_CONSOLE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console_server  # noqa: E402

FAILS: list[str] = []
PASSES = 0
PORT = 0
TOKEN = ""


def check(cond, msg) -> bool:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
    return bool(cond)


def raw(method: str, path: str, *, headers: dict | None = None,
        body: bytes = b"") -> tuple[int, bytes]:
    """裸 socket 发一次请求，返回 (状态码, 原始响应)。

    ⚠ 用裸 socket 而不是 urllib：urllib 会自动补 `Host`、把非 2xx 抛成异常、
    可能跟随重定向 —— 验"精确 Host"和"精确状态码"时这些都是干扰。
    """
    hdrs = {"Host": f"127.0.0.1:{PORT}", "Connection": "close"}
    hdrs.update(headers or {})
    lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in hdrs.items()]
    if body or method == "POST":
        lines.append(f"Content-Length: {len(body)}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    try:
        try:
            s.sendall(head + body)
        except (BrokenPipeError, ConnectionResetError):
            # 服务端可能在我们把大 body 发完之前就拒了并关连接 —— 那正是我们要的
            pass
        data = b""
        while True:
            try:
                chunk = s.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            data += chunk
            if len(data) > 4 * 1024 * 1024:
                break
    finally:
        s.close()

    if not data:
        return 0, b""
    try:
        return int(data.split(b" ", 2)[1]), data
    except (IndexError, ValueError):
        return 0, data


def hdr_value(resp: bytes, name: str) -> str:
    for line in resp.split(b"\r\n")[1:]:
        if not line:
            break
        k, _, v = line.partition(b":")
        if k.decode("latin-1").strip().lower() == name.lower():
            return v.decode("latin-1").strip()
    return ""


def token_is_generated(src: str) -> bool:
    """令牌必须是**运行期随机生成**的，不能是源码里写死的常量。

    写死的令牌 = 等于没有令牌（源码是公开的）。
    """
    return "secrets.token_urlsafe(" in src and '_TOKEN: str = ""' in src


def main() -> int:
    global PORT, TOKEN
    srv = console_server.ensure_started(0)
    PORT = srv.port
    TOKEN = console_server.console_token()
    cookie = {"Cookie": f"rvb_token={TOKEN}"}
    json_ok = {"Content-Type": "application/json"}

    # ── A. 无令牌 / 错令牌 → 401 ────────────────────────────────────────────
    for meth, path, extra, body in (
        ("GET", "/api/state", None, b""),
        ("GET", "/api/log", None, b""),
        ("POST", "/api/config", json_ok, b'{"gain":3.0}'),
        ("POST", "/api/reconnect", json_ok, b"{}"),
        ("POST", "/api/test_hotkey", json_ok, b"{}"),
    ):
        code, _ = raw(meth, path, headers=extra, body=body)
        check(code == 401, f"A1 无令牌 {meth} {path} 应 401，实际 {code}")

    for bad in ("", "wrong-token", TOKEN[:-1], TOKEN + "x"):
        h = {"Cookie": f"rvb_token={bad}"}
        code, _ = raw("GET", "/api/state", headers=h)
        check(code == 401, f"A2 错令牌 {bad[:12]!r} 应 401，实际 {code}")

    # ── B. 带令牌 → 200（反证：不是无差别拒绝）────────────────────────────
    code, _ = raw("GET", "/api/state", headers=cookie)
    check(code == 200, f"B1 带令牌 GET /api/state 应 200，实际 {code}（反证）")
    code, _ = raw("POST", "/api/config", headers={**cookie, **json_ok},
                  body=b'{"gain":3.0}')
    check(code == 200, f"B2 带令牌 POST /api/config 应 200，实际 {code}（反证）")
    # 头里给令牌也该认（非浏览器客户端用；浏览器走 Cookie）
    code, _ = raw("GET", "/api/state", headers={"X-RVB-Token": TOKEN})
    check(code == 200, f"B3 X-RVB-Token 头也认，实际 {code}")

    # ── C. 错 Origin / 错 Host → 403 ────────────────────────────────────────
    code, _ = raw("POST", "/api/config",
                  headers={**cookie, **json_ok, "Origin": "http://evil.example"},
                  body=b'{"gain":3.0}')
    check(code == 403, f"C1 跨站 Origin 应 403，实际 {code}")
    # 反证：本机自己的 Origin 必须放行
    code, _ = raw("POST", "/api/config",
                  headers={**cookie, **json_ok,
                           "Origin": f"http://127.0.0.1:{PORT}"},
                  body=b'{"gain":3.0}')
    check(code == 200, f"C2 本机 Origin 应 200，实际 {code}（反证）")

    for bad_host in ("evil.example", f"evil.example:{PORT}", f"127.0.0.1:{PORT + 1}"):
        code, _ = raw("GET", "/api/state",
                      headers={**cookie, "Host": bad_host})
        check(code == 403, f"C3 Host={bad_host!r} 应 403（防 DNS rebinding），实际 {code}")
    code, _ = raw("GET", "/api/state", headers={**cookie, "Host": f"localhost:{PORT}"})
    check(code == 200, f"C4 Host=localhost:{PORT} 应放行，实际 {code}（反证）")

    # ── D. 非 application/json → 415 ────────────────────────────────────────
    code, _ = raw("POST", "/api/config",
                  headers={**cookie, "Content-Type": "text/plain"},
                  body=b'{"gain":3.0}')
    check(code == 415, f"D1 text/plain 应 415，实际 {code}"
                       f"（简单请求不触发预检，是最省事的跨站写入口）")
    code, _ = raw("POST", "/api/config", headers=cookie, body=b'{"gain":3.0}')
    check(code in (415, 411), f"D2 没有 Content-Type 也该拒，实际 {code}")

    # ── E. 超大请求体 → 413 ─────────────────────────────────────────────────
    big = b'{"gain":3.0,"pad":"' + b"A" * (80 * 1024) + b'"}'
    code, _ = raw("POST", "/api/config", headers={**cookie, **json_ok}, body=big)
    check(code == 413, f"E1 80 KiB 请求体应 413，实际 {code}")
    # 反证：小 body 走同一条路必须通
    code, _ = raw("POST", "/api/config", headers={**cookie, **json_ok},
                  body=b'{"gain":3.0}')
    check(code == 200, f"E2 小请求体应 200，实际 {code}（反证）")

    # ── F. 静态资源不需要令牌 ───────────────────────────────────────────────
    code, resp = raw("GET", "/")
    check(code == 200, f"F1 首页不需要令牌（否则拿不到令牌 = 鸡生蛋），实际 {code}")
    code, _ = raw("GET", "/app.js")
    check(code == 200, f"F2 app.js 不需要令牌，实际 {code}")

    # ── G. 令牌本身 ─────────────────────────────────────────────────────────
    check(len(TOKEN) >= 40,
          f"G1 令牌足够长（token_urlsafe(32) 应 ≥ 40 字符，实际 {len(TOKEN)}）")
    check(TOKEN not in srv.url(),
          f"G2 令牌**不在**控制台 URL 里（URL 会进 bridge.log，写进去=泄漏）")
    src = open(console_server.__file__, encoding="utf-8").read()
    check(token_is_generated(src),
          "G3 令牌是**运行期随机生成**的，不是源码里写死的常量")
    # G0 反例：写死的令牌必须被判不合格
    check(not token_is_generated('_TOKEN = "hardcoded-abc"\n'),
          "G0 反例：写死的令牌 → 判不合格（说明 G3 测得出来）")

    # ── H. Cookie 属性 ──────────────────────────────────────────────────────
    _, resp = raw("GET", "/")
    sc = hdr_value(resp, "Set-Cookie")
    check(f"rvb_token={TOKEN}" in sc, f"H1 首页下发的是本进程的令牌（实际 {sc!r}）")
    check("HttpOnly" in sc, f"H2 Cookie 必须 HttpOnly（脚本/XSS 读不到），实际 {sc!r}")
    check("SameSite=Strict" in sc,
          f"H3 Cookie 必须 SameSite=Strict（跨站带不上），实际 {sc!r}")
    check(hdr_value(resp, "X-Content-Type-Options") == "nosniff",
          "H4 有 nosniff（防 MIME 嗅探导致的 XSS）")

    srv.stop()
    shutil.rmtree(_SANDBOX, ignore_errors=True)

    for m in FAILS:
        print(f"  FAIL {m}")
    if FAILS:
        print(f"CONSOLE AUTH FAILED（{len(FAILS)} 项）")
        print("  提示：绑 127.0.0.1 只挡住外网，挡不住同机进程和浏览器里的网页。")
        print("        随机端口是概率，不是鉴权。")
        return 1
    print(f"  OK   {PASSES} 项全过（401/403/415/413 + 反证 + 令牌与 Cookie 属性）")
    print("CONSOLE AUTH OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
