import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeSmokeTests(unittest.TestCase):
    def run_probe(self, code: str):
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
        })
        with tempfile.TemporaryDirectory() as cwd:
            env["XGENT_TRACE_LOG_FILE"] = str(Path(cwd) / "xgent_full_trace.log")
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=60,
            )
        if result.returncode != 0:
            self.fail(f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result.stdout

    def test_legacy_entrypoint_shim(self):
        output = self.run_probe(r'''
import json
import bot_server as bot
print(json.dumps({
    "config": bot.BotConfig.AUTHORIZED_USER_ID,
    "sections": len(bot._SECTION_FILES),
}))
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(1, data["config"])
        self.assertGreaterEqual(data["sections"], 10)

    def test_import_and_core_symbols(self):
        output = self.run_probe(r'''
import json
import xgent_server as bot
print(json.dumps({
    "config": bot.BotConfig.AUTHORIZED_USER_ID,
    "agent": bot.AgentExecutor.__name__,
    "model": bot.ModelClient.__name__,
    "callback": bot.handle_button_click.__name__,
    "sections": len(bot._SECTION_FILES),
}))
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(1, data["config"])
        self.assertEqual("AgentExecutor", data["agent"])
        self.assertEqual("ModelClient", data["model"])
        self.assertEqual("handle_button_click", data["callback"])
        self.assertGreaterEqual(data["sections"], 10)

    def test_provider_config_accepts_legacy_format_and_exports_xgent_format(self):
        output = self.run_probe(r'''
import json
import xgent_server as bot
payload = {
    "format": "telegram-ai-bot-provider-config",
    "version": 1,
    "providers": {
        "legacy": {
            "base_url": "https://example.com/v1",
            "api_key": "secret",
            "models": ["model-a"],
            "api_format": "openai",
        }
    },
    "defaults": {},
}
providers, defaults = bot.parse_provider_config_import(
    json.dumps(payload).encode("utf-8")
)
print(json.dumps({
    "imported": sorted(providers),
    "defaults": defaults,
    "export_format": bot.PROVIDER_CONFIG_FORMAT,
}))
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(["legacy"], data["imported"])
        self.assertEqual({}, data["defaults"])
        self.assertEqual("xgent-telegram-provider-config", data["export_format"])

    def test_shared_http_client_and_database_connection_reuse(self):
        output = self.run_probe(r'''
import asyncio
import json
import tempfile
from pathlib import Path
import xgent_server as bot

async def main():
    http_client_a = await bot.ModelClient._get_http_client()
    http_client_b = await bot.ModelClient._get_http_client()
    http_same = http_client_a is http_client_b
    await bot.ModelClient.close_http_client()
    http_closed = http_client_a.is_closed

    with tempfile.TemporaryDirectory() as temp_dir:
        db = bot.BotMemoryDB(str(Path(temp_dir) / "memory.db"))
        connections = await asyncio.gather(*[db._get_conn() for _ in range(8)])
        db_same = all(connection is connections[0] for connection in connections)
        await db.close()
        db_closed = db._connection is None

    class FakePortal:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    fake_portal = FakePortal()
    bot.PortalManager._portals = {"test|key": {"client": fake_portal, "hash": "x"}}
    await bot.PortalManager.close_all()
    portal_closed = fake_portal.closed and not bot.PortalManager._portals

    print(json.dumps({
        "http_same": http_same,
        "http_closed": http_closed,
        "db_same": db_same,
        "db_closed": db_closed,
        "portal_closed": portal_closed,
    }))

asyncio.run(main())
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertTrue(data["http_same"])
        self.assertTrue(data["http_closed"])
        self.assertTrue(data["db_same"])
        self.assertTrue(data["db_closed"])
        self.assertTrue(data["portal_closed"])

    def test_trigger_delivery_claim_is_atomic(self):
        output = self.run_probe(r'''
import asyncio
import json
import tempfile
import time
from pathlib import Path
import xgent_server as bot

async def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        db = bot.BotMemoryDB(str(Path(temp_dir) / "memory.db"))
        await db._init_db()
        now = time.time()
        await db.create_trigger_task({
            "id": "trg_claim",
            "chat_id": 1,
            "conversation_id": "conv",
            "command": "echo ok",
            "summary": "claim test",
            "schedule_type": "delay",
            "schedule_expr": "1s",
            "timezone": "UTC",
            "next_run_at": now,
            "condition_expr": None,
            "repeat": False,
            "status": "running",
            "created_at": now,
            "updated_at": now,
        })
        run, created = await db.create_trigger_run("trg_claim", now, "test")
        await db.finish_trigger_run(run["run_id"], finished_at=now, status="completed")
        claims = await asyncio.gather(*[
            db.claim_trigger_run_delivery(run["run_id"], now + index)
            for index in range(8)
        ])
        stored = await db.get_trigger_run(run["run_id"])
        await db.close()
    print(json.dumps({
        "created": created,
        "claim_count": sum(bool(value) for value in claims),
        "claimed": stored["delivery_started_at"] is not None,
    }))

asyncio.run(main())
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertTrue(data["created"])
        self.assertEqual(1, data["claim_count"])
        self.assertTrue(data["claimed"])

    def test_protocol_and_normalization_behavior(self):
        output = self.run_probe(r'''
import json
import xgent_server as bot
sample = "before\n```run-x <<AGENT_BEGIN_0123456789AB\necho ok\nAGENT_END_0123456789AB\n```\nafter"
blocks = bot.AgentExecutor.extract_protocol_blocks(sample)
print(json.dumps({
    "bool_true": bot.normalize_bool("yes"),
    "bool_false": bot.normalize_bool("off", True),
    "timeout": bot.parse_timeout_seconds("15s", minimum=5, maximum=30),
    "block_type": blocks[0]["type"],
    "block_body": blocks[0]["body"],
    "stripped": bot.AgentExecutor.strip_protocol_blocks(sample),
}, ensure_ascii=False))
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertTrue(data["bool_true"])
        self.assertFalse(data["bool_false"])
        self.assertEqual(15, data["timeout"])
        self.assertEqual("run", data["block_type"])
        self.assertEqual("echo ok", data["block_body"])
        self.assertEqual("before\nafter", data["stripped"])

    def test_reply_context_prefix_behavior(self):
        output = self.run_probe(r'''
import json
from types import SimpleNamespace as NS
import xgent_server as bot

user = NS(full_name="张三", first_name="张三", title=None, username="zhang")
replied = NS(from_user=user, sender_chat=None, text="把服务器重启一下", caption=None)

plain = bot.build_reply_context_prefix(
    NS(reply_to_message=replied, external_reply=None, quote=None))

quoted = bot.build_reply_context_prefix(
    NS(reply_to_message=replied, external_reply=None, quote=NS(text="服务器")))

media = bot.build_reply_context_prefix(
    NS(reply_to_message=NS(from_user=user, sender_chat=None, text=None, caption=None,
                           photo=[NS(file_id="p1")], document=None, sticker=None),
       external_reply=None, quote=None))

doc = bot.build_reply_context_prefix(
    NS(reply_to_message=NS(from_user=user, sender_chat=None, text=None, caption=None,
                           photo=None,
                           document=NS(file_name="日志.txt"),
                           sticker=None),
       external_reply=None, quote=None))

none_case = bot.build_reply_context_prefix(
    NS(reply_to_message=None, external_reply=None, quote=None))

external = bot.build_reply_context_prefix(
    NS(reply_to_message=None,
       external_reply=NS(origin=NS(type="user", sender_user=NS(
           full_name="李四", first_name="李四", username="li"))),
       quote=None))

long_text = "长" * 600
truncated = bot.build_reply_context_prefix(
    NS(reply_to_message=NS(from_user=user, sender_chat=None, text=long_text, caption=None),
       external_reply=None, quote=None))

combined = bot.build_incoming_context_prefix(
    NS(reply_to_message=replied, external_reply=None, quote=None,
       forward_origin=NS(type="hidden_user", sender_user_name="老王"),
       forward_from=None, forward_from_chat=None))

print(json.dumps({
    "plain": plain, "quoted": quoted, "media": media, "doc": doc,
    "none": none_case, "external": external, "truncated": truncated,
    "combined": combined,
}, ensure_ascii=False))
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertIn("张三", data["plain"])
        self.assertIn("把服务器重启一下", data["plain"])
        self.assertIn("[引用片段：服务器]", data["quoted"])
        self.assertIn("[图片]", data["media"])
        self.assertIn("[文件：日志.txt]", data["doc"])
        self.assertEqual("", data["none"])
        self.assertIn("李四", data["external"])
        self.assertTrue(data["truncated"].endswith("…]"))
        self.assertIn("老王", data["combined"])
        self.assertIn("张三", data["combined"])


if __name__ == "__main__":
    unittest.main()
