"""三端同步基础设施的测试。

覆盖四件事：
  1. GlobalRecorder 给每条记录盖的进程源标记（metadata.src）——网页观察者
     靠它跳过本进程记录，漏盖会导致网页每句话显示两遍；
  2. get_records_since_rowid 的 rowid 游标查询；
  3. _external_row_to_frame 的记录→帧映射（CLI 的对话长什么样推给网页）；
  4. CLI 侧的 Telegram 镜像分块与 /sync 跨端历史渲染。

sections 靠共享命名空间加载、xgent_cli 在 import 期就加载全部 section，
所以都用带环境变量的子进程探针跑（同 test_cli_input 的做法）。
"""

import io
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
""" % str(ROOT)


class ProbeMixin:
    def run_probe(self, code: str, extra_env=None):
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        })
        env.update(extra_env or {})
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


class SectionsProbeMixin(ProbeMixin):
    """加载完整 sections 命名空间的探针。"""

    SECTIONS_PREAMBLE = """
from xgent_app.bootstrap import load_sections
ns = {"__file__": "xgent_server.py"}
load_sections(ns)
"""


class RecorderSourceStampTests(SectionsProbeMixin, unittest.TestCase):
    def test_records_carry_source_id_and_cursor_query_works(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    cursor = await db.get_max_global_rowid()
    GR = ns["GlobalRecorder"]
    MT = ns["MessageType"]
    await GR.record_user_message("hello from cli", MT.USER_TEXT)
    await GR.record_ai_reply("hi *bold* reply")
    await GR.record_system_op("导出全部数据")
    rows = await db.get_records_since_rowid(cursor)
    own_id = ns["_RECORDER_SOURCE_ID"]
    print(json.dumps({
        "count": len(rows),
        "src_all_own": all(r.get("src") == own_id for r in rows),
        "types": [r["msg_type"] for r in rows],
        "old_cursor_empty": (await db.get_records_since_rowid(
            await db.get_max_global_rowid())) == [],
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertEqual(3, result["count"])
        self.assertTrue(result["src_all_own"], "漏盖 src 会让网页观察者把本进程消息推两遍")
        self.assertEqual(["user_text", "ai_reply", "system_op"], result["types"])
        self.assertTrue(result["old_cursor_empty"], "游标在最大 rowid 上时不应再取到旧记录")


class ExternalFrameMappingTests(SectionsProbeMixin, unittest.TestCase):
    def test_row_to_frame_mapping_by_role_and_type(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    GR = ns["GlobalRecorder"]
    MT = ns["MessageType"]
    await GR.record_user_message("cli 说的话", MT.USER_TEXT)
    await GR.record_ai_reply("回 **加粗** 正文")
    await GR.record_system_op("系统操作记录")
    await GR.record(MT.AGENT_RESULT, "user", "[sendfile结果] 已发送服务器文件给用户: /x/a.txt (1 bytes)")
    rows = await db.get_records_since_rowid(0)
    frames = [ns["_external_row_to_frame"](r) for r in rows]
    print(json.dumps({
        "user": {"type": frames[0]["type"], "external": frames[0].get("external")},
        "ai": {"type": frames[1]["type"], "pm": frames[1].get("parse_mode"),
               "bold": "<b>" in frames[1]["text"]},
        "sys": {"type": frames[2]["type"]},
        "agent_result": {"type": frames[3]["type"], "external": frames[3].get("external")},
        "empty": ns["_external_row_to_frame"]({"msg_type": MT.USER_TEXT, "role": "user", "content": "  "}),
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertEqual("user_message", result["user"]["type"])
        self.assertTrue(result["user"]["external"])
        self.assertEqual("message", result["ai"]["type"])
        self.assertEqual("HTML", result["ai"]["pm"])
        self.assertTrue(result["ai"]["bold"], "AI_REPLY 的 Markdown 要转成 HTML")
        self.assertEqual("notice", result["sys"]["type"])
        # AGENT_RESULT 显示到 AI 侧（role 改映射为 assistant -> message 帧）
        self.assertEqual("message", result["agent_result"]["type"])
        self.assertIsNone(result["empty"])


class CliMirrorTests(ProbeMixin, unittest.TestCase):
    def test_tg_chunks_respect_limit_and_keep_content(self):
        result = self.run_probe("""
import xgent_cli

text = ("line\\n" * 1500) + "tail"
chunks = xgent_cli._split_tg_chunks(text, limit=3800)
joined = "".join(chunks)
print(json.dumps({
    "chunks": len(chunks),
    "max_len": max(len(c) for c in chunks),
    "chars_kept": len(joined.replace("\\n", "")) == len(text.replace("\\n", "")),
    "short_passthrough": xgent_cli._split_tg_chunks("short") == ["short"],
    "empty": xgent_cli._split_tg_chunks("") == [],
}))
""")
        self.assertGreater(result["chunks"], 1)
        self.assertLessEqual(result["max_len"], 3800)
        self.assertTrue(result["chars_kept"])
        self.assertTrue(result["short_passthrough"])
        self.assertTrue(result["empty"])

    def test_mirror_disabled_without_token_or_with_env_switch(self):
        result = self.run_probe("""
import asyncio
import xgent_cli

async def main():
    # 测试环境 BOT_TOKEN 是占位值；用环境开关走"直接返回"分支即可验证开关。
    import os
    os.environ["XGENT_CLI_NO_TG_MIRROR"] = "1"
    before = xgent_cli.BotConfig.TOKEN
    token_saved = xgent_cli.BotConfig.TOKEN
    xgent_cli.BotConfig.TOKEN = ""
    await xgent_cli._mirror_turn_to_telegram(0)      # 无 token：静默返回
    xgent_cli.BotConfig.TOKEN = token_saved
    await xgent_cli._mirror_turn_to_telegram(0)      # 有 token + 开关：静默返回
    print(json.dumps({"ok": True, "had_token": bool(before)}))

asyncio.run(main())
""")
        self.assertTrue(result["ok"])

    def test_sync_renders_roles_to_screen(self):
        result = self.run_probe("""
import io
import xgent_cli
from xgent_app.cli_render import TerminalScreen
from xgent_app.cli_bridge import set_screen

stream = io.StringIO()
screen = TerminalScreen(stream=stream, color=False, width=100)
xgent_cli.SCREEN = screen
rows = [
    {"role": "user", "content": "跨端的用户消息", "msg_type": "user_text"},
    {"role": "assistant", "content": "AI 的 *加粗* 回复", "msg_type": "ai_reply"},
    {"role": "system", "content": "系统操作记录", "msg_type": "system_op"},
]
xgent_cli._render_history_rows(rows)
out = stream.getvalue()
print(json.dumps({
    "user_row": "❯ 你" in out and "跨端的用户消息" in out,
    "ai_row": "◆ XGent" in out and "加粗" in out,
    "sys_row": "系统操作记录" in out,
}))
""")
        self.assertTrue(result["user_row"], "用户消息要带 ❯ 你 标记")
        self.assertTrue(result["ai_row"], "AI 回复要走 Markdown 渲染并带 XGent 头")
        self.assertTrue(result["sys_row"])


class RestartFromCliTests(SectionsProbeMixin, unittest.TestCase):
    """CLI 发起 /restart：检测外部 PM2 托管、CLI 会话不退出。"""

    @unittest.skipIf(os.name == "nt", "fake pm2 是 shell 脚本，只在 POSIX 可执行")
    def test_cli_restart_finds_external_pm2_and_keeps_session(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio, json, os, tempfile

# 假 pm2：jlist 返回一个 pm_cwd 指向项目根的应用
bindir = tempfile.mkdtemp()
fake_pm2 = os.path.join(bindir, "pm2")
project_root = ns["PROJECT_ROOT"]
with open(fake_pm2, "w") as handle:
    handle.write(
        "#!/bin/sh\\n"
        "echo '[{\\"name\\": \\"xgent-telegram\\", \\"pm2_env\\": {\\"pm_cwd\\": \\"%s\\"}}]'\\n"
        % project_root
    )
os.chmod(fake_pm2, 0o755)
os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]

app = ns["_find_external_pm2_app"]()

# 探针里 PROJECT_ROOT 是临时目录，放一个只会写标记的假 install.sh：
# restart 真去跑它也无害，跑没跑过看标记文件。
marker = os.path.join(project_root, "restart-marker")
install_sh = os.path.join(project_root, "install.sh")
with open(install_sh, "w") as handle:
    handle.write("#!/bin/sh\\necho restart-called >> %s\\n" % marker)

sent = []
class _FakeCliBot:
    _is_xgent_cli_bot = True
    async def send_message(self, chat_id=None, text="", **kwargs):
        sent.append(text)

async def main():
    await ns["UserDataManager"].init()
    # CLI 进程环境里没有任何 PM2 变量——这正是要修的场景
    for key in ("PM2_HOME", "pm_id", "PM2_USAGE"):
        os.environ.pop(key, None)
    await ns["restart_current_process"](42, _FakeCliBot())
    print(json.dumps({
        "app_found": app == "xgent-telegram",
        "no_exit": True,
        "install_restart_called": os.path.exists(marker),
        "kept_session_msg": any("CLI 会话保持" in t for t in sent),
    }))
    db = await ns["BotMemoryDB"].get_instance()
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["app_found"], "pm2 jlist 里应按项目目录匹配到应用")
        self.assertTrue(result["no_exit"], "走到这里说明 CLI 会话没有被 sys.exit 带走")
        self.assertTrue(result["install_restart_called"])
        self.assertTrue(result["kept_session_msg"])

    @unittest.skipIf(os.name == "nt", "fake pm2 是 shell 脚本，只在 POSIX 可执行")
    def test_cli_restart_without_any_daemon_still_keeps_session(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio, json, os, tempfile

# 没有 pm2 可执行文件 -> 外部检测落空
os.environ["PATH"] = tempfile.mkdtemp()
for key in ("PM2_HOME", "pm_id", "PM2_USAGE", "INVOCATION_ID", "JOURNAL_STREAM"):
    os.environ.pop(key, None)

project_root = ns["PROJECT_ROOT"]
marker = os.path.join(project_root, "restart-marker")
install_sh = os.path.join(project_root, "install.sh")
with open(install_sh, "w") as handle:
    handle.write("#!/bin/sh\\necho restart-called >> %s\\n" % marker)

sent = []
class _FakeCliBot:
    _is_xgent_cli_bot = True
    async def send_message(self, chat_id=None, text="", **kwargs):
        sent.append(text)

async def main():
    await ns["UserDataManager"].init()
    await ns["restart_current_process"](42, _FakeCliBot())
    print(json.dumps({
        "no_exit": True,
        "no_install_call": not os.path.exists(marker),
        "manual_hint": any("install.sh" in t for t in sent),
    }))
    db = await ns["BotMemoryDB"].get_instance()
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["no_exit"])
        self.assertTrue(result["no_install_call"])
        self.assertTrue(result["manual_hint"])


class DisplayHistoryCompactionTests(SectionsProbeMixin, unittest.TestCase):
    """刷新后的历史显示压缩：协议块/媒体提示词折叠成一行，token 行降级灰条。"""

    def test_agent_cmd_and_token_rows_compact_for_display(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    GR = ns["GlobalRecorder"]
    MT = ns["MessageType"]
    await GR.record(MT.AGENT_CMD, "system", "[Agent媒体生成] Please generate an image: " + "x" * 500)
    NL = chr(10)
    await GR.record(MT.AGENT_CMD, "system",
                    NL.join(["```media-x", "<<BEGIN_gen_luoxi_3d1a",
                             "Please generate", "<<END_gen_luoxi_3d1a", "```"]))
    await GR.record(MT.AGENT_CMD, "system",
                    NL.join(["```edit-x:/app/bot.py", "<<BEGIN_edit_greet_82e6",
                             "OLD", "<<END_edit_greet_82e6", "```"]))
    await GR.record(MT.TOKEN_USAGE, "assistant", "↑ 21825 tokens (0 cached) · ↓ 1958 tokens")
    await GR.record_ai_reply("正文回复 **加粗**")

    rows = await db.get_display_history(10)
    web = await ns["_web_read_history"](10)
    compact = [r for r in rows if r["msg_type"] == MT.AGENT_CMD]
    token_web = [r for r in web if r.get("parse_mode") and "21825" in r["content"]]
    print(json.dumps({
        "media_folded": compact[0]["content"] == "🖼 [Agent媒体生成] 提示词已折叠",
        "media_fence": compact[1]["content"] == "⚙ 已执行 media-x",
        "edit_fence": compact[2]["content"] == "⚙ 已执行 edit-x",
        "token_gray": token_web and token_web[0]["role"] == "system",
        "ai_reply_html": any("<b>" in r["content"] for r in web if r.get("parse_mode") == "HTML"),
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["media_folded"], "媒体生成提示词要折叠成一行")
        self.assertTrue(result["media_fence"], "协议块要压缩成 ⚙ 已执行 media-x")
        self.assertTrue(result["edit_fence"])
        self.assertTrue(result["token_gray"], "token 统计行要降级成 system 灰条")
        self.assertTrue(result["ai_reply_html"], "AI 正文仍走 Markdown->HTML，不受影响")


if __name__ == "__main__":
    unittest.main()
