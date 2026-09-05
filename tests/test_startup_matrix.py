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

async def prepare_web(enabled=True):
    \"\"\"给探针备好"Web 能起来"的最小条件：密码 + 开关 + 空闲端口。

    一律走 save_config **落库**，不只是 set 进内存快照：配置对账任务
    （reconcile_web_config_once）以库为准，快照里的孤值会被它对掉——那正是它存在
    的意义（CLI / install.sh 是另外的进程，只能通过库说话）。

    enabled=None 表示"库里根本没有 web_enabled 这一行"，用来测迁移分支。
    \"\"\"
    await ns["UserDataManager"].init()
    await ns["persist_web_password"]("probe-password-123")
    await ns["UserDataManager"].save_config("web_port", free_port())
    if enabled is not None:
        await ns["UserDataManager"].save_config("web_enabled", enabled)
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
    health = ns["runtime_health"]()
    payload["health_telegram"] = health["components"]["telegram"]["state"]
    payload["health_web_running"] = health["surfaces"]["web_running"]
    payload["health_relay_running"] = health["surfaces"]["cli_relay_running"]
    payload["health_has_channel"] = "telegram" in health["channels"]
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
        self.assertEqual("degraded", result["health_telegram"],
                         "/api/health 必须能区分'进程死了'和'进程活着但 TG 断了'"
                         "——那次 502 缺的正是这个可观测性")
        self.assertTrue(result["health_web_running"])
        self.assertTrue(result["health_relay_running"])
        self.assertTrue(result["health_has_channel"], "出站通道要出现在健康快照里")
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
        """没有 BOT_TOKEN：走同一条 run_app，不再有第二条 run_web_only_main。

        顺带钉住迁移分支：库里从没写过 web_enabled 而 Web 又是唯一入口时，写一次
        True 并启动。以前这里是无条件 force（"没 token 就不看开关"），代价是用户
        显式关掉 Web 之后一重启又被无声打开——那等于这个开关不存在。
        """
        result = self.run_probe("""
import asyncio

async def main():
    await prepare_web(enabled=None)          # 库里没有 web_enabled 这一行
    task = asyncio.get_running_loop().create_task(ns["run_app"]())
    await asyncio.sleep(3.0)
    health = ns["component_health"]()
    db = await ns["BotMemoryDB"].get_instance()
    payload = {
        "telegram_state": health.get("telegram", {}).get("state"),
        "web_state": health.get("web", {}).get("state"),
        "web_running": ns["is_web_chat_running"](),
        "persisted_enabled": await db.get_config_fresh("web_enabled", None),
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
                        "从没表态过、且 Web 是唯一入口时要默认开起来")
        self.assertTrue(result["persisted_enabled"],
                        "迁移要把这次默认落库，之后库里的值就是权威")
        self.assertTrue(result["legacy_entry_gone"],
                        "run_web_only_main 应已被合并进 run_app，不留第二条启动路径")
        self.assertTrue(result["legacy_shutdown_gone"])
        self.assertTrue(result["legacy_post_init_gone"])
        self.assertEqual(0, result["exit_code"])

    def test_explicit_web_off_survives_a_restart(self):
        """用户显式关掉 Web：没有 token 也不许偷偷打开，进程也不许因此退出。

        cli+web 部署里"把网页关掉、只用 xgent 终端"是完全正当的诉求。以前
        _start_web_component 里 force=WEB_ONLY 会无声地把它打开回来。
        """
        result = self.run_probe("""
import asyncio

async def main():
    await prepare_web(enabled=False)
    task = asyncio.get_running_loop().create_task(ns["run_app"]())
    await asyncio.sleep(5.0)                  # 跨过一轮配置对账（3s）
    health = ns["component_health"]()
    db = await ns["BotMemoryDB"].get_instance()
    payload = {
        "web_running": ns["is_web_chat_running"](),
        "web_state": health.get("web", {}).get("state"),
        "still_off": await db.get_config_fresh("web_enabled", None),
        "triggers_state": health.get("triggers", {}).get("state"),
        "process_alive": not task.done(),
    }
    ns["request_app_stop"]()
    payload["exit_code"] = await asyncio.wait_for(task, timeout=30)
    print(json.dumps(payload))

asyncio.run(main())
""", extra_env={"BOT_TOKEN": ""})
        self.assertFalse(result["web_running"], "显式关掉就是关掉，不许被强制启动")
        self.assertEqual("disabled", result["web_state"])
        self.assertFalse(result["still_off"], "对账任务不许把开关改回去")
        self.assertEqual("up", result["triggers_state"],
                         "没有对话入口也要继续跑定时任务")
        self.assertTrue(result["process_alive"],
                        "进程不许因为'没有对话入口'而退出——PM2 会把它拖进重启循环")
        self.assertEqual(0, result["exit_code"])

    def test_password_set_later_brings_web_up_without_a_restart(self):
        """另一个进程写进库的密码，服务端必须自己发现并起来。

        这是"在 xgent CLI 里改 Web 密码，网页登录一直认证失败"的回归测试。以前
        每个进程只认自己启动时那份配置快照，CLI 写库之后服务端一无所知，而 CLI 自己
        去 restart_web_chat 又只会撞上服务端占着的端口（EADDRINUSE，且被日志吞掉）。
        """
        result = self.run_probe("""
import asyncio

async def main():
    await ns["UserDataManager"].init()        # 刻意先不设密码
    await ns["UserDataManager"].save_config("web_port", free_port())
    await ns["UserDataManager"].save_config("web_enabled", True)
    task = asyncio.get_running_loop().create_task(ns["run_app"]())
    await asyncio.sleep(1.0)
    before = ns["is_web_chat_running"]()

    # 模拟"另一个进程"（xgent CLI / install.sh）写库：只落库，不碰本进程的
    # 内存快照，也不碰服务器对象。
    db = await ns["BotMemoryDB"].get_instance()
    digest = ns["web_auth"].hash_password("set-from-another-process")
    await db.set_config(ns["WEB_PASSWORD_CONFIG_KEY"], digest)
    db._config_cache.pop(ns["WEB_PASSWORD_CONFIG_KEY"], None)

    await asyncio.sleep(5.0)                  # 跨过一轮对账
    payload = {
        "before": before,
        "after": ns["is_web_chat_running"](),
        "hash_in_use": (ns["_web_chat_server"].config.password_hash == digest
                        if ns["_web_chat_server"] is not None else None),
    }
    ns["request_app_stop"]()
    payload["exit_code"] = await asyncio.wait_for(task, timeout=30)
    print(json.dumps(payload))

asyncio.run(main())
""", extra_env={"BOT_TOKEN": ""})
        self.assertFalse(result["before"], "没密码时不该启动")
        self.assertTrue(result["after"], "密码落库后要自己起来，不需要重启服务")
        self.assertTrue(result["hash_in_use"], "服务端必须换用新哈希，否则登录照样失败")
        self.assertEqual(0, result["exit_code"])

    def test_no_entrypoint_stays_up_instead_of_crashlooping(self):
        """既没有 token、Web 也起不来（没设密码）：说清楚，但**不退出**。

        以前这里 return 1，而 PM2 立刻把它拉起来再撞同一堵墙——实测每 1.5~3 秒一轮、
        100% CPU、pm2 list 里还显示 online，正是那句"别硬撑"想避免的情形，只是更糟。
        定时任务和 CLI 同步还在，进程留着有意义；谁挂了由 /api/health 讲。
        """
        result = self.run_probe("""
import asyncio

async def main():
    await ns["UserDataManager"].init()          # 刻意不设密码
    task = asyncio.get_running_loop().create_task(ns["run_app"]())
    await asyncio.sleep(2.0)
    health = ns["component_health"]()
    payload = {
        "web_running": ns["is_web_chat_running"](),
        "web_state": health.get("web", {}).get("state"),
        "triggers_state": health.get("triggers", {}).get("state"),
        "process_alive": not task.done(),
    }
    ns["request_app_stop"]()
    payload["exit_code"] = await asyncio.wait_for(task, timeout=30)
    print(json.dumps(payload))

asyncio.run(main())
""", extra_env={"BOT_TOKEN": ""})
        self.assertFalse(result["web_running"])
        self.assertEqual("disabled", result["web_state"])
        self.assertEqual("up", result["triggers_state"])
        self.assertTrue(result["process_alive"], "不许自己退出，否则 PM2 会无限重启")
        self.assertEqual(0, result["exit_code"])

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
