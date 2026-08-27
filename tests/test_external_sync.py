"""三端同步基础设施的测试。

核心约定：**CLI 里对话，Telegram/网页看到的东西和在 Telegram 里直接对话
完全一样**——带停止按钮的占位提示、Agent 每轮状态行、工具/命令返回、流式
编辑、消息删除，一条不少；唯一的差异是用户自己那句话前面带 "🖥 [CLI]"。
做法是在 bot 方法层扇出：CLI 把对话核心对 CliBot 的每次调用写进
cli_relay_ops，服务端回放器重放到 MirrorBot（同时打真实 TG 和网页 SSE）。

覆盖：
  1. GlobalRecorder 给每条记录盖的进程源标记（metadata.src）；
  2. get_records_since_rowid 的 rowid 游标查询（含 metadata.origin 透传）；
  3. CLI 中继：bot 操作按序落库、回放到 Telegram 时逐条一致（含停止按钮、
     编辑、删除），且不做任何类型过滤；
  4. 用户消息是唯一带来源标识的一处；
  5. getchat 跨端历史渲染；
  6. 回放器与 Web 开关解耦（Web 关着也要跑）。

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


class RelayReplayTests(SectionsProbeMixin, unittest.TestCase):
    """CLI 的 bot 操作流 -> Telegram，逐条一致。

    这是整套同步的核心断言。此前服务端是读 global_messages 的行按类型白名单
    重新编成文本再发，中间轮次/命令返回/Agent 状态行/带停止按钮的占位消息
    全都编不出来（用户看到的就是"同步过去少了一大半，还多出一句我没写过的
    提示"）。现在回放的是操作本身，所以这里直接断言"发了什么就到了什么"。
    """

    def test_relay_ops_replay_verbatim_to_telegram(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio
from xgent_app import cli_bridge

calls = []
class _FakeBot:
    async def send_message(self, chat_id=None, text="", reply_markup=None, **kwargs):
        assigned = 5000 + len(calls)
        calls.append(("send", text, _buttons(reply_markup), assigned))
        class _M:
            message_id = assigned
        return _M()
    async def edit_message_text(self, text="", chat_id=None, message_id=None,
                                reply_markup=None, **kwargs):
        calls.append(("edit", text, message_id))
    async def delete_message(self, chat_id=None, message_id=None, **kwargs):
        calls.append(("delete", message_id))
    async def send_chat_action(self, chat_id=None, action=None, **kwargs):
        calls.append(("action", str(action)))

def _buttons(markup):
    kb = getattr(markup, "inline_keyboard", None)
    if not kb:
        return []
    return [b.text for row in kb for b in row]

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    ns["_web_real_bot"] = _FakeBot()
    ns["_web_external_outbox"] = ns["WebOutbox"]()

    # 回放器的游标从启动时的最大 id 起步（不重播历史），所以必须先起回放器
    # 再写操作——真实场景里服务端是常驻的，CLI 后启动。
    task = asyncio.get_running_loop().create_task(ns["_web_external_record_watcher"]())
    await asyncio.sleep(0.5)

    # --- 模拟 CLI 进程：把一轮真实对话会产生的 bot 调用写进中继 ---
    cli_bridge.configure_relay(ns["BotConfig"].DB_FILE, 1, "sess-test")
    cli_bridge.relay_user_message("帮我看下这个 bug")
    bot = cli_bridge.CliBot(1)
    stop_kb = ns["build_stop_keyboard"]()
    # 1) 原生的带停止按钮占位消息
    msg = await bot.send_message(1, "流式输出中...", reply_markup=stop_kb)
    # 2) Agent 轮次状态行（以前被整类丢弃）
    await bot.send_message(1, "\\u2705 Agent 第 1 轮 · 1 个操作已完成")
    # 3) 工具返回卡片（以前被整类丢弃）
    await bot.send_message(1, "<b>run</b> ls -la", parse_mode="HTML")
    # 4) 流式编辑到最终回复
    await bot.edit_message_text("最终回复正文", message_id=msg.message_id)
    # 5) 占位消息用完删掉
    await bot.delete_message(message_id=msg.message_id)
    cli_bridge.close_relay()
    await asyncio.sleep(2.0)
    task.cancel()

    sends = [c for c in calls if c[0] == "send"]
    edits = [c for c in calls if c[0] == "edit"]
    deletes = [c for c in calls if c[0] == "delete"]
    placeholder = [c for c in sends if c[1] == "流式输出中..."]
    print(json.dumps({
        "user_echo_once": sum(1 for c in sends if "帮我看下这个 bug" in c[1]) == 1,
        "user_echo_marked": any(c[1].startswith("🖥 [CLI]") for c in sends),
        "only_user_marked": sum(1 for c in sends if "[CLI]" in c[1]) == 1,
        "placeholder_verbatim": len(placeholder) == 1,
        "placeholder_has_stop_button": bool(placeholder) and placeholder[0][2] != [],
        "agent_status_relayed": any("Agent 第 1 轮" in c[1] for c in sends),
        "tool_card_relayed": any("run</b> ls -la" in c[1] for c in sends),
        "edit_applied": any("最终回复正文" in c[1] for c in edits),
        # 编辑必须打在"占位消息那一条"真实拿到的 id 上，而不是别的消息或 CLI 的假 id
        "edit_targets_real_id": bool(edits) and bool(placeholder)
                                and edits[0][2] == placeholder[0][3],
        "placeholder_deleted": len(deletes) == 1,
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["user_echo_once"], "用户消息必须恰好同步一次")
        self.assertTrue(result["user_echo_marked"], "用户自己的话要带 🖥 [CLI] 来源标识")
        self.assertTrue(result["only_user_marked"],
                        "**只有**用户消息带来源标识——其余任何一条都不该被加标记或改写")
        self.assertTrue(result["placeholder_verbatim"],
                        "占位提示必须是原生那条『流式输出中...』本身，一字不改，"
                        "不能替换成另编的提示语")
        self.assertTrue(result["placeholder_has_stop_button"],
                        "占位提示要带着停止按钮到 Telegram——这正是用户要的那条『带暂停按钮的提示』")
        self.assertTrue(result["agent_status_relayed"],
                        "Agent 轮次状态行必须同步（旧实现按类型整类丢弃）")
        self.assertTrue(result["tool_card_relayed"],
                        "工具/命令返回卡片必须同步（旧实现按类型整类丢弃）")
        self.assertTrue(result["edit_applied"], "流式编辑要落到 Telegram 上")
        self.assertTrue(result["edit_targets_real_id"],
                        "编辑必须打到首次发送时拿到的真实 message_id——映射断了就会"
                        "变成每次编辑新发一条（历史上的无限刷屏）")
        self.assertTrue(result["placeholder_deleted"],
                        "占位消息跑完要被删掉，和原生 Telegram 会话一样自己消失")

    def test_relay_ops_are_purged_after_replay(self):
        """操作流是一次性的：回放完就该从表里清掉，不能无限堆积。"""
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio
from xgent_app import cli_bridge

class _FakeBot:
    async def send_message(self, chat_id=None, text="", reply_markup=None, **kwargs):
        class _M:
            message_id = 1
        return _M()

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    ns["_web_real_bot"] = _FakeBot()
    ns["_web_external_outbox"] = ns["WebOutbox"]()

    task = asyncio.get_running_loop().create_task(ns["_web_external_record_watcher"]())
    await asyncio.sleep(0.5)

    cli_bridge.configure_relay(ns["BotConfig"].DB_FILE, 1, "sess-purge")
    bot = cli_bridge.CliBot(1)
    for i in range(20):
        await bot.send_message(1, f"第 {i} 条")
    cli_bridge.close_relay()

    before = len(await db.fetch_relay_ops(0))
    await asyncio.sleep(2.0)
    task.cancel()
    after = len(await db.fetch_relay_ops(0))
    print(json.dumps({"before": before, "after": after}))
    await db.close()

asyncio.run(main())
""")
        self.assertGreaterEqual(result["before"], 20, "CLI 的 bot 调用要按序落进中继表")
        self.assertEqual(0, result["after"], "回放过的操作必须清掉，否则这张表会无限增长")


class ServerSideCliMirrorTests(SectionsProbeMixin, unittest.TestCase):
    """回放放在服务端（idle.py）：CLI 只写中继表，服务端用自己那条健康的
    PTB 连接回放。CLI 进程自己连 Telegram 是明确否掉的（裸连接逐条握手，
    网络差时拖到 30 秒超时，且进程一退未发完的直接被砍）。"""

    def test_tg_channel_gate_and_relay_switch_are_independent(self):
        """Telegram 通道开关与回放循环总开关必须分开。

        用"有没有真实 bot"决定整个回放循环跑不跑，会让纯 Web 模式的用户在
        CLI 里说的话连网页都到不了。
        """
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import os

ns["_web_real_bot"] = None
no_bot = ns["_web_external_tg_mirror_enabled"]()      # 没有真实 bot：TG 不发
relay_no_bot = ns["_cli_relay_enabled"]()             # 但回放循环照跑（网页要收）
ns["_web_real_bot"] = object()
with_bot = ns["_web_external_tg_mirror_enabled"]()    # 有 bot：TG 发
os.environ["XGENT_CLI_NO_TG_MIRROR"] = "1"
switched_off = ns["_web_external_tg_mirror_enabled"]()
relay_off = ns["_cli_relay_enabled"]()
del os.environ["XGENT_CLI_NO_TG_MIRROR"]
print(json.dumps({
    "no_bot": no_bot, "relay_no_bot": relay_no_bot, "with_bot": with_bot,
    "switched_off": switched_off, "relay_off": relay_off,
}))
""")
        self.assertFalse(result["no_bot"], "纯 Web 模式（无真实 bot）不该往 TG 发")
        self.assertTrue(result["relay_no_bot"],
                        "纯 Web 模式下回放循环仍要跑——否则 CLI 里说的话连网页都到不了"
                        "（MirrorBot 原生支持 real_bot=None，退化成纯网页输出）")
        self.assertTrue(result["with_bot"])
        self.assertFalse(result["switched_off"], "XGENT_CLI_NO_TG_MIRROR=1 要能关掉 TG 通道")
        self.assertFalse(result["relay_off"], "该环境变量是总开关，连回放循环一起关")

    def test_web_only_mode_still_relays_to_web(self):
        """纯 Web 模式（没有真实 bot）：CLI 的操作流仍要变成网页帧。"""
        result = self.run_probe(self.SECTIONS_PREAMBLE + """
import asyncio
from xgent_app import cli_bridge

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    ns["_web_real_bot"] = None                     # 纯 Web 模式
    outbox = ns["WebOutbox"]()
    ns["_web_external_outbox"] = outbox
    sub = outbox.subscribe()

    cli_bridge.configure_relay(ns["BotConfig"].DB_FILE, 1, "sess-webonly")
    cli_bridge.relay_user_message("网页应该看到这句")
    bot = cli_bridge.CliBot(1)
    await bot.send_message(1, "AI 的回复")
    cli_bridge.close_relay()

    task = asyncio.get_running_loop().create_task(ns["_web_external_record_watcher"]())
    await asyncio.sleep(2.0)
    task.cancel()

    frames = []
    while True:
        f = sub.get(timeout=0.01)
        if f is None:
            break
        frames.append(f)
    print(json.dumps({
        "user_frame": any(f["type"] == "user_message" and "网页应该看到这句" in f.get("text", "")
                          for f in frames),
        "ai_frame": any(f["type"] == "message" and "AI 的回复" in f.get("text", "")
                        for f in frames),
    }))
    await db.close()

asyncio.run(main())
""")
        self.assertTrue(result["user_frame"], "纯 Web 模式下用户消息要推成网页用户气泡帧")
        self.assertTrue(result["ai_frame"], "纯 Web 模式下 AI 消息要推成网页帧")

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
    # CLI 进程环境里没有任何托管标记——这正是要修的场景。也要清 systemd 的
    # INVOCATION_ID/JOURNAL_STREAM：GitHub Actions 的 ubuntu-latest runner
    # 本身跑在 systemd 单元里，这两个变量会从 runner 进程一路继承到测试
    # 子进程，本地跑（非 systemd 环境）测不出来，CI 上却让 is_systemd 提前
    # 为真，restart_via_install 分支被跳过，install.sh 从未被调用。
    for key in ("PM2_HOME", "pm_id", "PM2_USAGE", "INVOCATION_ID", "JOURNAL_STREAM"):
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
        self.assertTrue(result["status_shown"], "落库的轮次状态行要原样显示（与 bot 界面逐字一致）")
        self.assertTrue(result["status_not_in_ctx"], "轮次状态是 UI 信息，不进 AI 上下文")
        self.assertTrue(result["cmd_prefixed_in_ctx"], "命令记录在 AI 上下文里要带 [命令] 前缀")
        self.assertTrue(result["no_raw_fence"], "刷新后的显示里不允许出现协议围栏原文")
        self.assertTrue(result["no_media_prompt"], "媒体生成的完整提示词不该出现在显示里")
        self.assertTrue(result["token_gray"], "token 统计行要降级成 system 灰条")
        self.assertTrue(result["ai_reply_html"], "AI 正文仍走 Markdown->HTML，不受影响")


class CliUserEchoTests(ProbeMixin, unittest.TestCase):
    """CLI 用户消息成块回显（user 样式的框）。TG 镜像见 ServerSideCliMirrorTests。"""

    def test_everything_the_user_types_gets_the_same_box(self):
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
    cmd_out = stream.getvalue()[len(block_out):]
    print(json.dumps({
        "block_has_marker": "❯ User" in block_out,
        "block_has_text": "你好，洛溪" in block_out,
        "block_is_boxed": "╭" in block_out and "╰" in block_out,
        "cmd_has_marker": "❯ User" in cmd_out,
        "cmd_has_text": "/start" in cmd_out,
        "cmd_is_boxed": "╭" in cmd_out and "╰" in cmd_out,
    }))
    await xgent_cli._shutdown_runtime()

asyncio.run(main())
""")
        self.assertTrue(result["block_has_marker"], "对话消息要有 ❯ User 用户块")
        self.assertTrue(result["block_has_text"])
        self.assertTrue(result["block_is_boxed"], "用户消息要围在框里")
        # 命令也是"用户敲进去的东西"，同款框——回卷里我说的话长得一致，
        # 才看得出"我说了什么、它回了什么"这条交替的线。
        self.assertTrue(result["cmd_has_marker"], "命令回显也走用户块")
        self.assertTrue(result["cmd_has_text"])
        self.assertTrue(result["cmd_is_boxed"], "命令回显同样要围在框里")


if __name__ == "__main__":
    unittest.main()
