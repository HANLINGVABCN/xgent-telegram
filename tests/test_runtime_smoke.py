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

    def test_protocol_and_normalization_behavior(self):
        output = self.run_probe(r'''
import json
import bot_server as bot
sample = "before\n```run-x\necho ok\n```\nafter"
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
