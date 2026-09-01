import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelFetchStatusTests(unittest.TestCase):
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
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_saved_models_view_prefix_icons_and_legend(self):
        data = self.run_probe(r'''
import json
import time
import xgent_server as bot

bot.UserDataManager._data['providers'] = {
    'p1': {'models': ['alive', 'ghost'], 'base_url': 'https://x/v1', 'api_key': 'k', 'api_format': 'openai'},
}
bot.UserDataManager._data['temp_page'] = 1
bot.UserDataManager._data['temp_saved_filter'] = None
bot.UserDataManager._data['temp_viewing_prov'] = 'p1'

# 未检测过：不显示图标，也不显示图例
title_none, kb_none = bot.build_saved_models_view('p1')
no_record_prefix = bot.make_model_status_fn('p1')

# 记录一次联网拉取结果：alive 存在，ghost 不存在
bot.UserDataManager._data['model_fetch_status'] = {
    'p1': {'models': ['alive'], 'ts': int(time.time())},
}
title, kb = bot.build_saved_models_view('p1')
prefix = bot.make_model_status_fn('p1')
first_row_texts = [row[0].text for row in kb.inline_keyboard[:2]]

detail_text, _ = bot.build_model_detail_menu('p1', 'ghost')

print(json.dumps({
    "no_record_prefix_none": no_record_prefix is None,
    "title_no_legend_before": '🟢' not in title_none,
    "prefix_alive": prefix('alive'),
    "prefix_ghost": prefix('ghost'),
    "title_has_legend": '🟢 有效' in title and '🔴 失效' in title and '上次检测' in title,
    "button_texts": first_row_texts,
    "detail_has_fetch_line": '联网检测' in detail_text and '🔴' in detail_text,
}, ensure_ascii=False))
''')
        self.assertTrue(data["no_record_prefix_none"])
        self.assertTrue(data["title_no_legend_before"])
        self.assertEqual("🟢", data["prefix_alive"])
        self.assertEqual("🔴", data["prefix_ghost"])
        self.assertTrue(data["title_has_legend"])
        self.assertEqual("🟢 alive", data["button_texts"][0])
        self.assertEqual("🔴 ghost", data["button_texts"][1])
        self.assertTrue(data["detail_has_fetch_line"])

    def test_fetch_status_persists_across_restart(self):
        data = self.run_probe(r'''
import asyncio
import json
import time
import xgent_server as bot

async def main():
    await bot.UserDataManager.init()
    await bot.UserDataManager.save_config('model_fetch_status', {
        'p1': {'models': ['m1'], 'ts': int(time.time())},
    })
    # 模拟重启：清空内存态后重新从数据库加载
    bot.UserDataManager._initialized = False
    bot.UserDataManager._data = {}
    bot.UserDataManager._db = None
    await bot.UserDataManager.init()
    record = bot.get_provider_fetch_record('p1')
    prefix = bot.make_model_status_fn('p1')
    # aiosqlite 连接线程非 daemon：不关掉，探针进程退出时会卡在 threading 收尾
    db = await bot.BotMemoryDB.get_instance()
    await db.close()
    print(json.dumps({
        "persisted": record is not None and 'm1' in record.get('models', []),
        "prefix": prefix('m1') if prefix else None,
    }, ensure_ascii=False))

asyncio.run(main())
''')
        self.assertTrue(data["persisted"])
        self.assertEqual("🟢", data["prefix"])

    def test_web_model_options_carry_status_icons(self):
        data = self.run_probe(r'''
import asyncio
import json
import time
import xgent_server as bot

async def main():
    await bot.UserDataManager.init()
    bot.UserDataManager._data['providers'] = {
        'p1': {'models': ['alive', 'ghost'], 'base_url': 'https://x/v1', 'api_key': 'k', 'api_format': 'openai'},
        'p2': {'models': ['unknown'], 'base_url': 'https://y/v1', 'api_key': 'k', 'api_format': 'openai'},
    }
    bot.UserDataManager._data['model_fetch_status'] = {
        'p1': {'models': ['alive'], 'ts': int(time.time())},
    }
    settings = await bot._web_read_settings()
    labels = {opt['value']: opt['label'] for opt in settings['options']['chat_model']}
    # aiosqlite 连接线程非 daemon：不关掉，探针进程退出时会卡在 threading 收尾
    db = await bot.BotMemoryDB.get_instance()
    await db.close()
    print(json.dumps({
        "alive": labels.get('p1|alive'),
        "ghost": labels.get('p1|ghost'),
        "unknown": labels.get('p2|unknown'),
    }, ensure_ascii=False))

asyncio.run(main())
''')
        self.assertEqual("🟢 p1 / alive", data["alive"])
        self.assertEqual("🔴 p1 / ghost", data["ghost"])
        self.assertEqual("p2 / unknown", data["unknown"])


if __name__ == "__main__":
    unittest.main()
