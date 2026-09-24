"""xgent_trigger.py CLI 的端到端测试。

CLI 顶层会 load_sections（import 即触发、且会起常驻线程），所以不能在测试进程里
直接 import——改用子进程运行，这也正是它在生产里的真实调用方式（被 run-x 起为
独立进程）。每个用例在**临时工作目录**跑，相对路径 DB `xgent_memory.db` 落在临时处，
绝不碰真实库。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _run_cli(args, *, workdir, chat_id="42", conversation_id="testconv", extra_env=None):
    env = {
        **os.environ,
        "AUTHORIZED_USER_ID": "1",
        "BOT_TOKEN": "",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    if chat_id is not None:
        env["XGENT_CHAT_ID"] = chat_id
    else:
        env.pop("XGENT_CHAT_ID", None)
    if conversation_id is not None:
        env["XGENT_CONVERSATION_ID"] = conversation_id
    else:
        env.pop("XGENT_CONVERSATION_ID", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-u", str(REPO / "xgent_trigger.py"), *args],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc


class TriggerCliTests(unittest.TestCase):
    def setUp(self):
        # 临时工作目录：复制 CLI 运行必需的代码/资源，DB 落这里
        self.workdir = tempfile.mkdtemp(prefix="xgent_trigcli_")
        for name in ("xgent_app", "prompts"):
            shutil.copytree(REPO / name, Path(self.workdir) / name)
        shutil.copy(REPO / "xgent_trigger.py", Path(self.workdir) / "xgent_trigger.py")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_add_registers_and_show_lists(self):
        add = _run_cli(
            ["add", "--cmd", "echo hello", "--after", "30s", "--task", "冒烟"],
            workdir=self.workdir,
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertIn("已登记触发任务", add.stdout)

        show = _run_cli(["show"], workdir=self.workdir)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("冒烟", show.stdout)
        self.assertIn("echo hello", show.stdout)

    def test_repeat_without_when_fails(self):
        proc = _run_cli(["add", "--cmd", "x", "--repeat"], workdir=self.workdir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("repeat", proc.stderr + proc.stdout)

    def test_bad_after_fails(self):
        proc = _run_cli(["add", "--cmd", "x", "--after", "xyz"], workdir=self.workdir)
        self.assertEqual(proc.returncode, 1)

    def test_conflicting_schedule_fails(self):
        proc = _run_cli(
            ["add", "--cmd", "x", "--after", "30s", "--cron", "0 * * * *"],
            workdir=self.workdir,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("只能使用一个时间字段", proc.stderr + proc.stdout)

    def test_missing_conversation_env_rejected(self):
        proc = _run_cli(
            ["add", "--cmd", "echo hi", "--after", "30s"],
            workdir=self.workdir,
            chat_id=None,
            conversation_id=None,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("对话上下文", proc.stderr)

    def test_missing_cmd_argparse_error(self):
        proc = _run_cli(["add", "--after", "30s"], workdir=self.workdir)
        self.assertEqual(proc.returncode, 2)  # argparse 缺必填参数


if __name__ == "__main__":
    unittest.main()
