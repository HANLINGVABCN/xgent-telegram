"""三端同步基础设施的测试。

覆盖六件事：
  1. GlobalRecorder 给每条记录盖的进程源标记（metadata.src）——网页观察者
     靠它跳过本进程记录，漏盖会导致网页每句话显示两遍；
  2. get_records_since_rowid 的 rowid 游标查询（含 metadata.origin 透传）；
  3. _external_row_to_frame 的记录到帧映射（CLI 的对话长什么样推给网页）：
     显示语义对齐，AGENT_CMD 协议原文不推（刷屏根源），TOKEN_USAGE 走灰条，
     CLI_STREAM_NOTICE 同样不推给网页（网页已有 SSE busy 状态，不需要这条）；
  4. CLI 到 Telegram 镜像（服务端观察者侧）：镜像 origin=cli-chat 的用户
     文本、最终 AI_REPLY、TOKEN_USAGE 和 CLI_STREAM_NOTICE（去 i 标签的纯
     文本）；状态机输入/命令/协议块/轮次状态不镜像；长文分块；
  5. getchat 跨端历史渲染；
  6. CLI 生成开始时落一条 CLI_STREAM_NOTICE，让 Telegram 镜像不必等到最终
     回复落库才有反应，见 rendering.py 的 _notify_cli_generation_started。

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
    MT2 = ns["MessageType"]
    # 游标可用性：返回的行必须带非空 rowid——id 是 INTEGER PRIMARY KEY 时
    # SQLite 对裸 SELECT rowid 回的列名是 id 不是 rowid，取不到 rowid 游标
    # 就永远停在 0，观察者每个周期全量重发（"一条消息在 TG 出现 12 遍"）。
    rowids = [r.get("rowid") for r in rows]
    advanced_cursor = max(rowids) if rowids else cursor
    print(json.dumps({
        "count": len(rows),
        "src_all_own": all(r.get("src") == own_id for r in rows),
        "types": [r["msg_type"] for r in rows],
        "old_cursor_empty": (await db.get_records_since_rowid(
            await db.get_max_global_rowid())) == [],
        "rowids_all_present": all(rid is not None and rid > 0 for rid in rowids),
        "cursor_advances": (await db.get_records_since_rowid(advanced_cursor)) == [],
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertEqual(3, result["count"])
        self.assertTrue(result["src_all_own"], "漏盖 src 会让网页观察者把本进程消息推两遍")
        self.assertEqual(["user_text", "ai_reply", "system_op"], result["types"])
        self.assertTrue(result["old_cursor_empty"], "游标在最大 rowid 上时不应再取到旧记录")
        self.assertTrue(result["rowids_all_present"],
                        "行里必须带非空 rowid——否则观察者游标永远不前进，全量重发")
        self.assertTrue(result["cursor_advances"],
                        "用返回行里的 rowid 推进游标后，必须取不到已处理过的行")


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
    NL = chr(10)
    await GR.record(MT.AGENT_CMD, "system",
                    NL.join(["```run-x", "<<BEGIN_run_cmd_1a2b", "ls -la", "<<END_run_cmd_1a2b", "```"]))
    await GR.record(MT.TOKEN_USAGE, "assistant", "up 21825 tokens (0 cached), down 1958 tokens")
    await GR.record(MT.CLI_STREAM_NOTICE, "assistant", "CLI is generating a reply")
    rows = await db.get_records_since_rowid(0)
    frames = [ns["_external_row_to_frame"](r) for r in rows]
    print(json.dumps({
        "user": {"type": frames[0]["type"], "external": frames[0].get("external")},
        "ai": {"type": frames[1]["type"], "pm": frames[1].get("parse_mode"),
               "bold": "<b>" in frames[1]["text"]},
        "sys": {"type": frames[2]["type"]},
        "agent_result": {"type": frames[3]["type"], "external": frames[3].get("external")},
        "agent_cmd_hidden": frames[4] is None,
        "token_notice": frames[5] is not None and frames[5]["type"] == "notice",
        "cli_stream_notice_hidden": frames[6] is None,
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
        self.assertTrue(result["agent_result"]["external"])
        # AGENT_CMD 协议原文绝不推给网页——此前每个协议块一个新气泡，
        # CLI 一轮 Agent 对话就是"网页刷屏"的根源。
        self.assertTrue(result["agent_cmd_hidden"], "AGENT_CMD 协议原文不应推成网页帧")
        self.assertTrue(result["token_notice"], "TOKEN_USAGE 要降级成 notice 灰条，与显示历史一致")
        self.assertTrue(result["cli_stream_notice_hidden"],
                        "CLI_STREAM_NOTICE 不该推给网页——网页已经通过 SSE 的 busy/typing "
                        "状态知道生成中了，只有 Telegram 镜像需要这条信号")
        self.assertIsNone(result["empty"])


class ServerSideCliMirrorTests(SectionsProbeMixin, unittest.TestCase):
    """CLI→Telegram 镜像已移到服务端观察者（idle.py）——CLI 只写库，服务端用
    自己的 PTB 连接镜像。这里测纯函数部分：记录→镜像文本的映射与开关。"""

    def test_row_to_telegram_texts_scope_and_chunks(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    GR = ns["GlobalRecorder"]
    MT = ns["MessageType"]
    # CLI 对话文本（带 origin 标记）与状态机输入（不带）都落库
    await GR.record_user_message(
        "我在 CLI 里说的话", MT.USER_TEXT, metadata={"origin": "cli-chat"})
    await GR.record_user_message("填进状态机的 API Key", MT.USER_TEXT)
    # Agent 多轮：中间轮响应（带协议块）打 agent_intermediate，最终轮不打
    await GR.record_ai_reply(
        "中间轮，带协议块", metadata={"agent_intermediate": True})
    await GR.record_ai_reply("AI 在 CLI 里的最终回答")
    # Ctrl+C 停止标记：no_mirror，绝不镜像到 TG
    await GR.record_ai_reply(
        "⏹️ 当前回复已被用户手动停止", metadata={"no_mirror": True})
    await GR.record(MT.TOKEN_USAGE, "assistant", "<i>up 1 tokens</i>")
    await GR.record(MT.CLI_STREAM_NOTICE, "assistant", "<i>CLI notice text</i>")
    await GR.record(MT.AGENT_CMD, "system", "```run-x")
    await GR.record(MT.AGENT_STATUS, "assistant", "✅ Agent 第 1 轮 · 1 个操作已完成")
    rows = await db.get_records_since_rowid(0)
    texts = []
    for r in rows:
        texts.extend(ns["_external_row_to_telegram_texts"](r))
    # 长文分块：渲染层的 split_text_for_telegram 负责切 Telegram 发得下的块
    long_reply = "AI 长回复" + ("x" * 9000)
    chunks = ns["split_text_for_telegram"](long_reply)
    print(json.dumps({
        "user_sent": any("🖥 [CLI]" in t and "我在 CLI 里说的话" in t for t in texts),
        "ai_sent": any("AI 在 CLI 里的最终回答" in t for t in texts),
        "intermediate_skipped": all("中间轮" not in t for t in texts),
        "stop_marker_skipped": all("⏹️" not in t for t in texts),
        "state_input_not_sent": all("API Key" not in t for t in texts),
        "token_sent_plain": any("up 1 tokens" in t and "<i>" not in t for t in texts),
        "cli_notice_sent_plain": any("CLI notice text" in t and "<i>" not in t for t in texts),
        "protocol_and_status_skipped": all(("run-x" not in t and "Agent 第" not in t) for t in texts),
        "chunks": len(chunks) > 1 and max(len(c) for c in chunks) <= 4000,
        "chars_kept": sum(len(c) for c in chunks) >= len(long_reply.replace(" ", "")),
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["user_sent"], "镜像必须带上用户的话（🖥 [CLI] 前缀）")
        self.assertTrue(result["ai_sent"])
        self.assertTrue(result["intermediate_skipped"],
                        "Agent 中间轮响应绝不镜像——每轮都镜像曾把 TG 刷成洪水")
        self.assertTrue(result["stop_marker_skipped"],
                        "Ctrl+C 停止标记（⏹️）是回合收尾噪音，绝不镜像到 TG")
        self.assertTrue(result["state_input_not_sent"], "状态机输入（填 Key 等）不镜像到 TG")
        self.assertTrue(result["token_sent_plain"],
                        "token 用量要镜像到 TG（去掉 <i> 标签，走纯文本发送）")
        self.assertTrue(result["cli_notice_sent_plain"],
                        "CLI 生成开始占位提示要镜像到 TG（去掉 <i> 标签），"
                        "否则 Telegram 端要等到最终回复落库才有反应，感觉不到立即同步")
        self.assertTrue(result["protocol_and_status_skipped"], "协议块/轮次状态一律不镜像")
        self.assertTrue(result["chunks"], "超长回复要切成 Telegram 发得下的块")
        self.assertTrue(result["chars_kept"], "分块不能丢内容")

    def test_watcher_each_row_mirrored_exactly_once(self):
        """端到端：跑真正的观察者循环多个轮询周期，另一个进程（模拟 CLI）
        写入记录。每行必须恰好镜像一次——不多不少。这是"一条 CLI 消息在
        TG 重复出现 12 遍"生产事故的回归测试。"""
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio, os, subprocess, sys, time

sent = []
class _FakeBot:
    async def send_message(self, chat_id=None, text="", **kwargs):
        sent.append(text)

WATCH_SECONDS = 5.5   # 覆盖 5 个轮询周期（1s 间隔）

# 模拟 CLI 的独立进程：延迟启动（等观察者先起跑），写入一条用户消息 + 最终回复。
# 项目根从 PYTHONPATH 继承（run_probe 已设好），避免嵌套字符串插值。
WRITER = (
    "import asyncio, os, sys\\n"
    "sys.path.insert(0, os.environ['PYTHONPATH'])\\n"
    "from xgent_app.bootstrap import load_sections\\n"
    "ns = {'__file__': 'xgent_server.py'}\\n"
    "load_sections(ns)\\n"
    "async def main():\\n"
    "    await asyncio.sleep(2.0)\\n"
    "    GR = ns['GlobalRecorder']; MT = ns['MessageType']\\n"
    "    await GR.record_user_message('在吗', MT.USER_TEXT, metadata={'origin': 'cli-chat'})\\n"
    "    await GR.record_ai_reply('最终回复，只该出现一次')\\n"
    "    db = await ns['BotMemoryDB'].get_instance()\\n"
    "    await db.close()\\n"
    "asyncio.run(main())\\n"
)

async def main():
    await ns["UserDataManager"].init()
    ns["_web_real_bot"] = _FakeBot()
    ns["_web_external_outbox"] = ns["WebOutbox"]()
    task = asyncio.get_running_loop().create_task(ns["_web_external_record_watcher"]())
    proc = subprocess.Popen([sys.executable, "-c", WRITER])
    await asyncio.sleep(WATCH_SECONDS)
    proc.wait(timeout=30)
    task.cancel()
    user_count = sum(1 for t in sent if "在吗" in t)
    reply_count = sum(1 for t in sent if "最终回复" in t)
    print(json.dumps({
        "user_count": user_count,
        "reply_count": reply_count,
        "total": len(sent),
    }))
    db = await ns["BotMemoryDB"].get_instance()
    await db.close()

asyncio.run(main())
""")
        self.assertEqual(1, result["user_count"],
                         f"用户消息必须恰好镜像一次，实际 {result['user_count']} 次——重复即刷屏事故回归")
        self.assertEqual(1, result["reply_count"],
                         f"最终回复必须恰好镜像一次，实际 {result['reply_count']} 次")
        self.assertEqual(2, result["total"])

    def test_watcher_rate_limit_stops_flood(self):
        """限流保险丝：外部进程一次灌入大量可镜像记录，TG 实际发送数必须
        被每分钟上限硬性截停。这正是生产刷屏事故的复现场景。"""
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio, time

sent = []
class _FakeBot:
    async def send_message(self, chat_id=None, text="", **kwargs):
        sent.append(text)

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    ns["_web_real_bot"] = _FakeBot()
    ns["_web_external_outbox"] = ns["WebOutbox"]()
    # 模拟失控场景：外部进程一口气写入 80 条最终 AI_REPLY
    GR = ns["GlobalRecorder"]
    MT = ns["MessageType"]
    real_src = ns["_RECORDER_SOURCE_ID"]
    ns["_RECORDER_SOURCE_ID"] = "external-fake"   # 盖外部源标记
    for i in range(80):
        await GR.record_ai_reply(f"洪水消息 {i}")
    ns["_RECORDER_SOURCE_ID"] = real_src
    # 直接跑观察者的一轮循环体：不等 sleep，手动取行+镜像
    rows = await db.get_records_since_rowid(0)
    texts = []
    for r in rows:
        texts.extend(ns["_external_row_to_telegram_texts"](r))
    cap = ns["_TG_MIRROR_MAX_PER_MINUTE"]
    print(json.dumps({
        "texts_generated": len(texts),
        "cap": cap,
        "texts_over_cap": len(texts) > cap,
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertGreaterEqual(result["texts_generated"], 80)
        self.assertLess(result["cap"], result["texts_generated"],
                        "限流上限必须显著低于失控时的生成量，保险丝才有意义")

    def test_tg_mirror_gate_requires_bot_and_env_switch(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import os

ns["_web_real_bot"] = None
no_bot = ns["_web_external_tg_mirror_enabled"]()      # 没有真实 bot：不镜像
ns["_web_real_bot"] = object()
with_bot = ns["_web_external_tg_mirror_enabled"]()    # 有 bot：镜像
os.environ["XGENT_CLI_NO_TG_MIRROR"] = "1"
switched_off = ns["_web_external_tg_mirror_enabled"]()  # 环境开关：关
del os.environ["XGENT_CLI_NO_TG_MIRROR"]
print(json.dumps({"no_bot": no_bot, "with_bot": with_bot, "switched_off": switched_off}))
""")
        self.assertFalse(result["no_bot"], "纯 Web 模式（无真实 bot）不应镜像")
        self.assertTrue(result["with_bot"])
        self.assertFalse(result["switched_off"], "XGENT_CLI_NO_TG_MIRROR=1 要能关掉镜像")

    def test_watcher_survives_web_off_and_swaps_outbox(self):
        """观察者与 Web 开关解耦：Web 没开也要启动（CLI→TG 镜像依赖它）。"""
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    # web_enabled 关着：start_web_chat_if_enabled 仍要启动跨端观察者
    ns["UserDataManager"].set("web_enabled", False)
    await ns["start_web_chat_if_enabled"](None)
    task = ns["_web_external_watch_task"]
    print(json.dumps({
        "watcher_running": task is not None and not task.done(),
        "no_web_server": ns["_web_chat_server"] is None,
    }))
    if task is not None:
        task.cancel()
    # aiosqlite 连接线程非 daemon：不关掉，探针进程退出时会卡在 threading 收尾
    db = await ns["BotMemoryDB"].get_instance()
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["watcher_running"], "Web 关闭时跨端观察者也要运行")
        self.assertTrue(result["no_web_server"])

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
    "user_row": "❯ User" in out and "跨端的用户消息" in out,
    "ai_row": "◆ XGent" in out and "加粗" in out,
    "sys_row": "系统操作记录" in out,
}))
""")
        self.assertTrue(result["user_row"], "用户消息要带 ❯ User 标记")
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
    await GR.record(MT.TOKEN_USAGE, "assistant", "up 21825 tokens (0 cached), down 1958 tokens")
    await GR.record(MT.CLI_STREAM_NOTICE, "assistant", "CLI is generating a reply")
    await GR.record_ai_reply("正文回复 **加粗**")
    await GR.record(MT.AGENT_STATUS, "assistant", "✅ Agent 第 1 轮 · 1 个操作已完成")
    await GR.record_user_message("/restart", MT.COMMAND)

    rows = await db.get_display_history(10)
    web = await ns["_web_read_history"](10)
    ctx = await db.get_conversation_messages(50)
    status_rows = [r for r in rows if r["msg_type"] == MT.AGENT_STATUS]
    token_web = [r for r in web if r.get("parse_mode") and "21825" in r["content"]]
    all_display = " ".join(r["content"] for r in rows)
    all_ctx = " ".join(str(m.get("content") or "") for m in ctx)
    print(json.dumps({
        "cmd_hidden": all(r["msg_type"] != MT.AGENT_CMD for r in rows),
        "cli_stream_notice_hidden": all(r["msg_type"] != MT.CLI_STREAM_NOTICE for r in rows),
        "status_shown": any(r["content"] == "✅ Agent 第 1 轮 · 1 个操作已完成" for r in status_rows),
        "no_raw_fence": "```" not in all_display,
        "no_media_prompt": "Please generate" not in all_display,
        "token_gray": token_web and token_web[0]["role"] == "system",
        "ai_reply_html": any("<b>" in r["content"] for r in web if r.get("parse_mode") == "HTML"),
        "status_not_in_ctx": "Agent 第 1 轮" not in all_ctx,
        "cmd_prefixed_in_ctx": "[命令] /restart" in all_ctx or "[命令]" in all_ctx,
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["cmd_hidden"], "AGENT_CMD 协议原文在显示历史里应整体隐藏")
        self.assertTrue(result["cli_stream_notice_hidden"],
                        "CLI_STREAM_NOTICE 是纯跨端信号行，Web/CLI 的历史列表里不该出现")
        self.assertTrue(result["status_shown"], "落库的轮次状态行要原样显示（与 bot 界面逐字一致）")
        self.assertTrue(result["status_not_in_ctx"], "轮次状态是 UI 信息，不进 AI 上下文")
        self.assertTrue(result["cmd_prefixed_in_ctx"], "命令记录在 AI 上下文里要带 [命令] 前缀")
        self.assertTrue(result["no_raw_fence"], "刷新后的显示里不允许出现协议围栏原文")
        self.assertTrue(result["no_media_prompt"], "媒体生成的完整提示词不该出现在显示里")
        self.assertTrue(result["token_gray"], "token 统计行要降级成 system 灰条")
        self.assertTrue(result["ai_reply_html"], "AI 正文仍走 Markdown->HTML，不受影响")


class CliUserEchoTests(ProbeMixin, unittest.TestCase):
    """CLI 用户消息成块回显（user 样式）。TG 镜像见 ServerSideCliMirrorTests。"""

    def test_user_block_and_line_echo(self):
        result = self.run_probe("""
import asyncio, io
import xgent_cli
from xgent_app.cli_render import TerminalScreen

stream = io.StringIO()
xgent_cli.SCREEN = TerminalScreen(stream=stream, color=False, width=100)
xgent_cli._READER.last_read_native_echo = False

async def main():
    await xgent_cli._init_runtime()
    xgent_cli.UserDataManager.set('state', xgent_cli.BotState.IDLE)
    xgent_cli._echo_submitted("你好，洛溪", conversation=True)
    block_out = stream.getvalue()
    xgent_cli._echo_submitted("/start", conversation=False)
    line_out = stream.getvalue()[len(block_out):]
    print(json.dumps({
        "block_has_marker": "❯ User" in block_out,
        "block_has_text": "你好，洛溪" in block_out,
        "line_single": "/start" in line_out and "❯ User" not in line_out,
    }))
    await xgent_cli._shutdown_runtime()

asyncio.run(main())
""")
        self.assertTrue(result["block_has_marker"], "对话消息要有 ❯ User 用户块")
        self.assertTrue(result["block_has_text"])
        self.assertTrue(result["line_single"], "命令只回显单行，不带用户块")


class CliStreamNoticeTests(SectionsProbeMixin, unittest.TestCase):
    """CLI 生成开始时的即时同步：见用户报告的"后台流式输出中...这个消息没有
    立即同步到 bot 里"——CliBot.send_message 只画终端屏幕不落库，服务端
    跨端观察者要等到最终回复落库才第一次有动静可镜像。_notify_cli_generation_started
    补一条 CLI_STREAM_NOTICE 记录，让观察者能在生成刚开始时就推一条镜像。"""

    def test_notify_only_fires_for_cli_bot(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    GR = ns["GlobalRecorder"]
    MT = ns["MessageType"]

    class FakeCliBot:
        _is_xgent_cli_bot = True

    class FakeWebBot:
        pass

    class Ctx:
        def __init__(self, bot):
            self.bot = bot

    cursor = await db.get_max_global_rowid()
    await ns["_notify_cli_generation_started"](Ctx(FakeCliBot()), 1)
    await ns["_notify_cli_generation_started"](Ctx(FakeWebBot()), 1)
    rows = await db.get_records_since_rowid(cursor)
    notice_rows = [r for r in rows if r["msg_type"] == MT.CLI_STREAM_NOTICE]
    print(json.dumps({
        "cli_bot_wrote_one": len(notice_rows) == 1,
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["cli_bot_wrote_one"],
                        "只有 CLI bot（_is_xgent_cli_bot 标记）该落这条通知，"
                        "Web/Telegram 会话本来就是原生实时发消息，不需要额外信号")


if __name__ == "__main__":
    unittest.main()
