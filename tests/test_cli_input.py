"""xgent_cli 输入层的行为测试：退出路径与 `/` 命令提示。

xgent_cli 在 import 期就会加载全部 section（需要 AUTHORIZED_USER_ID、会写日志
和数据库），所以这里沿用 test_runtime_smoke 的做法——在带好环境变量的子进程
和临时工作目录里跑探针，不污染仓库，也不需要真的接终端。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PROBE_PREAMBLE = """
import json, sys
sys.path.insert(0, %r)
import xgent_cli
""" % str(ROOT)


class CliProbeMixin:
    def run_probe(self, code: str):
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            # 提示符/菜单里的 ANSI 会让断言变得没法读，探针里一律关掉颜色。
            "NO_COLOR": "1",
        })
        with tempfile.TemporaryDirectory() as cwd:
            env["XGENT_TRACE_LOG_FILE"] = str(Path(cwd) / "xgent_full_trace.log")
            result = subprocess.run(
                [sys.executable, "-c", PROBE_PREAMBLE + code],
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=120,
            )
        if result.returncode != 0:
            self.fail(f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return json.loads(result.stdout.strip().splitlines()[-1])


class ExitPathTests(CliProbeMixin, unittest.TestCase):
    """空闲时按 Ctrl+C 必须一次就退干净。

    老实现把读输入交给 loop.run_in_executor(None, input)，默认执行器的工作线程
    是非守护线程且永远卡在 input() 里，于是 asyncio.run() 的
    shutdown_default_executor() 和解释器退出时的 threading._shutdown() 会连着
    等它两次——用户看到的就是"打印了再见却退不出去，再按一次才退，还甩出一段
    concurrent.futures 的 traceback"。
    """

    def test_reader_thread_is_a_daemon(self):
        result = self.run_probe("""
import asyncio

reader = xgent_cli._StdinReader()
reader._ensure_thread()
print(json.dumps({
    "daemon": reader._thread.daemon,
    "alive": reader._thread.is_alive(),
}))
""")
        self.assertTrue(result["daemon"], "读输入的线程必须是守护线程，否则退出时会被 join 卡住")

    def test_default_executor_is_never_used_for_input(self):
        # 只要还有一次 run_in_executor(None, ...)，退出路径上的两道等待就会回来。
        # 用 AST 判而不是字符串搜：_StdinReader 的 docstring 里正解释着"为什么
        # 不用它"，按字符串搜会被自己的注释绊倒。
        import ast

        tree = ast.parse((ROOT / "xgent_cli.py").read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("run_in_executor", called)

    def test_idle_ctrl_c_resolves_the_pending_read_with_exit_sentinel(self):
        result = self.run_probe("""
import asyncio

async def main():
    reader = xgent_cli._StdinReader()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    # 直接摆一个"正在等输入"的状态，不真的去读 stdin（探针没有 tty）。
    reader._pending = (future, loop, 1)
    handled = reader.request_exit()
    value = await asyncio.wait_for(future, timeout=5)
    again = reader.request_exit()
    print(json.dumps({
        "handled": handled,
        "is_exit": value is xgent_cli._EXIT,
        "second_call": again,
    }))

asyncio.run(main())
""")
        self.assertTrue(result["handled"])
        self.assertTrue(result["is_exit"])
        # 兑现过就不该再兑现第二次，否则 handler 会重复触发退出。
        self.assertFalse(result["second_call"])

    def test_cancel_ctrl_c_resolves_and_stale_reads_are_dropped(self):
        # 状态输入中的 Ctrl+C 兑现成 _CANCEL；之后旧读取才姗姗返回时，
        # 代次号对不上必须丢弃——否则用户取消后随手补敲的键会冒充新输入。
        result = self.run_probe("""
import asyncio

async def main():
    reader = xgent_cli._StdinReader()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    reader._pending = (future, loop, 1)
    handled = reader.request_cancel()
    value = await asyncio.wait_for(future, timeout=5)

    next_future = loop.create_future()
    reader._pending = (next_future, loop, 2)
    reader._resolve("x", 1)   # 旧代次的迟到结果
    try:
        await asyncio.wait_for(asyncio.shield(next_future), timeout=0.2)
        landed = True
    except asyncio.TimeoutError:
        landed = False
    print(json.dumps({
        "handled": handled,
        "is_cancel": value is xgent_cli._CANCEL,
        "stale_dropped": not landed,
    }))

asyncio.run(main())
""")
        self.assertTrue(result["handled"])
        self.assertTrue(result["is_cancel"])
        self.assertTrue(result["stale_dropped"])

    def test_blank_line_is_not_end_of_file(self):
        # 老实现里 _read_input 把 EOFError 也翻译成空串，于是在提示符上按一下
        # 回车就会静默退出 CLI（`if not line: break`）。两者必须可区分。
        result = self.run_probe("""
print(json.dumps({
    "distinct": xgent_cli._EOF is not xgent_cli._EXIT,
    "eof_is_not_empty_string": xgent_cli._EOF != "",
    "exit_is_not_empty_string": xgent_cli._EXIT != "",
}))
""")
        self.assertTrue(result["distinct"])
        self.assertTrue(result["eof_is_not_empty_string"])
        self.assertTrue(result["exit_is_not_empty_string"])

    def test_main_loop_breaks_only_on_sentinels(self):
        source = (ROOT / "xgent_cli.py").read_text(encoding="utf-8")
        self.assertIn("if line is _EXIT or line is _EOF:", source)
        # 空行必须走 continue 而不是 break。
        self.assertIn("if not text:\n            continue", source)


class SlashCommandTests(CliProbeMixin, unittest.TestCase):
    """输入 `/` 就该看到命令列表，而不是被当成一句发给 AI 的普通文本。"""

    def test_bare_slash_opens_the_palette(self):
        result = self.run_probe("""
routed = xgent_cli._route_command_prefix("/")
print(json.dumps({
    "routed": routed,
    "palette": len(xgent_cli._palette),
    "has_start": "start" in xgent_cli._palette,
}))
""")
        self.assertIsNone(result["routed"], "`/` 不该被路由成命令，而是打开面板")
        self.assertGreater(result["palette"], 5)
        self.assertTrue(result["has_start"])

    def test_unique_prefix_is_completed_and_executed(self):
        result = self.run_probe("""
print(json.dumps({"routed": xgent_cli._route_command_prefix("/provider_c")}))
""")
        self.assertEqual("/provider_config", result["routed"])

    def test_ambiguous_prefix_shows_filtered_palette(self):
        result = self.run_probe("""
routed = xgent_cli._route_command_prefix("/prov")
print(json.dumps({
    "routed": routed,
    "palette": sorted(xgent_cli._palette),
}))
""")
        self.assertIsNone(result["routed"])
        self.assertEqual(["provider_config", "providers"], result["palette"])

    def test_exact_command_is_passed_through_untouched(self):
        result = self.run_probe("""
print(json.dumps({
    "exact": xgent_cli._route_command_prefix("/providers"),
    "with_args": xgent_cli._route_command_prefix("/stats 7"),
    "alias": xgent_cli._route_command_prefix("/黑名单"),
}))
""")
        self.assertEqual("/providers", result["exact"])
        # 带参数的命令不能被前缀猜测截胡，否则 "/stats 7" 会退化成 "/stats"。
        self.assertEqual("/stats 7", result["with_args"])
        self.assertEqual("/黑名单", result["alias"])

    def test_unknown_command_still_falls_through_to_the_ai(self):
        result = self.run_probe("""
print(json.dumps({"routed": xgent_cli._route_command_prefix("/zzzz")}))
""")
        self.assertEqual("/zzzz", result["routed"])

    def test_palette_is_single_use(self):
        result = self.run_probe("""
xgent_cli._route_command_prefix("/")
first = len(xgent_cli._take_palette())
second = len(xgent_cli._take_palette())
print(json.dumps({"first": first, "second": second}))
""")
        self.assertGreater(result["first"], 5)
        # 取走即失效，否则它会一直挡住按钮菜单的编号。
        self.assertEqual(0, result["second"])


class CompletionTests(CliProbeMixin, unittest.TestCase):
    def test_completer_lists_commands_for_slash(self):
        result = self.run_probe("""
matches = []
state = 0
while True:
    item = xgent_cli._command_completer("/prov", state)
    if item is None:
        break
    matches.append(item)
    state += 1
print(json.dumps({"matches": sorted(matches)}))
""")
        self.assertEqual(["/provider_config", "/providers"], result["matches"])

    def test_completer_ignores_non_slash_words(self):
        result = self.run_probe("""
print(json.dumps({"first": xgent_cli._command_completer("prov", 0)}))
""")
        self.assertIsNone(result["first"])

    def test_prompt_wraps_ansi_for_readline(self):
        # readline 靠提示符显示宽度算光标位置；ANSI 字节不用 \\001..\\002 圈起来
        # 的话，方向键翻历史会把行内容画错位。
        result = self.run_probe("""
import xgent_app.cli_render as cr
xgent_cli.PALETTE = cr.Palette(True)
prompt = xgent_cli._prompt_text()
print(json.dumps({
    "prompt": prompt,
    "starts_marked": prompt.startswith("\\001"),
    "balanced": prompt.count("\\001") == prompt.count("\\002"),
    "no_newline": "\\n" not in prompt,
}))
""")
        self.assertTrue(result["starts_marked"], result["prompt"])
        self.assertTrue(result["balanced"], result["prompt"])
        # 含换行的提示符会让 readline 重绘错位，空行必须单独打印。
        self.assertTrue(result["no_newline"], result["prompt"])

    def test_descriptions_come_from_the_shared_telegram_table(self):
        result = self.run_probe("""
print(json.dumps({
    "agent": xgent_cli._describe_command("agent"),
    "skills": xgent_cli._describe_command("skills"),
    "alias": xgent_cli._describe_command("黑名单"),
    "unknown": xgent_cli._describe_command("nope"),
}))
""")
        self.assertEqual("开关 Agent 模式", result["agent"])
        self.assertEqual("管理技能库", result["skills"])
        self.assertEqual("管理 Agent 命令黑名单", result["alias"])
        self.assertEqual("", result["unknown"])

    def test_telegram_menu_reuses_the_same_table(self):
        # 抄第二份的结果一定是加了新命令后两边不一致，而用户看到哪一份完全
        # 取决于他用的是哪个客户端。
        source = (ROOT / "xgent_app" / "sections" / "lifecycle.py").read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_COMMAND_DESCRIPTIONS", source)
        self.assertIn(
            "BotCommand(name, description)\n                for name, description in TELEGRAM_COMMAND_DESCRIPTIONS",
            source,
        )


if __name__ == "__main__":
    unittest.main()
