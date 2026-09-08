"""AgentShellSession 的 PTY 读取线程：关闭时不许留下 traceback，也不许读错 fd。

回归的是 pm2 日志里那段假警报：

  File ".../shell_triggers.py", line 288, in _read_pty_loop
      chunk = os.read(self.controller_fd, 4096)
  TypeError: 'NoneType' object cannot be interpreted as an integer

成因是读线程原先**每轮循环都重新读 self.controller_fd 属性**，而 close() 在另一个
线程上是"先 os.close、再把属性置 None"。close() 一旦插在"上一轮 read 返回数据"与
"下一轮取属性"之间，这里拿到的就是 None，os.read(None, 4096) 抛 TypeError——它不是
OSError，接不住，异常直接逃出守护线程。

比日志噪音更实际的隐患是 fd 复用：属性每轮重取，一旦 close() 关掉 fd N、内核随后
把 N 分给别的文件，下一轮就会去读一个毫不相干的 fd。

sections 靠共享命名空间加载，所以用带环境变量的子进程探针跑（同 test_external_sync）。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "PTY 路径只在 POSIX 存在（Windows 走管道降级）")
class ShellSessionPtyReaderTests(unittest.TestCase):
    def run_probe(self, code: str):
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        })
        with tempfile.TemporaryDirectory() as cwd:
            env["XGENT_TRACE_LOG_FILE"] = str(Path(cwd) / "xgent_full_trace.log")
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=cwd, env=env, text=True, encoding="utf-8",
                capture_output=True, timeout=120,
            )
        if result.returncode != 0:
            self.fail(f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_nulling_controller_fd_mid_read_does_not_raise(self):
        result = self.run_probe("""
import json, os, pty, sys, threading, time
sys.path.insert(0, %r)
from xgent_app.bootstrap import load_sections
ns = {"__file__": "xgent_server.py"}
load_sections(ns)

session = ns["AgentShellSession"]("probe-1", "true")
controller_fd, terminal_fd = pty.openpty()
session.controller_fd = controller_fd

errors = []

def run():
    try:
        session._read_pty_loop()
    except BaseException as exc:          # noqa: BLE001 —— 就是要抓住逃出线程的那个
        errors.append(type(exc).__name__)

thread = threading.Thread(target=run, daemon=True)
thread.start()

os.write(terminal_fd, b"first\\n")
time.sleep(0.4)                            # 完成一次成功读取，再回到阻塞
session.controller_fd = None               # 模拟 close() 的第二步
os.write(terminal_fd, b"second\\n")        # 逼读线程再走一轮
time.sleep(0.4)
os.close(terminal_fd)                      # 对端关闭 -> read 报错/返回空 -> break
thread.join(timeout=5.0)
try:
    os.close(controller_fd)
except OSError:
    pass

print(json.dumps({
    "errors": errors,
    "thread_exited": not thread.is_alive(),
    "got_first": "first" in session.output,
    "got_second": "second" in session.output,
}))
""" % str(ROOT))
        self.assertEqual([], result["errors"],
                         "读线程不许因为属性被置 None 而抛 TypeError——"
                         "那会在 pm2 日志里留下一段假警报的 traceback")
        self.assertTrue(result["thread_exited"], "对端关闭后读线程要干净退出")
        self.assertTrue(result["got_first"])
        self.assertTrue(result["got_second"],
                        "fd 抓成局部量之后，属性被置 None 不该影响仍在进行的读取")


if __name__ == "__main__":
    unittest.main()
