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
            env["TELEGRAM_AI_BOT_TRACE_LOG_FILE"] = str(Path(cwd) / "bot_full_trace.log")
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

    def test_import_and_core_symbols(self):
        output = self.run_probe(r'''
import json
import bot_server as bot
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

    def test_shared_http_client_and_database_connection_reuse(self):
        output = self.run_probe(r'''
import asyncio
import json
import tempfile
from pathlib import Path
import bot_server as bot

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
import bot_server as bot

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
import bot_server as bot
sample = "before\n```run-x <<AGENT_END_0123456789ABCDEF\necho ok\nAGENT_END_0123456789ABCDEF\n```\nafter"
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


if __name__ == "__main__":
    unittest.main()
