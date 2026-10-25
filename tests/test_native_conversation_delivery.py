"""Native Telegram must have the same output isolation as Web/CLI."""
import asyncio
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_channel_isolation import _FakeStore
from xgent_app.agent_sendfile import execute_sendfile_protocol
from xgent_app.fanout import ChannelWorker, Op, OP_SEND, OpTransientError
from xgent_app.web_bridge import (
    WebOutbox, build_telegram_conversation_objects, deliver_op_to_bot,
)


class NativeConversationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_input_and_context_are_preserved_without_mutating_ptb_objects(self):
        native = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=71)),
            edit_message_text=AsyncMock(),
        )
        message = SimpleNamespace(chat_id=42, message_id=19, text='native question',
                                  caption='original caption', forward_origin=object())
        query = SimpleNamespace(message=message, data='original callback', answer=AsyncMock())
        update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=42),
                                 effective_user=object(), callback_query=query, xgent_origin='telegram')
        context = SimpleNamespace(bot=native, user_data={'keep': 'original'}, args=['arg'])
        frames = []
        outbox = WebOutbox()
        outbox.put = frames.append

        async def deliver(op, mid):
            return await deliver_op_to_bot(native, op, mid)

        worker = ChannelWorker('native-proxy-test', deliver)
        try:
            wrapped, ctx, bot = build_telegram_conversation_objects(update, context, outbox, worker)
            self.assertIs(context.bot, native)
            self.assertIs(update.message, message)
            self.assertEqual('telegram', wrapped.xgent_origin)
            self.assertIs(update.effective_user, wrapped.effective_user)
            self.assertIs(message.forward_origin, wrapped.message.forward_origin)
            self.assertEqual('original caption', wrapped.message.caption)
            self.assertIs(context.user_data, ctx.user_data)
            self.assertIs(bot, wrapped.get_bot())
            self.assertIs(bot, wrapped.message.get_bot())
            self.assertEqual(query.data, wrapped.callback_query.data)
            # Disabling CLI mirroring must not mute native Telegram replies.
            with patch.dict(os.environ, {'XGENT_CLI_NO_TG_MIRROR': '1'}):
                sent = await wrapped.message.reply_text('first')
                native.send_message.assert_not_awaited()
                await sent.edit_text('final')
                self.assertTrue(await worker.wait_idle(timeout=2))
            native.send_message.assert_awaited_once()
            self.assertEqual(71, native.edit_message_text.call_args.kwargs['message_id'])
            self.assertEqual(['message', 'edit'], [f['type'] for f in frames])
        finally:
            await worker.aclose()

    async def test_new_messages_cannot_overtake_a_failed_earlier_result(self):
        attempts, delivered = [], []

        async def deliver(op, mid):
            text = op.payload['text']
            attempts.append(text)
            if len(attempts) == 1:
                raise OpTransientError('temporary Telegram timeout')
            delivered.append(text)
            return len(attempts)

        store = _FakeStore()
        worker = ChannelWorker('native-fifo-test', deliver, store=store)
        await worker._handle(Op(OP_SEND, logical_id=1, chat_id=42, payload={'text': 'command result'}))
        self.assertEqual(1, len(store.rows))
        await worker._handle(Op(OP_SEND, logical_id=2, chat_id=42, payload={'text': 'AI reply'}))
        self.assertEqual(['command result', 'command result', 'AI reply'], attempts)
        self.assertEqual(['command result', 'AI reply'], delivered)
        self.assertEqual([], store.rows)

    async def test_queued_files_are_reopened_after_callers_close_their_handles(self):
        from xgent_app.web_bridge import MirrorBot
        consumed = []

        async def send(**kwargs):
            handle = kwargs.get('document', kwargs.get('photo'))
            self.assertFalse(handle.closed)
            consumed.append(handle.read())
            return SimpleNamespace(message_id=len(consumed))

        native = SimpleNamespace(send_document=send, send_photo=send)

        async def deliver(op, mid):
            return await deliver_op_to_bot(native, op, mid)

        worker = ChannelWorker('native-file-handles', deliver)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'file.png'
                path.write_bytes(b'original bytes')
                bot = MirrorBot(WebOutbox(), 42, native, worker)
                with path.open('rb') as document, path.open('rb') as photo:
                    await bot.send_document(document=document)
                    await bot.send_photo(photo=photo)
                self.assertTrue(document.closed and photo.closed)
                self.assertTrue(await worker.wait_idle(timeout=2))
                self.assertEqual([b'original bytes', b'original bytes'], consumed)
        finally:
            await worker.aclose()

    async def test_local_api_mapping_lives_through_each_delayed_send_and_retry(self):
        from telegram.error import TimedOut
        from xgent_app.web_bridge import MirrorBot
        attempts = []
        complete = asyncio.Event()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'original file.bin'
            source.write_bytes(b'large file fixture')
            api_dir = root / 'api'

            async def send_document(**kwargs):
                uri = urlsplit(kwargs['document'])
                self.assertEqual('file', uri.scheme)
                relative = unquote(uri.path).removeprefix('/container/')
                exposed = api_dir / relative
                self.assertEqual(source.read_bytes(), exposed.read_bytes())
                attempts.append(exposed)
                if len(attempts) == 1:
                    raise TimedOut('temporary upload timeout')
                return SimpleNamespace(message_id=91)

            native = SimpleNamespace(send_document=send_document)

            async def deliver(op, mid):
                result = await deliver_op_to_bot(native, op, mid)
                complete.set()
                return result

            worker = ChannelWorker('native-local-api', deliver, store=_FakeStore())
            bot = MirrorBot(WebOutbox(), 42, native, worker)
            try:
                notice = await execute_sendfile_protocol(
                    str(source), executor=SimpleNamespace(resolve_file_path=lambda _: str(source)),
                    context=SimpleNamespace(bot=bot), chat_id=42, api_base_url='http://local',
                    local_api_host_data_dir=str(api_dir), local_api_container_data_dir='/container',
                    max_file_size=1, safe_send_message=AsyncMock(), safe_text=str,
                    logger=logging.getLogger(__name__), cancel_task_quietly=AsyncMock(),
                )
                self.assertIn('发送队列', notice)
                from xgent_app.web_history import delivered_file_reference
                self.assertEqual(str(source), delivered_file_reference(notice)[0]['path'])
                self.assertFalse(api_dir.exists(), 'no temporary file until actual delivery')
                await asyncio.wait_for(complete.wait(), 3)
                self.assertEqual(2, len(attempts))
                self.assertTrue(all(not path.exists() for path in attempts))
                self.assertTrue(source.exists())
            finally:
                await worker.aclose()
