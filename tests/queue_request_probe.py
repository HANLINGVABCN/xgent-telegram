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
            task = queue._task
            if (not task or task.done()) and not bot.get_conversations().lock.busy():
                head = await (await bot.BotMemoryDB.get_instance()).queue_head()
                if not head or head['dispatch_instance'] != queue.instance:
                    return
            await asyncio.sleep(.01)
        raise AssertionError('queue never settled')
    await asyncio.wait_for(wait(), 25)


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
