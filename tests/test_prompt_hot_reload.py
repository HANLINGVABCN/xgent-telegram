"""提示词实时载入回归：直接改 prompts/ 文件后，PromptFileManager.get 必须即时
生效，不需要重启或手动「从文件重载」。记忆与技能本就每次读盘，这里锁住提示词这条
之前唯一走启动期缓存的路径，并覆盖两条边界：读盘失败保留旧值、DB 覆盖值不遮蔽文件。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PromptHotReloadTests(unittest.TestCase):
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
                cwd=cwd, env=env, text=True, encoding="utf-8",
                capture_output=True, timeout=60,
            )
        if result.returncode != 0:
            self.fail(f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_prompt_file_edits_go_live_without_reload(self):
        data = self.run_probe(r'''
import json, os, tempfile, time
import xgent_server as bot

PFM = bot.PromptFileManager
with tempfile.TemporaryDirectory() as tmp:
    # 把管理器指向隔离的临时目录，绝不碰真实 prompts/
    PFM.PROMPTS_DIR = tmp
    PFM.FILES = {"agent_prompt_addon": "agent_addon.txt",
                 "assistant_prompt": "main.txt"}
    PFM._cache.clear(); PFM._stat_sigs.clear()

    addon = os.path.join(tmp, "agent_addon.txt")
    with open(addon, "w", encoding="utf-8") as f:
        f.write("V1")
    first = PFM.get("agent_prompt_addon")

    # 直接改文件（模拟用户手改），不调用 reload_all
    time.sleep(0.02)
    with open(addon, "w", encoding="utf-8") as f:
        f.write("V2-HOTRELOAD")
    live = PFM.get("agent_prompt_addon")

    # 文件读不到（删除/占用）时保留上一份可用值，不清空运行中的提示词
    os.remove(addon)
    kept = PFM.get("agent_prompt_addon")

    # get_runtime_prompt 文件优先：即便存在 DB 覆盖值，非空文件也要赢
    main = os.path.join(tmp, "main.txt")
    with open(main, "w", encoding="utf-8") as f:
        f.write("FROM_FILE")
    bot.UserDataManager.set("assistant_prompt", "DB_OVERRIDE")
    file_wins = bot.get_runtime_prompt("assistant_prompt")

    # 文件被清空时才回退到 DB 覆盖值，避免核心提示词整段消失
    with open(main, "w", encoding="utf-8") as f:
        f.write("   ")
    empty_falls_back = bot.get_runtime_prompt("assistant_prompt")

print(json.dumps({
    "first": first, "live": live, "kept": kept,
    "file_wins": file_wins, "empty_falls_back": empty_falls_back,
}, ensure_ascii=False))
''')
        self.assertEqual("V1", data["first"])
        self.assertEqual("V2-HOTRELOAD", data["live"],
                         "直接改文件后 get 必须即时返回新内容，无需 reload_all")
        self.assertEqual("V2-HOTRELOAD", data["kept"],
                         "文件读不到时必须保留上一份可用缓存")
        self.assertEqual("FROM_FILE", data["file_wins"],
                         "非空文件必须盖过 DB 覆盖值（文件是唯一事实来源）")
        self.assertEqual("DB_OVERRIDE", data["empty_falls_back"],
                         "文件被清空时才回退 DB 覆盖值")


if __name__ == "__main__":
    unittest.main()
