"""把标准输出/标准错误钉成 UTF-8 —— 让自检脚本在 cp1252 控制台也不会炸。

为什么需要这个文件
------------------
这些脚本会用中文输出（例如 check_keymap.py 的 "OK 32 个目标动作 / ..."）。
Windows 上的 Python 若没有显式指定编码，stdout 会跟随系统代码页：

  · 英文 Windows → cp1252 / cp437
  · GitHub Actions 的 windows-latest → 也是 cp1252（实测）

于是 `print("…个目标动作…")` 在 CI 上直接抛

  UnicodeEncodeError: 'charmap' codec can't encode characters in position 6-10

构建就挂在那一行 —— 而报错指向 print，看上去和脚本的业务逻辑毫无关系，
第一次遇到极难往"控制台编码"上想。（这个文件就是这么来的：v1.0.2 的
tag 构建正是被它挂掉的。）

用法
----
在脚本开头 import 一次即可：

    from _utf8 import setup as _setup_utf8
    _setup_utf8()

`tools/` 是脚本自己所在的目录，Python 会自动把它放进 sys.path，
所以直接 import 就行，不需要额外配路径。

注意：编码被改成 UTF-8 后，在 cp1252 控制台上中文可能显示成乱码 ——
**乱码可以接受，抛异常不行**。CI 的日志是 UTF-8 解析的，显示正常。
"""

from __future__ import annotations

import sys


def setup() -> None:
    """尽量把两个输出流切成 UTF-8；切不动就算了（被重定向/打桩时不是 TextIOWrapper）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
