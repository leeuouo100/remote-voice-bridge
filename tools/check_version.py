"""
版本号一致性校验 —— config.py 的 APP_VERSION 必须和 installer.iss 的默认值一致。

为什么要有这个脚本：版本号散在两个文件里，改一个忘一个，结果就是
"代码是 1.0.2、装出来的安装包标题还写着 1.0.1" —— 而这种错误只有在
别人装完点开「设置 → 关于」才会发现，那时候 tag 已经推出去了，只能重新发版。
CI 也会跑同样的一致性校验（见 .github/workflows/build-installer.yml）。

用法： python tools/check_version.py
输出： OK 1.0.2   /   不一致时非零退出
"""

from __future__ import annotations

import os
import re
import sys

from _utf8 import setup as _setup_utf8  # 下面是中文输出，先钉住编码（CI 是 cp1252）

_setup_utf8()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_config_version() -> str:
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        raise SystemExit("config.py 里找不到 APP_VERSION")
    return m.group(1)


def read_iss_version() -> str:
    src = open(os.path.join(ROOT, "installer.iss"), encoding="utf-8").read()
    # 只认 #ifndef 后面的兜底默认值那一行
    m = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', src)
    if not m:
        raise SystemExit("installer.iss 里找不到 MyAppVersion")
    return m.group(1)


def main() -> int:
    cfg = read_config_version()
    iss = read_iss_version()
    if cfg != iss:
        print(f"FAIL 版本号不一致：config.py APP_VERSION={cfg}，"
              f"installer.iss MyAppVersion={iss}")
        return 1
    print(f"OK {cfg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
