"""粘贴的 pty 冒烟测试：把真终端会发的字节喂进 SlashPalette，看它提交几次。

两种终端各跑一遍：
  bracketed —— 支持括号粘贴（Windows Terminal / VSCode / iTerm2 …），
                内容被包在 \x1b[200~ … \x1b[201~ 里整块到达；
  dumb      —— 不支持（老 conhost、部分 SSH 客户端），粘贴就是一串裸字节，
                里面的换行和真回车没有任何区别，只能靠时序识别。

两种都必须只提交一次、正文一字不差。用法：
    PYTHONPATH=. python3 tools/paste_smoke.py
"""

import os
import pty
import sys
import threading
import time

PAYLOAD = "请选择操作:\n  1) 安装 / 部署 / 配置\n  0) 退出\n请输入选项 [0-10]: 1\n✓ 再见。\n"
TYPED = " 这段是什么意思"
ONE_LINE = "只有一行但末尾带换行\n"


def _pty_case(feed_fn, expect: str, name: str) -> bool:
    """在真 pty 里跑一次 read_line：feed_fn 负责往终端里灌字节。"""
    master, slave = pty.openpty()
    real_stdin, real_stdout = sys.stdin, sys.stdout
    sys.stdin = os.fdopen(slave, "r")
    sys.stdout = os.fdopen(os.dup(slave), "w")

    from xgent_app.cli_palette import SlashPalette

    threading.Thread(target=feed_fn, args=(master,), daemon=True).start()
    try:
        line = SlashPalette("> ", lambda p: [], lambda n, s: []).read_line()
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout
        os.close(master)

    ok = line == expect
    print(f"[{name}] {'OK' if ok else 'FAIL'} 只提交一次，结果={line!r}")
    if not ok:
        print(f"[{name}] 期望={expect!r}")
    return ok


def run_case(name: str, wrap: bool) -> bool:
    body = PAYLOAD.encode()
    if wrap:
        body = b"\x1b[200~" + body + b"\x1b[201~"

    def feed(master: int) -> None:
        time.sleep(0.3)
        os.write(master, body)
        time.sleep(0.3)
        os.write(master, TYPED.encode())  # 粘贴之后再补敲几个字
        time.sleep(0.3)
        os.write(master, b"\r")           # 这一下才是真的提交

    return _pty_case(feed, PAYLOAD.rstrip("\n") + TYPED, name)


def run_trailing_newline_case(name: str, wrap: bool) -> bool:
    """单行粘贴、末尾带一个换行：绝不能自动发出去。

    这是用户报的第二个症状——"粘贴之后会自动回车"。不支持括号粘贴的终端上，
    这一行的换行和真回车字节完全一样，只能靠"它是跟着正文一起整块来的"识别。
    """
    body = ONE_LINE.encode()
    if wrap:
        body = b"\x1b[200~" + body + b"\x1b[201~"

    def feed(master: int) -> None:
        time.sleep(0.3)
        os.write(master, body)
        time.sleep(0.4)          # 这段静默里如果被自动提交了，下面就对不上
        os.write(master, "，然后我补一句".encode())
        time.sleep(0.3)
        os.write(master, b"\r")

    return _pty_case(feed, ONE_LINE.rstrip("\n") + "，然后我补一句", name)


if __name__ == "__main__":
    results = [
        run_case("多行·bracketed", True),
        run_case("多行·dumb", False),
        run_trailing_newline_case("单行尾随换行·bracketed", True),
        run_trailing_newline_case("单行尾随换行·dumb", False),
    ]
    sys.exit(0 if all(results) else 1)
