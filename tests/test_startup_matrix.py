"""启动矩阵：任一端出问题都不许拖垮其他端。

这是两台 VPS 上那次故障的回归测试。故障链条是：

  main.py 裸调 app.run_polling() → PTB 的 Application.__run 先跑
  _bootstrap_initialize（内含 Bot.initialize() 的 get_me() 真实网络请求，
  bootstrap_retries 默认 0 == 不重试）→ 再跑 post_init。而 Web 服务偏偏挂在
  post_init（旧 setup_bot_commands）里。于是 Telegram 不通时 get_me 抛
  NetworkError，穿出 run_polling，main 打一行 Fatal Error 后 sys.exit(1)——
  **Web 的监听端口从来没有 bind 过**，nginx 502，PM2 无限重启。CLI 是独立
  进程、不碰 Telegram，所以只有它还能用。

现在 Telegram 只是 runtime.run_app() 下的一个受监督组件。下面的用例逐条钉住
"它挂了别人照跑"。

sections 靠共享命名空间加载，所以都用带环境变量的子进程探针跑（同
test_external_sync 的做法）。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PREAMBLE = """
import json, socket, sys
sys.path.insert(0, %r)
from xgent_app.bootstrap import load_sections
ns = {"__file__": "xgent_server.py"}
load_sections(ns)

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

async def prepare_web():
    \"\"\"给探针备好"Web 能起来"的最小条件：密码 + 开关 + 空闲端口。\"\"\"
    await ns["UserDataManager"].init()
    await ns["persist_web_password"]("probe-password-123")
    ns["UserDataManager"].set("web_enabled", True)
    ns["UserDataManager"].set("web_port", free_port())
""" % str(ROOT)


class StartupMatrixTests(unittest.TestCase):
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
                [sys.executable, "-c", PREAMBLE + code],
                cwd=cwd, env=env, text=True, encoding="utf-8",
                capture_output=True, timeout=180,
            )
        if result.returncode != 0:
            self.fail(f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_web_and_cli_sync_survive_unreachable_telegram(self):
        """TG 完全不通（模拟 IPv4 全超时）：网页照样起、CLI 回放照样跑。"""
        result = self.run_probe("""
import asyncio
import telegram.ext
from telegram.error import NetworkError

async def _unreachable(self):
    raise NetworkError("simulated: all IPv4 routes to Telegram time out")

telegram.ext.Application.initialize = _unreachable

async def main():
    await prepare_web()
    task = asyncio.get_running_loop().create_task(ns["run_app"]())
    # 给足时间：Web 组件要 bind、trigger 要恢复、TG 要失败并进入退避重试
    await asyncio.sleep(4.0)
    health = ns["component_health"]()
    watcher = ns["_web_external_watch_task"]
    payload = {
        "web_running": ns["is_web_chat_running"](),
        "web_state": health.get("web", {}).get("state"),
        "telegram_state": health.get("telegram", {}).get("state"),
        "telegram_has_error": bool(health.get("telegram", {}).get("last_error")),
        "triggers_state": health.get("triggers", {}).get("state"),
        "cli_relay_state": health.get("cli_relay", {}).get("state"),
        "watcher_alive": watcher is not None and not watcher.done(),
        # 通道没就绪就不该登记 bot：否则每条 CLI 中继都会去打注定超时的请求
        "tg_channel_unregistered": ns["_web_real_bot"] is None,
        "tg_mirror_gate_closed": ns["_web_external_tg_mirror_enabled"]() is False,
        "process_alive": not task.done(),
    }
    ns["request_app_stop"]()
    payload["exit_code"] = await asyncio.wait_for(task, timeout=30)
    print(json.dumps(payload))

asyncio.run(main())
""")
        self.assertTrue(result["web_running"],
                        "TG 不通时网页必须照样 bind——这正是 502 的根源")
        self.assertEqual("up", result["web_state"])
        self.assertEqual("degraded", result["telegram_state"],
                         "TG 应停在 degraded 并持续重试，而不是带走整个进程")
        self.assertTrue(result["telegram_has_error"], "degraded 要带上脱敏后的原因")
        self.assertEqual("up", result["triggers_state"],
                         "trigger 调度器原先埋在 post_init 里，TG 不通就静默全停")
        self.assertEqual("up", result["cli_relay_state"])
        self.assertTrue(result["watcher_alive"], "CLI 跨端回放器必须在跑")
        self.assertTrue(result["tg_channel_unregistered"],
                        "get_me 没成功前不能登记 bot 引用——否则 CLI 中继会被"
                        "注定超时的 TG 请求拖慢")
        self.assertTrue(result["tg_mirror_gate_closed"])
        self.assertTrue(result["process_alive"], "进程不许因为 TG 连不上而退出")
        self.assertEqual(0, result["exit_code"])

    def test_invalid_token_still_exits_78(self):
        """Token 无效不是网络问题，重试无意义：必须以 78 退出。

        PM2 的 --stop-exit-codes 78（install.sh）认这个码后不再无限重启，
        换成别的码会变成刷屏重启。
        """
        result = self.run_probe("""
import asyncio
import telegram.ext
from telegram.error import InvalidToken

async def _bad_token(self):
    raise InvalidToken("simulated bad token")

telegram.ext.Application.initialize = _bad_token

async def main():
    await prepare_web()
    code = await asyncio.wait_for(ns["run_app"](), timeout=60)
    print(json.dumps({"exit_code": code}))

asyncio.run(main())
""")
        self.assertEqual(78, result["exit_code"])

    def test_no_token_uses_the_same_startup_path(self):
        """没有 BOT_TOKEN：走同一条 run_app，不再有第二条 run_web_only_main。"""
        result = self.run_probe("""
import asyncio

async def main():
    await prepare_web()
    # 没有 token 时 Web 是唯一入口，应被强制启动（不看 web_enabled 开关）
    ns["UserDataManager"].set("web_enabled", False)
    task = asyncio.get_running_loop().create_task(ns["run_app"]())
    await asyncio.sleep(3.0)
    health = ns["component_health"]()
    payload = {
        "telegram_state": health.get("telegram", {}).get("state"),
        "web_state": health.get("web", {}).get("state"),
        "web_running": ns["is_web_chat_running"](),
        "legacy_entry_gone": "run_web_only_main" not in ns,
        "legacy_shutdown_gone": "on_shutdown_web_only" not in ns,
        "legacy_post_init_gone": "setup_bot_commands" not in ns,
    }
    ns["request_app_stop"]()
    payload["exit_code"] = await asyncio.wait_for(task, timeout=30)
    print(json.dumps(payload))

asyncio.run(main())
""", extra_env={"BOT_TOKEN": ""})
        self.assertEqual("disabled", result["telegram_state"])
        self.assertEqual("up", result["web_state"])
        self.assertTrue(result["web_running"],
                        "纯 Web 部署里 Web 是唯一入口，必须强制启动")
        self.assertTrue(result["legacy_entry_gone"],
                        "run_web_only_main 应已被合并进 run_app，不留第二条启动路径")
        self.assertTrue(result["legacy_shutdown_gone"])
        self.assertTrue(result["legacy_post_init_gone"])
        self.assertEqual(0, result["exit_code"])

    def test_no_entrypoint_at_all_fails_loudly(self):
        """既没有 token、Web 也起不来（没设密码）：直接以 1 退出。

        不能留一个什么都不做的进程——PM2 会把它显示成 online，用户以为在跑。
        """
        result = self.run_probe("""
import asyncio

async def main():
    await ns["UserDataManager"].init()          # 刻意不设密码
    code = await asyncio.wait_for(ns["run_app"](), timeout=60)
    print(json.dumps({"exit_code": code, "web_running": ns["is_web_chat_running"]()}))

asyncio.run(main())
""", extra_env={"BOT_TOKEN": ""})
        self.assertEqual(1, result["exit_code"])
        self.assertFalse(result["web_running"])

    def test_shutdown_closes_database_exactly_once(self):
        """只有一条停机路径：数据库连接关一次，不多不少。"""
        result = self.run_probe("""
import asyncio
import telegram.ext
from telegram.error import NetworkError

async def _unreachable(self):
    raise NetworkError("simulated network down")

telegram.ext.Application.initialize = _unreachable

async def main():
    await prepare_web()
    db = await ns["BotMemoryDB"].get_instance()
    closes = []
    original = db.close
    async def counted_close():
        closes.append(1)
        await original()
    db.close = counted_close

    task = asyncio.get_running_loop().create_task(ns["run_app"]())
    await asyncio.sleep(2.0)
    ns["request_app_stop"]()
    code = await asyncio.wait_for(task, timeout=30)
    print(json.dumps({"closes": len(closes), "exit_code": code}))

asyncio.run(main())
""")
        self.assertEqual(1, result["closes"], "数据库只能被关闭一次")
        self.assertEqual(0, result["exit_code"])


if __name__ == "__main__":
    unittest.main()
