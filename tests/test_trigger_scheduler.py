"""trigger 三端互通的调度器解耦测试。

背景（v1 限制）：SelfTriggerManager 的调度器寄生在 PTB 的
application.job_queue.scheduler 上——纯 Web 模式（无 PTB）无法创建定时任务，
CLI 进程里创建会 RuntimeError / 条件任务静默烂在"未投递"状态。方案：
自持 AsyncIOScheduler + 拾取扫描对账 + 投递走 MirrorBot 双通道。覆盖：
  T1 自持调度器：startup(None)（纯 Web 语义）起调度器与拾取扫描，shutdown 收干净；
  T2 CLI 登记：无调度器进程里 register 只落库不激活，返回接管文案；
  T3 对账双向：拾取外部进程登记的任务；DB 失效的任务摘除调度 job；
  T4 投递 bot：build_trigger_delivery_bot 在 PTB/纯 Web 两态构造正确。

sections 靠共享命名空间加载，用带环境变量的子进程探针跑（同 test_token_stats_table）。
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

YAML_BODY = """
task: probe task
schedule:
  at: 2030-01-01 00:00:00
command: echo hello
"""


class TriggerSchedulerTests(ProbeMixin, unittest.TestCase):
    def test_t1_self_owned_scheduler(self):
        """T1: startup(None) 起自持调度器 + 拾取扫描 job；shutdown 收干净。"""
        result = self.run_probe(SECTIONS_PREAMBLE + """
import asyncio

async def main():
    mgr = ns["SelfTriggerManager"]
    await ns["UserDataManager"].init()
    await ns["BotMemoryDB"].get_instance()
    await mgr.startup(None)
    scheduler = mgr._scheduler
    scan_jobs = [j for j in scheduler.get_jobs()
                 if j.id == 'self-trigger:pickup-scan']
    await mgr.shutdown()
    db = await ns["BotMemoryDB"].get_instance()
    await db.close()
    print(json.dumps({
        "scheduler_up": scheduler is not None,
        "scan_job_count": len(scan_jobs),
        "scheduler_after_shutdown": mgr._scheduler is None,
        "started_after_shutdown": mgr._started,
    }))

asyncio.run(main())
""")
        self.assertTrue(result["scheduler_up"])
        self.assertEqual(result["scan_job_count"], 1)
        self.assertTrue(result["scheduler_after_shutdown"])
        self.assertFalse(result["started_after_shutdown"])

    def test_t2_cli_register_no_activation(self):
        """T2: 无调度器进程（CLI 语义）register 只落库，返回接管文案。"""
        result = self.run_probe(SECTIONS_PREAMBLE + """
import asyncio
from types import SimpleNamespace

async def main():
    mgr = ns["SelfTriggerManager"]
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    # CLI 进程从不 startup：_scheduler 保持 None
    assert mgr._scheduler is None
    text = await mgr.register(
        %r, SimpleNamespace(), 1, "conv",
        "origin user text", "origin assistant text")
    rows = await db.list_trigger_tasks(active_only=True)
    await db.close()
    print(json.dumps({
        "notice": text,
        "db_rows": len(rows),
        "runtime_tasks": len(mgr._runtime_tasks),
        "scheduler": mgr._scheduler is None,
    }))

asyncio.run(main())
""" % YAML_BODY)
        self.assertIn("已登记触发任务", result["notice"])
        self.assertIn("接管执行", result["notice"])
        self.assertEqual(result["db_rows"], 1)
        self.assertEqual(result["runtime_tasks"], 0)
        self.assertTrue(result["scheduler"])

    def test_t3_pickup_reconciliation(self):
        """T3: 拾取外部登记的任务；DB 失效任务摘除调度 job。"""
        result = self.run_probe(SECTIONS_PREAMBLE + """
import asyncio

async def main():
    mgr = ns["SelfTriggerManager"]
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    await mgr.startup(None)

    # 模拟另一个进程（CLI）登记的任务：直接写库，不经 register
    task = {
        'id': 'trg_probe_pickup',
        'chat_id': 1,
        'conversation_id': 'conv',
        'command': 'echo hello',
        'summary': 'probe task',
        'schedule_type': 'once',
        'schedule_expr': '2030-01-01 00:00:00',
        'timezone': 'Asia/Shanghai',
        'next_run_at': 1893456000.0,
        'condition_expr': None,
        'repeat': 0,
        'status': 'scheduled',
        'origin_user_text': 'origin user',
        'origin_assistant_text': 'origin assistant',
        'created_at': 1700000000.0,
        'updated_at': 1700000000.0,
    }
    await db.create_trigger_task(task)
    before = [j.id for j in mgr._scheduler.get_jobs()
              if j.id == 'self-trigger:trg_probe_pickup']
    await mgr._pickup_scan()
    picked = [j.id for j in mgr._scheduler.get_jobs()
              if j.id == 'self-trigger:trg_probe_pickup']

    # CLI 里 kill 的场景：DB 标记取消后，扫描应摘除 job
    await db.cancel_trigger_tasks('trg_probe_pickup')
    await mgr._pickup_scan()
    removed = [j.id for j in mgr._scheduler.get_jobs()
               if j.id == 'self-trigger:trg_probe_pickup']

    await mgr.shutdown()
    await db.close()
    print(json.dumps({
        "before": len(before),
        "picked": len(picked),
        "removed": len(removed),
    }))

asyncio.run(main())
""")
        self.assertEqual(result["before"], 0)
        self.assertEqual(result["picked"], 1)
        self.assertEqual(result["removed"], 0)

    def test_t4_delivery_bot(self):
        """T4: build_trigger_delivery_bot 在 PTB / 纯 Web 两态构造正确。"""
        result = self.run_probe(SECTIONS_PREAMBLE + """
import asyncio
from types import SimpleNamespace

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    outbox = ns["_web_external_outbox"]
    fake_bot = SimpleNamespace(name="fake")
    ns["_web_real_bot"] = fake_bot
    bot_ptb = ns["build_trigger_delivery_bot"](42)
    ns["_web_real_bot"] = None
    bot_web = ns["build_trigger_delivery_bot"](42)
    ns["_web_real_bot"] = None
    await db.close()
    print(json.dumps({
        "ptb_type": type(bot_ptb).__name__,
        "ptb_real": bot_ptb.real_bot is fake_bot,
        "ptb_outbox": bot_ptb.outbox is outbox,
        "ptb_chat": bot_ptb.chat_id == 42,
        "web_type": type(bot_web).__name__,
        "web_real_none": bot_web.real_bot is None,
    }))

asyncio.run(main())
""")
        self.assertEqual(result["ptb_type"], "MirrorBot")
        self.assertTrue(result["ptb_real"])
        self.assertTrue(result["ptb_outbox"])
        self.assertTrue(result["ptb_chat"])
        self.assertEqual(result["web_type"], "MirrorBot")
        self.assertTrue(result["web_real_none"])


if __name__ == '__main__':
    unittest.main()
