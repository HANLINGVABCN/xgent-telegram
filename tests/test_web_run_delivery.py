"""Join only the unfinished Web reply; exercise the real asynchronous TG queue."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_conversations as support
from tests.test_channel_isolation import _FakeStore
from xgent_app.conversations import current_scope
from xgent_app.fanout import ChannelWorker, CircuitBreaker, Op, OP_SEND
from xgent_app.interaction import interaction
from xgent_app.reply_presentation import track_reply_presentation
from xgent_app.web_bridge import MirrorBot, WebOutbox, deliver_op_to_bot, markup_from_frame
from xgent_app.web_bridge import install_tg_to_web_mirror


class NativeBot:
    def __init__(self):
        self.messages = {}
        self.events = []
        self.gate = None
        self.offline = False

    async def send_message(self, **kw):
        if self.offline:
            raise ConnectionError('offline fixture')
        if self.gate:
            await self.gate.wait()
        mid = len(self.messages) + 1
        self.messages[mid] = kw
        self.events.append(('send', mid, kw))
        return SimpleNamespace(message_id=mid)

    async def edit_message_text(self, **kw):
        mid = kw['message_id']
        assert mid in self.messages, 'an edit overtook native message creation'
        self.messages[mid].update(kw)
        self.events.append(('edit', mid, kw))

    async def send_document(self, **kw):
        contents = kw['document'].read()
        return await self.send_message(text=contents.decode(), caption=kw.get('caption'))


class WebRunDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ConversationTests.asyncSetUp
    asyncTearDown = support.ConversationTests.asyncTearDown

    async def bridge(self, store=None):
        native = NativeBot()
        async def deliver(op, mid):
            return await deliver_op_to_bot(native, op, mid, markup_builder=markup_from_frame)
        channel = ChannelWorker('join-test', deliver, store=store, timeout=2,
                                breaker=CircuitBreaker(failure_threshold=1, cooldown=1))
        self.addAsyncCleanup(channel.aclose)
        return native, MirrorBot(WebOutbox(), 1, native, channel=channel), channel

    async def test_join_snapshots_all_current_parts_before_edits_and_is_sticky(self):
        a = (await self.manager.state())['telegram_conversation_id']
        b = (await self.manager.manage('create', name='Web B'))['conversation_id']
        native, mirror, channel = await self.bridge()
        native.gate = asyncio.Event()
        with patch('xgent_app.conversations._manager', self.manager), interaction('web'):
            async with self.manager.operation(b, execution=True, fresh=True):
                @track_reply_presentation
                async def old_reply():
                    await mirror.send_message(text='DO-NOT-BACKFILL')
                await old_reply()
                muted = Op(OP_SEND, logical_id=999, payload={'text': 'OLD-MUTED-OP'})
                @track_reply_presentation
                async def reply():
                    first = await mirror.send_message(text='placeholder')
                    await first.edit_text('current first part')
                    second = await mirror.send_message(text='current second part')
                    await self.manager.manage('switch', b, selector='telegram')
                    self.assertFalse(current_scope().telegram_delivery, 'original scope must remain immutable')
                    self.assertTrue(muted.muted)
                    self.assertFalse(channel.offer(Op.from_row(muted.to_row())))
                    self.assertEqual([], native.events, 'joining must not await Telegram')
                    await second.edit_text('second part + subsequent tokens')
                    await self.manager.manage('switch', a, selector='telegram')
                    native.gate.set()
                    self.assertTrue(await mirror.flush_telegram())
                    self.assertEqual(2, len(native.messages))
                    self.assertIn('current first part', native.messages[1]['text'])
                    self.assertIn('subsequent tokens', native.messages[2]['text'])
                    self.assertTrue(native.messages[2]['text'].startswith('🗂 Web B'))
                    await self.manager.manage('switch', b, selector='telegram')
                    await self.manager.manage('switch', b, selector='telegram')
                    await second.edit_text('⏹️ stopped, all partial text retained')
                    self.assertTrue(await mirror.flush_telegram())
                    self.assertEqual(2, len(native.messages), 'repeated switches must not create messages')
                    self.assertEqual('⏹️ stopped, all partial text retained', native.messages[2]['text'])
                await reply()
            self.assertIsNone(self.manager._reply_delivery)
        self.assertNotIn('DO-NOT-BACKFILL', str(native.events))

    async def test_join_between_replies_has_no_backlog_but_receives_next_reply(self):
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        native, mirror, _ = await self.bridge()
        with patch('xgent_app.conversations._manager', self.manager), interaction('web'):
            async with self.manager.operation(b, execution=True, fresh=True):
                @track_reply_presentation
                async def reply(text):
                    await mirror.send_message(text=text)
                await reply('past')
                await self.manager.manage('switch', b, selector='telegram')
                self.assertEqual({}, native.messages)
                await reply('next')
                self.assertTrue(await mirror.flush_telegram())
        self.assertEqual(['next'], [m['text'] for m in native.messages.values()])

    async def test_join_and_closed_upload_survive_disconnect_and_run_completion(self):
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        store = _FakeStore()
        native, mirror, channel = await self.bridge(store)
        native.offline = True
        path = Path(self.temp.name) / 'result.txt'
        path.write_text('attachment snapshot', encoding='utf-8')
        with patch('xgent_app.conversations._manager', self.manager), interaction('web'):
            async with self.manager.operation(b, execution=True, fresh=True):
                @track_reply_presentation
                async def reply():
                    message = await mirror.send_message(text='already produced')
                    with path.open('rb') as handle:
                        await mirror.send_document(document=handle, filename=path.name)
                    await self.manager.manage('switch', b, selector='telegram')
                    await message.edit_text('final after joining')
                    self.assertTrue(await mirror.flush_telegram())
                await reply()
            self.assertTrue(store.rows)
            self.assertTrue(all(not Op.from_row(row).muted for row in store.rows))
            native.offline = False
            channel._breaker.reset()
            await channel._drain_store(0)
            self.assertEqual([], store.rows)
        texts = [m['text'] for m in native.messages.values()]
        self.assertEqual(['final after joining', 'attachment snapshot'], texts)

    async def test_clear_revokes_an_unsubscribed_snapshot(self):
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        native, mirror, _ = await self.bridge()
        with patch('xgent_app.conversations._manager', self.manager), interaction('web'):
            async with self.manager.operation(b, execution=True, fresh=True):
                @track_reply_presentation
                async def reply():
                    await mirror.send_message(text='old generation')
                    await self.db.clear_all_conversation_memory()
                    await self.manager.manage('switch', b, selector='telegram')
                await reply()
        self.assertEqual({}, native.messages)

    async def test_join_during_bot_selection_does_not_echo_native_sends_back_to_web(self):
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        native, mirror, _ = await self.bridge()
        with patch('xgent_app.conversations._manager', self.manager), interaction('web'):
            async with self.manager.operation(b, execution=True, fresh=True):
                with mirror.outbox.subscribe() as web:
                    @track_reply_presentation
                    async def reply():
                        message = await mirror.send_message(text='current reply')
                        # Telegram entry installs this class patch while handling
                        # the switch button. Fanout runs concurrently with it.
                        restore = install_tg_to_web_mirror(native, mirror.outbox)
                        try:
                            await self.manager.manage('switch', b, selector='telegram')
                            await message.edit_text('current reply continued')
                            self.assertTrue(await mirror.flush_telegram())
                        finally:
                            restore()
                    await reply()
                    frames = []
                    while (frame := web.get(timeout=0)) is not None:
                        frames.append(frame)
        self.assertEqual(['message', 'edit'], [f['type'] for f in frames])
        self.assertEqual(1, len(native.messages))


def test_real_long_stream_renderer_joins_all_parts_once_and_stops_in_place():
    from tests.test_thinking_params import run_in_app
    result = run_in_app(r'''
import asyncio,tempfile
from types import SimpleNamespace
from tests.attachment_request_probe import Harness
from tests.test_web_run_delivery import NativeBot
from xgent_app.fanout import ChannelWorker
from xgent_app.interaction import interaction
from xgent_app.reply_presentation import track_reply_presentation,bind_reply_message_ids
from xgent_app.web_bridge import MirrorBot,deliver_op_to_bot,markup_from_frame

async def main():
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot,folder) as h:
            manager=bot.get_conversations()
            first=(await manager.state())['telegram_conversation_id']
            cid=(await manager.manage('create',name='Long Web reply'))['conversation_id']
            native=NativeBot()
            async def deliver(op,mid):
                return await deliver_op_to_bot(native,op,mid,markup_builder=markup_from_frame)
            channel=ChannelWorker('renderer-join-test',deliver,timeout=2)
            mirror=MirrorBot(h.outbox,1,native,channel=channel)
            context=SimpleNamespace(bot=mirror)
            raw='# 主标题\n\n'+''.join(f'fragment-key-{i:03d}: '+('完整的段落内容 '*18)+'\n\n' for i in range(60))
            tail='\n## 新的章节\n\n'+''.join(f'fragment-key-{i:03d}: '+('追加内容 '*20)+'\n\n' for i in range(60,95))
            try:
                with interaction('web','chat'):
                    async with manager.operation(cid,execution=True,fresh=True):
                        @bot.without_ui_history
                        @track_reply_presentation
                        async def reply():
                            msg=await mirror.send_message(chat_id=1,text='流式输出中...')
                            renderer=bot.TelegramStreamRenderer(context,1,msg,None,4000,asyncio.Event())
                            bind_reply_message_ids(renderer.message_ids)
                            renderer.start()
                            await renderer.append(raw)
                            async def flushed():
                                while len(renderer._web_chunks)<2:
                                    await asyncio.sleep(.01)
                            await asyncio.wait_for(flushed(),3)
                            assert not native.messages
                            await manager.manage('switch',cid,selector='telegram')
                            assert await mirror.flush_telegram()
                            initial=len(native.messages)
                            assert initial==len(renderer._web_chunks)>1
                            await manager.manage('switch',first,selector='telegram')
                            await renderer.append(tail)
                            assert await renderer.stop_and_keep_partial()==raw+tail.rstrip()
                            assert await mirror.flush_telegram()
                            joined='\n'.join(message['text'] for message in native.messages.values())
                            for i in range(95):
                                assert joined.count(f'fragment-key-{i:03d}:')==1,(i,joined)
                            assert joined.count('已停止，保留以上已生成内容')==1
                            assert len(native.messages)==len(renderer._web_chunks)>initial
                            await manager.manage('switch',cid,selector='telegram')
                            assert await mirror.flush_telegram()
                            assert len(native.messages)==len(renderer._web_chunks)
                            displays=[frame['web_display'] for frame in h.drain_frames() if frame.get('web_display')]
                            assert displays and all(d.get('reply_message_id')==msg.message_id for d in displays)
                        await reply()
                return {'all_fragments_once':True,'stopped_in_place':True,'one_semantic_reply_id':True}
            finally:
                await channel.aclose()
print(json.dumps(asyncio.run(main())))
''')
    assert all(result.values())


def test_stream_stop_exits_if_wait_for_loses_a_cancellation_race():
    """Deterministically reproduce the Python <=3.11 queue.get/wait_for race."""
    from tests.test_thinking_params import run_in_app
    result = run_in_app(r'''
import asyncio,tempfile
from unittest.mock import patch
from tests.attachment_request_probe import Harness
async def main():
    async with Harness(bot,tempfile.mkdtemp()) as h:
        for mode in ('stop','cancel'):
            msg=await h.context.bot.send_message(chat_id=1,text='fixture')
            renderer=bot.TelegramStreamRenderer(h.context,1,msg,None,4000,asyncio.Event())
            entered=asyncio.Event()
            async def lose_cancel(awaitable,timeout):
                try:
                    entered.set()
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    # Older asyncio can return the completed queue.get value
                    # instead of propagating cancellation. Do not extend timeout.
                    return await awaitable
            with patch.object(bot.asyncio,'wait_for',lose_cancel):
                renderer.start()
                await entered.wait()
                await renderer.append('Already generated text')
                operation=asyncio.create_task(renderer.stop_and_keep_partial() if mode=='stop' else renderer.cancel())
                done,_=await asyncio.wait({operation},timeout=2)
                assert operation in done,'lost cancellation left renderer waiting forever'
                await operation
                assert renderer._task.done()
        return {'bounded_stop':True,'bounded_cancel':True}
print(json.dumps(asyncio.run(main())))
''')
    assert all(result.values())
