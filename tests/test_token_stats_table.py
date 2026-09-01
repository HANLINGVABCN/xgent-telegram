"""token 用量统计独立表（token_usage_stats）的行为测试。

背景：清空对话记忆（clear_all_conversation_memory 全表删 global_messages）曾把
token 用量统计一起删掉。方案：统计迁入独立表 + record_token_usage 双写 +
_load_token_records 改读新表。覆盖：
  T1 双写：用量行同时落 global_messages（聊天显示）与 token_usage_stats（统计）；
  T2 事故场景：clear_all_conversation_memory 后统计行存活、/stats 读路径正常；
  T3 迁移幂等：global_messages 里的历史用量行启动时搬入新表，重复跑不重复；
  T4 旧格式：无结构化 metadata 的历史行不迁移不统计。

sections 靠共享命名空间加载，用带环境变量的子进程探针跑（同 test_external_sync）。
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
""" % str(ROOT)


class ProbeMixin:
    def run_probe(self, code: str):
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
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


SECTIONS_PREAMBLE = """
from xgent_app.bootstrap import load_sections
ns = {"__file__": "xgent_server.py"}
load_sections(ns)
"""

USAGE = {"input_tokens": 100, "output_tokens": 50, "cached_tokens": 10,
         "reasoning_tokens": 5, "total_tokens": 150}


class TokenStatsTableTests(ProbeMixin, unittest.TestCase):
    def test_t1_dual_write(self):
        """T1: record_token_usage 双写——新表有结构化统计行，老表保留显示行。"""
        result = self.run_probe(SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    await ns["GlobalRecorder"].record_token_usage(
        "↑ 100 tokens ↓ 50 tokens", 1, usage=%r, model="test-model")
    stats = await db.get_token_stats()
    display = await db.get_global_messages(
        1000, include_types=[ns["MessageType"].TOKEN_USAGE])
    await db.close()  # aiosqlite 工作线程非守护，不关进程退不出去
    print(json.dumps({
        "stats_rows": len(stats),
        "display_rows": len(display),
        "stat": stats[0] if stats else None,
    }))

asyncio.run(main())
""" % USAGE)
        self.assertEqual(result["stats_rows"], 1)
        self.assertEqual(result["display_rows"], 1)
        stat = result["stat"]
        self.assertEqual(stat["model"], "test-model")
        self.assertEqual(stat["input_tokens"], 100)
        self.assertEqual(stat["output_tokens"], 50)
        self.assertEqual(stat["cached_tokens"], 10)
        self.assertEqual(stat["reasoning_tokens"], 5)
        self.assertEqual(stat["total_tokens"], 150)

    def test_t2_survives_memory_clear(self):
        """T2: 事故场景——清空全局记忆后，独立表统计存活，/stats 读路径正常。"""
        result = self.run_probe(SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    await ns["GlobalRecorder"].record_token_usage(
        "↑ 100 tokens ↓ 50 tokens", 1, usage=%r, model="test-model")
    await db.clear_all_conversation_memory()
    out = {
        "stats_after_clear": await db.get_token_stats(),
        "records_after_clear": await ns["_load_token_records"](),
        "display_rows_after_clear": len(await db.get_global_messages(
            1000, include_types=[ns["MessageType"].TOKEN_USAGE])),
    }
    await db.close()
    print(json.dumps(out))

asyncio.run(main())
""" % USAGE)
        self.assertEqual(len(result["stats_after_clear"]), 1)
        self.assertEqual(result["display_rows_after_clear"], 0)  # 老表显示行随对话清掉
        records = result["records_after_clear"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["model"], "test-model")
        self.assertEqual(records[0]["input"], 100)
        self.assertEqual(records[0]["output"], 50)
        self.assertEqual(records[0]["cached"], 10)
        self.assertEqual(records[0]["reasoning"], 5)
        self.assertEqual(records[0]["total"], 150)

    def test_t3_migration_idempotent(self):
        """T3: 历史用量行（仅存 global_messages）启动迁移入新表，重跑不重复。"""
        result = self.run_probe(SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    GR = ns["GlobalRecorder"]
    MT = ns["MessageType"]
    # 模拟旧版本数据：record() 不双写，只有 global_messages 行
    rid = await GR.record(
        msg_type=MT.TOKEN_USAGE, role="assistant", content="old row",
        metadata={"model": "legacy-model",
                  "usage": {"input_tokens": 7, "total_tokens": 7}})
    out = {"rowid_returned": isinstance(rid, int)}

    db._initialized = False
    await db._init_db()  # 模拟重启触发迁移
    first = await db.get_token_stats()
    out["after_first"] = [(r["model"], r["total_tokens"]) for r in first]

    db._initialized = False
    await db._init_db()  # 再重启一次
    second = await db.get_token_stats()
    out["after_second"] = [(r["model"], r["total_tokens"]) for r in second]
    await db.close()
    print(json.dumps(out))

asyncio.run(main())
""")
        self.assertTrue(result["rowid_returned"])
        self.assertEqual(result["after_first"], [["legacy-model", 7]])
        self.assertEqual(result["after_second"], [["legacy-model", 7]])  # 无重复行

    def test_t4_legacy_rows_without_metadata_skipped(self):
        """T4: 无结构化 metadata 的历史行不迁移（与旧 /stats 读路径语义一致）。"""
        result = self.run_probe(SECTIONS_PREAMBLE + """
import asyncio

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    GR = ns["GlobalRecorder"]
    MT = ns["MessageType"]
    await GR.record(msg_type=MT.TOKEN_USAGE, role="assistant",
                    content="no meta", metadata=None)
    db._initialized = False
    await db._init_db()
    stats = await db.get_token_stats()
    legacy_rows = await db.get_global_messages(
        1000, include_types=[MT.TOKEN_USAGE])
    await db.close()
    print(json.dumps({
        "stats_rows": len(stats),
        "legacy_display_rows": len(legacy_rows),
    }))

asyncio.run(main())
""")
        self.assertEqual(result["stats_rows"], 0)
        self.assertEqual(result["legacy_display_rows"], 1)  # 显示行照存，只是不进统计


if __name__ == "__main__":
    unittest.main()
