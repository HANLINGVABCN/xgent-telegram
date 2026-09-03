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

    def test_rich_finalize_does_not_bypass_web_outbox(self):
        """网页会话的最终回复必须投到 web outbox，不能直连 Telegram rich API。

        回归：rich_finalize_text_response 原本无差别调 TelegramRichAPI.send_rich_message
        （直连 TG Bot API，绕过 context.bot）。网页会话用 WebBot/MirrorBot，于是网页
        outbox 收不到回复帧——占位气泡被删后只剩 token 信息，必须刷新拉 history 才
        正常。修复后网页 bot 走 finalize_text_response，发 edit 帧到 outbox。
        """
        output = self.run_probe(r'''
import asyncio
import json
from types import SimpleNamespace as NS
import xgent_server as bot
from xgent_app.web_bridge import WebOutbox

async def main():
    # ---- 网页 bot：必须走 finalize_text_response，不直连 TG rich API ----
    outbox = WebOutbox()
    stream = outbox.subscribe()
    _update, context, webbot = bot.build_web_conversation_objects(7, outbox)
    msg = await webbot.send_message(chat_id=7, text="…")  # 占位气泡
    stream.get(timeout=0.5)  # 丢掉占位 message 帧

    web_rich_calls = []
    async def fake_web_rich(*a, **kw):
        web_rich_calls.append(kw.get("text"))
        return {"ok": True}
    bot.TelegramRichAPI.send_rich_message = fake_web_rich

    await bot.rich_finalize_text_response(context, 7, msg, "真实回复正文", limit=4000)
    frames = []
    while True:
        f = stream.get(timeout=0.1)
        if f is None:
            break
        frames.append(f)
    stream.close()

    # ---- 非 web bot：原生 Telegram 仍走直连 rich API ----
    tg_rich_calls = []
    async def fake_tg_rich(*a, **kw):
        tg_rich_calls.append(kw.get("text"))
        return {"ok": True}
    bot.TelegramRichAPI.send_rich_message = fake_tg_rich

    class FakeMsg:
        async def delete(self):
            return True
    await bot.rich_finalize_text_response(
        NS(bot=NS(_is_xgent_web_bot=False)), 7, FakeMsg(), "tg回复", limit=4000)

    print(json.dumps({
        "web_rich_called": bool(web_rich_calls),
        "web_frame_types": [f["type"] for f in frames],
        "web_has_reply_text": any("真实回复正文" in (f.get("text") or "") for f in frames),
        "tg_rich_called": bool(tg_rich_calls),
    }, ensure_ascii=False))

asyncio.run(main())
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertFalse(data["web_rich_called"], "网页会话不应直连 Telegram rich API")
        self.assertTrue(data["web_has_reply_text"], "网页 outbox 必须收到含回复正文的帧")
        self.assertTrue(data["tg_rich_called"], "原生 Telegram 仍应走 rich API")

    def test_provider_import_syncs_chat_session_model(self):
        """导入配置/Web 设置换默认模型后，会话绑定必须跟着换。

        回归：模型取值是两层存储——发消息优先读 chat_sessions.model，没有
        才回退全局 default_model。导入配置原本只更新全局层，会话里残留的
        旧模型名配上换掉后的新提供商通道发出去，上游 502 unknown
        provider；replace 导入还会删旧提供商，旧绑定直接悬空。前台显示
        读全局（新模型），实际请求读会话（旧模型），两边对不上。
        """
        output = self.run_probe(r'''
import asyncio
import json
import xgent_server as bot

def _payload(defaults):
    return {
        "format": "xgent-telegram-provider-config",
        "version": 1,
        "providers": {
            "newprov": {
                "base_url": "https://example.com/v1",
                "api_key": "secret",
                "models": ["model-new"],
                "api_format": "openai",
            }
        },
        "defaults": defaults,
    }

async def main():
    await bot.UserDataManager.init()
    db = await bot.BotMemoryDB.get_instance()

    # 预置"导入前"状态：旧提供商 + 会话钉着旧模型（正常使用中）
    await db.import_providers({
        "oldprov": {"base_url": "https://old.example.com/v1", "api_key": "k",
                    "models": ["model-old"], "api_format": "openai"}
    }, replace=False)
    await bot.save_model_target_selection('chat', 'oldprov', 'model-old')
    await db.create_session(bot.SINGLE_MEMORY_SESSION_ID, 'model-old')
    bot.UserDataManager.set('current_chat_id', bot.SINGLE_MEMORY_SESSION_ID)

    # replace 导入：defaults 指向新提供商的新模型 → 会话绑定同步
    providers, defaults = bot.parse_provider_config_import(
        json.dumps(_payload({"active_provider": "newprov",
                             "default_model": "model-new"})).encode("utf-8"))
    await bot.apply_provider_config_import(providers, defaults, mode='replace')
    session = await db.get_session(bot.SINGLE_MEMORY_SESSION_ID)
    replace_synced = session['model'] == 'model-new'

    # replace 导入但 defaults 的模型无效 → 会话绑定清空，不留悬空引用
    await db.create_session(bot.SINGLE_MEMORY_SESSION_ID, 'model-old')
    providers, defaults = bot.parse_provider_config_import(
        json.dumps(_payload({"active_provider": "newprov",
                             "default_model": "model-gone"})).encode("utf-8"))
    await bot.apply_provider_config_import(providers, defaults, mode='replace')
    session = await db.get_session(bot.SINGLE_MEMORY_SESSION_ID)
    replace_invalid_cleared = session['model'] is None

    # merge 导入 + 有效 defaults：同样要同步
    await db.create_session(bot.SINGLE_MEMORY_SESSION_ID, 'model-old')
    providers, defaults = bot.parse_provider_config_import(
        json.dumps(_payload({"active_provider": "newprov",
                             "default_model": "model-new"})).encode("utf-8"))
    await bot.apply_provider_config_import(providers, defaults, mode='merge')
    session = await db.get_session(bot.SINGLE_MEMORY_SESSION_ID)
    merge_synced = session['model'] == 'model-new'

    # Web 设置 chat_model：会话绑定同步
    await bot._web_write_setting('chat_model', 'newprov|model-new')
    session = await db.get_session(bot.SINGLE_MEMORY_SESSION_ID)
    web_synced = session['model'] == 'model-new'

    # merge 导入覆盖同名提供商、新模型列表不含当前模型、defaults 为空：
    # 悬空的全局选择连同会话绑定必须一起清掉，并在结果里汇报。这是上一轮
    # 修复漏掉的路径——merge 不删提供商，但同名覆盖同样换血模型列表。
    await db.import_providers({
        "p2": {"base_url": "https://p2.example.com/v1", "api_key": "k",
               "models": ["m2-old"], "api_format": "openai"}
    }, replace=False)
    await bot.save_model_target_selection('chat', 'p2', 'm2-old')
    await db.create_session(bot.SINGLE_MEMORY_SESSION_ID, 'm2-old')
    payload = {
        "format": "xgent-telegram-provider-config", "version": 1,
        "providers": {"p2": {"base_url": "https://p2.example.com/v1",
                             "api_key": "k", "models": ["m2-new"],
                             "api_format": "openai"}},
        "defaults": {},
    }
    providers, defaults = bot.parse_provider_config_import(json.dumps(payload).encode("utf-8"))
    result = await bot.apply_provider_config_import(providers, defaults, mode='merge')
    session = await db.get_session(bot.SINGLE_MEMORY_SESSION_ID)
    merge_dangling_cleared = session['model'] is None
    merge_dangling_global_cleared = bot.UserDataManager.get('default_model') is None
    merge_dangling_reported = result.get('cleared_dangling') == ['对话模型']

    # resolve_effective_chat_model：发消息前的最终防线，纯函数直接断言。
    resolved = {
        "valid": bot.resolve_effective_chat_model("m2-new", "m2-old", {"models": ["m2-new"]}),
        "fallback": bot.resolve_effective_chat_model("m2-gone", "m2-new", {"models": ["m2-new"]}),
        "both_stale": bot.resolve_effective_chat_model("m2-gone", "m2-old", {"models": ["m2-new"]}),
        "wildcard": bot.resolve_effective_chat_model("anything", None, {"models": []}),
        "unset": bot.resolve_effective_chat_model(None, None, {"models": ["m2-new"]}),
    }

    # aiosqlite 的工作线程不是 daemon：不关连接的话探针进程退不出去
    await db.close()

    print(json.dumps({
        "replace_synced": replace_synced,
        "replace_invalid_cleared": replace_invalid_cleared,
        "merge_synced": merge_synced,
        "web_synced": web_synced,
        "merge_dangling_cleared": merge_dangling_cleared,
        "merge_dangling_global_cleared": merge_dangling_global_cleared,
        "merge_dangling_reported": merge_dangling_reported,
        "resolved": resolved,
    }, ensure_ascii=False))

asyncio.run(main())
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertTrue(data["replace_synced"],
                        "replace 导入恢复默认模型后，会话绑定必须同步成新模型")
        self.assertTrue(data["replace_invalid_cleared"],
                        "replace 导入删掉旧提供商后，悬空的会话绑定必须清空")
        self.assertTrue(data["merge_synced"],
                        "merge 导入恢复默认模型后，会话绑定必须同步成新模型")
        self.assertTrue(data["web_synced"],
                        "Web 设置 chat_model 后，会话绑定必须同步")
        self.assertTrue(data["merge_dangling_cleared"],
                        "merge 覆盖同名提供商后，悬空的会话绑定必须清空")
        self.assertTrue(data["merge_dangling_global_cleared"],
                        "merge 覆盖同名提供商后，悬空的全局默认必须清空")
        self.assertTrue(data["merge_dangling_reported"],
                        "清空的悬空选择必须在导入结果里汇报给用户")
        resolved = data["resolved"]
        self.assertEqual("m2-new", resolved["valid"],
                         "会话模型有效时原样返回")
        self.assertEqual("m2-new", resolved["fallback"],
                         "会话模型悬空、全局默认有效时回退全局默认")
        self.assertIsNone(resolved["both_stale"],
                          "两层都悬空时必须返回 None（按未配置处理），绝不能拿旧模型名发请求")
        self.assertEqual("anything", resolved["wildcard"],
                         "models 为空的通配型提供商不做校验")
        self.assertIsNone(resolved["unset"], "两层都没设置时返回 None")

    def test_protocol_and_normalization_behavior(self):
        output = self.run_probe(r'''
import json
import xgent_server as bot
sample = "before\n```run-x\n<<BEGIN_run_ok_0123\necho ok\n<<END_run_ok_0123\n```\nafter"
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


    def test_album_message_merges_photos_into_one_multimodal_payload(self):
        output = self.run_probe(r'''
import json
import xgent_server as bot

photos = [
    {"image_b64": "IMG1", "saved_notice": "第1张已保存到 a/1.jpg", "index_text": "[图片] telegram_photo.jpg，已保存到 a/1.jpg。说明：看这个"},
    {"image_b64": "IMG2", "saved_notice": "第2张已保存到 a/2.jpg", "index_text": "[图片] telegram_photo.jpg，已保存到 a/2.jpg"},
    {"image_b64": "IMG3", "saved_notice": "第3张已保存到 a/3.jpg", "index_text": "[图片] telegram_photo.jpg，已保存到 a/3.jpg"},
]
memory_text, multimodal = bot.build_album_message(photos, "看这个", "[转发] 老王")

types = [part["type"] for part in multimodal]
images = [part["data"] for part in multimodal if part["type"] == "image"]
texts = [part["text"] for part in multimodal if part["type"] == "text"]

print(json.dumps({
    "photo_count": len(photos),
    "memory_text": memory_text,
    "image_count": len(images),
    "image_order": images,
    "has_caption": any("用户附言：看这个" == t for t in texts),
    "has_context_prefix": any(t == "[转发] 老王" for t in texts),
    "saved_notice_count": sum(1 for t in texts if "已保存到" in t),
}, ensure_ascii=False))
''')
        data = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(3, data["photo_count"])
        self.assertIn("共3张", data["memory_text"])
        self.assertEqual(3, data["image_count"])
        self.assertEqual(["IMG1", "IMG2", "IMG3"], data["image_order"])
        self.assertTrue(data["has_caption"])
        self.assertTrue(data["has_context_prefix"])
        self.assertEqual(3, data["saved_notice_count"])


if __name__ == "__main__":
    unittest.main()
