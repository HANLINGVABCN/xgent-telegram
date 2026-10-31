import asyncio
import html
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.attachment_request_probe import Harness
from tests.telegram_chat_probe import TelegramCapture
from tests.telegram_rich_probe import RichCapture
from xgent_app.agent_ask import PENDING_ASKS
from xgent_app.conversations import bind_conversation, current_scope
from xgent_app.fanout import get_channel_registry
from xgent_app.interaction import interaction
from xgent_app.web_ask import handle_ask


async def exercise(bot, root):
    result = {}
    for kind, cls in [('rich', RichCapture), ('legacy', TelegramCapture)]:
        async with Harness(bot, Path(root) / kind) as h:
            await bot.UserDataManager.save_config('agent_mode', True)
            native = cls(); calls = []; tasks = []
            raw = ('请填表。\n```ask-x\n<<BEGIN_RECEIPT123456\nQ1|single|环境\n- 测试\n- 生产\n'
                   'Q2|text|完整备注\nQ3|secret:SHOW_RECEIPT_KEY|密钥\n<<END_RECEIPT123456\n```')
            async def model(*args, **kwargs):
                calls.append(args[5]); return (raw if len(calls) == 1 else '已经收到答案。'), None
            def background(value):
                tasks.append(asyncio.create_task(value))
            with patch.object(bot, '_web_real_bot', native), patch.object(bot, 'get_web_outbox', return_value=h.outbox), \
                    patch.object(bot.ModelClient, 'think_and_reply', model):
                scope = replace(current_scope(), origin='telegram', purpose='chat', run_id=None, telegram_delivery=True)
                with interaction('telegram', 'chat'), bind_conversation(scope):
                    await bot.GlobalRecorder.record_user_message('填表', chat_id=1)
                    await bot.process_conversation(bot._SelfTriggerUpdate(native, 1), bot._SelfTriggerContext(native), '填表')
                assert await bot.telegram_channel().wait_idle(5)
                pending = next(item for item in PENDING_ASKS._store.values() if item.conversation_id == scope.conversation_id)
                mid = next(mid for mid, item in native.messages.items() if '📝 需要你的输入：' in item['text'])
                record_id = pending.form_record_id
                secret = 'PRIVATE<&>BOT-RECEIPT'
                with interaction('web', 'management'), bind_conversation(replace(scope, origin='web', purpose='management', telegram_delivery=False)):
                    await handle_ask(vars(bot), 'POST', {'id': pending.ask_id, 'action': 'submit', 'revision': pending.revision,
                        'draft': {'0': {'selected': [1]}, '1': {'custom': '完整用户答案'}},
                        'secrets': {'2': secret}}, background)
                await asyncio.gather(*tasks)
                assert await bot.telegram_channel().wait_idle(5)
                shown = native.messages[mid]['text']
                assert '✅ 表单已提交' in shown and '✅ 生产' in shown and '▫️ 测试' in shown
                assert secret in html.unescape(shown) and '完整用户答案' in shown
                assert ('<details><summary>查看问题与答案</summary>' if kind == 'rich' else '<blockquote expandable>') in shown
                assert native.messages[mid]['reply_markup'] is None
                assert not any('📝 需要你的输入：' in message['text'] for message in native.messages.values())
                records = await h.db.get_global_messages(100)
                assert secret not in json.dumps(records) and html.escape(secret) not in json.dumps(records)
                assert secret not in json.dumps(calls)
                assert html.escape(secret) not in bot.redact_sensitive_text(html.escape(secret))
                row = await h.db.get_display_message(record_id)
                metadata = json.loads(row['metadata']) if isinstance(row['metadata'], str) else row['metadata']
                assert metadata['message_policy']['model_context'] is False
                assert secret not in json.dumps(h.drain_frames())
                result[kind] = True
            await get_channel_registry().aclose_all()
    return result
