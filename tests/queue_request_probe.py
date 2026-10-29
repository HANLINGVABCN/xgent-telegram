"""Full application queue scenarios, isolated from real model/Bot networks."""
import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, AsyncMock

from tests.attachment_request_probe import Harness, FORMATS, unpack_request
from xgent_app.conversations import bind_conversation, current_scope
from xgent_app.interaction import interaction
from xgent_app.message_queue import mark_outcome


PROTOCOL = '```run-x\n<<BEGIN_QUEUEPROTO123\nfixture-tool\n<<END_QUEUEPROTO123\n```'
OVER = '\n```over-x\n<<BEGIN_QUEUEOVER123\nDone.\n<<END_QUEUEOVER123\n```'


async def settled(bot):
    async def wait():
        queue = bot.get_text_queue()
        for _ in range(1000):
            await queue.poll()
            if not any(not task.done() for task in queue._tasks.values()) and not bot.get_conversations().busy():
                head = await (await bot.BotMemoryDB.get_instance()).queue_head()
                if not head or head['dispatch_instance'] != queue.instance:
                    return
            await asyncio.sleep(.01)
        raise AssertionError('queue never settled')
    await asyncio.wait_for(wait(), 25)


async def native_telegram_delivery_failure(bot, root):
    """Reproduce an executed command + Telegram timeout + queued user input."""
    from types import SimpleNamespace
    from telegram.error import TimedOut
    from xgent_app.fanout import get_channel_registry
    marker = 'DURABLE-COMMAND-RESULT'
    async with Harness(bot, root) as h:
        bot.UserDataManager.set('agent_mode', True)
        bot.UserDataManager.set('readx_persist_context', True)
        h.replies = [PROTOCOL, 'finished after command and queued instruction', 'next message works']
        started, release = asyncio.Event(), asyncio.Event()
        executed, sent, attempts = [], [], []

        class NativeBot:
            failed = False

            async def deliver(self, kwargs):
                text = kwargs.get('text', '')
                attempts.append(text)
                if marker in text and not self.failed:
                    # The result must be saved before its first network attempt.
                    history = await h.db.get_conversation_messages(1000)
                    assert any(marker in str(m['content']) for m in history), history
                    started.set()
                    await release.wait()
                    self.failed = True
                    raise TimedOut('fixture: result card timed out')
                sent.append(kwargs)

            async def send_message(self, **kwargs):
                await self.deliver(kwargs)
                return SimpleNamespace(message_id=len(attempts))

            async def edit_message_text(self, **kwargs):
                # Results now replace the running-command slot. A timeout on
                # that edit must exercise the same ordered notification retry.
                await self.deliver(kwargs)
                return True

            async def edit_message_reply_markup(self, **kwargs):
                return True

            async def delete_message(self, **kwargs):
                return True

            async def send_chat_action(self, **kwargs):
                return True

        async def tool(block, **kwargs):
            executed.append(block['body'])
            assert current_scope().origin == 'telegram'
            await bot.submit_ordinary_text('queued native instruction', chat_id=1, request_key='tg:queued')
            return {'kind': 'run', 'success': True, 'notice': marker, 'output': marker, 'return_code': 0,
                    'context_message': {'role': 'user', 'content': marker + '-FULL-OUTPUT'}}

        original = bot.ModelClient.think_and_reply

        async def model(*args, **kwargs):
            if len(h.requests) == 1:
                await asyncio.wait_for(started.wait(), 10)
                assert not release.is_set(), 'the model must not wait for Telegram'
            return await original(*args, **kwargs)

        scope = replace(current_scope(), origin='telegram', run_id=None)
        try:
            with patch.dict('os.environ', {'XGENT_CLI_NO_TG_MIRROR': '1'}), patch.object(
                    bot, '_web_real_bot', NativeBot()), patch.object(bot, 'get_web_outbox', return_value=h.outbox), patch.object(
                    bot, 'dispatch_standard_protocol', tool), patch.object(bot.ModelClient, 'think_and_reply', model), interaction(
                    'telegram', 'chat'), bind_conversation(scope):
                worker = bot.telegram_channel()
                worker._timeout = 90
                await bot.submit_ordinary_text('initial native request', chat_id=1, request_key='tg:initial')
                await asyncio.wait_for(settled(bot), 20)
                assert started.is_set() and not release.is_set()
                assert len(executed) == 1 and len(h.requests) == 2
                texts = '\n'.join(unpack_request(h.requests[1])[0])
                assert texts.count(marker + '-FULL-OUTPUT') == 1, texts
                assert texts.index(marker + '-FULL-OUTPUT') < texts.index('queued native instruction')
                snapshot = await bot.get_text_queue().snapshot(scope.conversation_id)
                assert not snapshot['paused'] and not snapshot['items'], snapshot
                # A new native message is still runnable while notification is blocked.
                await bot.submit_ordinary_text('after network glitch', chat_id=1, request_key='tg:after')
                await asyncio.wait_for(settled(bot), 20)
                assert len(h.requests) == 3 and len(executed) == 1
                release.set()
                assert await worker.wait_idle(timeout=6), worker.stats()
                assert sum(marker in text for text in attempts) == 2, attempts
                delivered = [m['text'] for m in sent]
                result_index = next(i for i, text in enumerate(delivered) if marker in text)
                reply_index = next(i for i, text in enumerate(delivered) if 'finished after command' in text)
                assert result_index < reply_index, delivered
                # The immutable full result also survives beyond the live round.
                history = await h.db.get_conversation_messages(1000)
                assert any(marker in str(m['content']) for m in history)
                assert not bot.get_conversations().busy()
                return {'command_exactly_once': True, 'result_saved_before_network': True,
                        'model_and_queue_continue_while_tg_blocked': True,
                        'later_messages_not_paused': True, 'ordered_replay': True}
        finally:
            release.set()
            await get_channel_registry().aclose_all()


async def native_storage_failure_still_pauses(bot, root):
    """Do not hide real storage failures or replay an already executed action."""
    from types import SimpleNamespace
    from xgent_app.fanout import get_channel_registry
    from xgent_app.message_queue import queue_card_text
    async with Harness(bot, root) as h:
        bot.UserDataManager.set('agent_mode', True)
        h.replies = [PROTOCOL, 'recovered pending input']
        native = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
            delete_message=AsyncMock(), send_chat_action=AsyncMock(),
        )
        executed = []

        async def tool(block, **kwargs):
            executed.append(block['body'])
            await bot.submit_ordinary_text('waiting after storage failure', chat_id=1, request_key='storage:queued')
            return {'kind': 'run', 'success': True, 'notice': 'UNSAVED-RESULT', 'output': 'UNSAVED-RESULT',
                    'context_message': {'role': 'user', 'content': 'full result'}}

        scope = replace(current_scope(), origin='telegram', run_id=None)
        save_record = h.db.record_global_message

        async def broken_result_storage(*args, **kwargs):
            if kwargs.get('msg_type') == bot.MessageType.AGENT_RESULT:
                raise OSError('fixture storage unavailable')
            return await save_record(*args, **kwargs)

        try:
            with patch.object(bot, '_web_real_bot', native), patch.object(bot, 'get_web_outbox', return_value=h.outbox), patch.object(
                    bot, 'dispatch_standard_protocol', tool), interaction('telegram', 'chat'), bind_conversation(scope):
                with patch.object(h.db, 'record_global_message', broken_result_storage):
                    await bot.submit_ordinary_text('initial before storage failure', chat_id=1, request_key='storage:initial')
                    await settled(bot)
                queue = bot.get_text_queue()
                snapshot = await queue.snapshot(scope.conversation_id)
                assert snapshot['paused'] and snapshot['reason'] == 'failed', snapshot
                assert len(executed) == len(h.requests) == len(snapshot['items']) == 1
                assert '/queue' in queue_card_text(snapshot['items'][0])
                assert await bot.telegram_channel().wait_idle(timeout=5)
                assert not any('UNSAVED-RESULT' in str(call.kwargs) for call in native.send_message.call_args_list)
                # Real paused receipts offer a direct recovery button with the
                # queue's CAS revision, rather than an unexplained dead end.
                buttons = [button for call in native.send_message.call_args_list + native.edit_message_text.call_args_list
                           if call.kwargs.get('reply_markup') for row in call.kwargs['reply_markup'].inline_keyboard for button in row]
                resume = [button.callback_data for button in buttons if str(button.callback_data).startswith('q:r:')]
                assert resume, buttons
                data = f"q:r:{snapshot['conversation_id']}:{snapshot['generation']}:{snapshot['revision']}"
                query = SimpleNamespace(answer=AsyncMock(), data=data,
                                        message=SimpleNamespace(message_id=78, edit_text=AsyncMock()))
                update = SimpleNamespace(message=None, callback_query=query, xgent_origin='telegram',
                                         effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=1))
                await bot.handle_queue_callback(update, SimpleNamespace(bot=native, args=[]), data)
                await settled(bot)
                snapshot = await queue.snapshot(scope.conversation_id)
                assert not snapshot['paused'] and not snapshot['items'], snapshot
                assert len(executed) == 1 and len(h.requests) == 2
                return {'storage_failure_still_pauses': True, 'clear_recovery_button': True,
                        'resume_only_sends_pending_input': True, 'no_command_replay': True}
        finally:
            await get_channel_registry().aclose_all()


async def tools_and_over(bot, root):
    results = {}
    for fmt in FORMATS:
        for over in (False, True):
            async with Harness(bot, Path(root) / (fmt + str(over))) as h:
                bot.UserDataManager.set('agent_mode', True)
                bot.UserDataManager.set('global_depth', 1)
                bot.UserDataManager.get('providers')['p']['api_format'] = fmt
                h.replies = [PROTOCOL + (OVER if over else ''), 'final with queue']
                async def tool(block, **kwargs):
                    if block['type'] != 'run':
                        return None
                    cid = current_scope().conversation_id
                    await bot._web_accept_text('queued ONE', conversation_id=cid, request_id='one')
                    await bot._web_accept_text('queued TWO\n  keep spacing', conversation_id=cid, request_id='two')
                    before = await h.db.get_conversation_messages(1000)
                    assert all('queued ONE' != m['content'] for m in before)
                    return {'kind': 'read', 'success': True, 'notice': 'display-only tool notice',
                            'context_message': {'role': 'user', 'content': 'EXACT-FULL-TOOL-RESULT'}}
                with patch.object(bot, 'dispatch_standard_protocol', tool), patch.object(bot, 'get_web_outbox', return_value=h.outbox):
                    await h.turn('initial question')
                    await settled(bot)
                assert len(h.requests) == 2, (fmt, over, len(h.requests))
                text = '\n'.join(unpack_request(h.requests[-1])[0])
                assert text.count('queued ONE') == 1 and text.count('queued TWO\n  keep spacing') == 1, text
                assert text.index('EXACT-FULL-TOOL-RESULT') < text.index('queued ONE') < text.index('queued TWO')
                history = await h.db.get_conversation_messages(1000)
                assert sum(m['content'] == 'queued ONE' for m in history) == 1
                assert not (await bot.get_text_queue().snapshot(current_scope().conversation_id))['items']
                results[fmt + str(over)] = True
    return results


async def natural_end(bot, root):
    results = {}
    for style in ('nonstream', 'foreground', 'background'):
        async with Harness(bot, Path(root) / style) as h:
            bot.UserDataManager.set('stream_mode', style != 'nonstream')
            bot.UserDataManager.set('stream_style', style if style != 'nonstream' else 'foreground')
            bot.UserDataManager.set('global_depth', 1)
            saved = []
            async def admit():
                if saved:
                    return
                cid = current_scope().conversation_id
                saved.append(await bot._web_accept_text('new one', conversation_id=cid, request_id='one'))
                saved.append(await bot._web_accept_text('new two', conversation_id=cid, request_id='two'))
                # The dispatcher must not reinterpret pending chat as a new secret
                # input just because another tab opened a configuration menu.
                bot.UserDataManager.set('state', bot.BotState.SET_SEARCH_KEY)
            original = bot.ModelClient.think_and_reply
            original_stream = bot.ModelClient.think_and_reply_stream
            async def complete(*args, **kwargs):
                await admit()
                return await original(*args, **kwargs)
            async def stream(*args, **kwargs):
                await admit()
                async for value in original_stream(*args, **kwargs):
                    yield value
            with patch.object(bot.ModelClient, 'think_and_reply', complete), patch.object(bot.ModelClient, 'think_and_reply_stream', stream), patch.object(bot, 'get_web_outbox', return_value=h.outbox):
                await h.turn('old question')
                await settled(bot)
            assert len(h.requests) == 2, (style, h.requests)
            text = '\n'.join(unpack_request(h.requests[-1])[0])
            assert text.count('new one') == text.count('new two') == 1, text
            assert text.index('new one') < text.index('new two')
            assert 'ok' not in unpack_request(h.requests[-1])[0], 'do not expand prior history beyond configured depth'
            assert bot.UserDataManager.get('state') == bot.BotState.SET_SEARCH_KEY
            results[style] = True
    return results


async def failures_and_secrets(bot, root):
    async with Harness(bot, root) as h:
        cid = current_scope().conversation_id
        queue = bot.get_text_queue()
        await queue.start()
        # Keep the execution slot held while testing ingress and cancellation.
        async with bot.get_conversations().operation(execution=True):
            first = await bot._web_accept_text('keep waiting', conversation_id=cid, request_id='regular')
            bot.UserDataManager.set('state', bot.BotState.SET_WEB_PASSWORD)
            # A retry of a previously accepted ordinary input must not become
            # the value for a later-opened password/Token form.
            repeated = await bot._web_accept_text('keep waiting', conversation_id=cid, request_id='regular')
            assert repeated['id'] == first['id'] and repeated['queued']
            async def secret_handler(text, outbox, **kwargs):
                assert text == 'do-not-persist-fixture-secret'
                bot.UserDataManager.set('state', bot.BotState.IDLE)
            with patch.object(bot, '_web_run_conversation', secret_handler):
                await bot._web_accept_text('do-not-persist-fixture-secret', conversation_id=cid, request_id='secret-request')
                await asyncio.sleep(.03)
                await bot._web_accept_text('do-not-persist-fixture-secret', conversation_id=cid, request_id='secret-request')
            snapshot = await queue.snapshot(cid)
            assert len(snapshot['items']) == 1 and 'do-not-persist' not in str(snapshot)
            mark_outcome('failed')
        snapshot = await queue.snapshot(cid)
        assert snapshot['paused'] and snapshot['items'][0]['reason'] == 'failed'
        assert not h.requests
        return {'secret_not_queued_on_retry': True, 'failure_pauses': True, 'no_automatic_retry': True}


async def ask_queue(bot, root):
    results = {}
    for cancel in (False, True):
        async with Harness(bot, Path(root) / str(cancel)) as h:
            bot.UserDataManager.set('agent_mode', True)
            h.replies = ['```ask-x\n<<BEGIN_QUEUEASK1234\nQ1|text|补充说明\n<<END_QUEUEASK1234\n```', 'after form']
            original = bot.ModelClient.think_and_reply
            submitted = []
            async def model(*args, **kwargs):
                if not submitted:
                    submitted.append(await bot._web_accept_text('message while form generating', conversation_id=current_scope().conversation_id, request_id='ask-queued'))
                return await original(*args, **kwargs)
            with patch.object(bot.ModelClient, 'think_and_reply', model), patch.object(bot, 'get_web_outbox', return_value=h.outbox):
                await h.turn('ask now')
                await settled(bot)
                snapshot = await bot.get_text_queue().snapshot(current_scope().conversation_id)
                assert snapshot['paused'] and snapshot['reason'] == 'ask'
                assert len(h.requests) == 1
                pending = list(bot.PENDING_ASKS._store.values())[-1]
                pending.ensure_entry('0')['custom'] = 'actual form answer'
                if cancel:
                    await bot.cancel_ask(pending.ask_id)
                else:
                    await bot.resume_from_ask(pending.ask_id)
                await settled(bot)
            assert len(h.requests) == 2
            text = '\n'.join(unpack_request(h.requests[-1])[0])
            if cancel:
                assert 'message while form generating' not in text
                assert (await bot.get_text_queue().snapshot(current_scope().conversation_id))['reason'] == 'ask_cancelled'
            else:
                assert text.count('message while form generating') == 1
                assert text.index('actual form answer') < text.index('message while form generating')
            results[str(cancel)] = True
    return results


async def stitched_native_text(bot, root):
    from types import SimpleNamespace
    from xgent_app.conversations import ConversationScope
    async with Harness(bot, root) as h:
        queue = bot.get_text_queue()
        await queue.start()
        bot.UserDataManager.set('text_stitch_mode', bot.TEXT_STITCH_MODE_FORCE)
        async with bot.get_conversations().operation(execution=True):
            original = current_scope()
            with interaction('telegram', 'chat'), bind_conversation(replace(original, origin='telegram', run_id=None)):
                await bot.handle_normal_text_conversation(h.update, h.context, 'first part')
                await bot.handle_normal_text_conversation(h.update, h.context, 'second part')
                assert not (await queue.snapshot(original.conversation_id))['items']
            with interaction('telegram', 'management'), bind_conversation(replace(original, origin='telegram', purpose='management', run_id=None)):
                update = SimpleNamespace(effective_chat=h.update.effective_chat, effective_user=h.update.effective_user,
                    message=None, callback_query=SimpleNamespace(answer=AsyncMock()))
                await bot.finish_text_conversation(update, h.context)
                items = (await queue.snapshot(original.conversation_id))['items']
                assert len(items) == 1 and 'first part' in items[0]['text'] and 'second part' in items[0]['text']
                assert items[0]['origin'] == 'telegram'
                assert not h.requests
            mark_outcome('stopped')
        return {'stitch_first_then_queue': True, 'management_callback_retains_chat_origin': True}
