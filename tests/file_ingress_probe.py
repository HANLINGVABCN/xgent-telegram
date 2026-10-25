import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from concurrent.futures import Future

from tests.attachment_request_probe import Harness, upload_update
from tests.test_attachments import image_bytes


def batch(root, files, **fields):
    temporary = tempfile.TemporaryDirectory(dir=root)
    items = []
    for i, (name, content) in enumerate(files):
        path = Path(temporary.name) / str(i)
        path.write_bytes(content)
        items.append({'filename': name, 'path': str(path), 'size': len(content),
                      'sha256': hashlib.sha256(content).hexdigest()})
    return {'files': items, 'text': 'batch caption', '_temporary': temporary, **fields}


async def workflows(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        files = [('image.dat', image_bytes()), ('notes.txt', b'full text\nEND'), ('archive.zip', b'PK\x03\x04\x00\x00binary')]
        sync = AsyncMock()
        process = AsyncMock()
        with patch.object(bot, 'sync_web_attachment_to_telegram', sync), patch.object(bot, 'process_conversation', process), patch.object(
            bot, 'build_web_mirror_objects', side_effect=lambda *a, **k: (h.update, h.context, None)):
            receipt = Future()
            await bot._web_run_file_batch(batch(root, files, idempotency_key='attachments'), 'attachment', h.outbox, receipt)
            await asyncio.sleep(.02)
            assert receipt.result()['ok'], receipt.result()
            assert process.await_count == 1
            assert sync.await_count == 3
            assert all(call.kwargs['ingress_mode'] == 'attachment' for call in sync.await_args_list)
            assert sync.await_args_list[0].args[2] == 'batch caption'
            assert receipt.result()['message']['content'] == 'batch caption'
            assert receipt.result()['message']['file_message']
            records = await h.db.get_attachment_records()
            refs = json.loads(records[-1]['metadata'])['attachments']
            assert [r['kind'] for r in refs] == ['image', 'text', 'binary']
            assert refs[-1]['path_only']
            from xgent_app.attachments import prepare_attachment_context
            parts, _, errors = prepare_attachment_context(records, bot.ArtifactManager.UPLOAD_DIR)
            assert not errors
            assert any(part['type'] == 'image' for part in parts)
            assert not any(part['type'] == 'binary' for part in parts)
            assert any('SHA-256:' in part.get('text', '') for part in parts)
            # Model capability fallback saves originals, supplies paths, no Base64.
            small, _, errors = prepare_attachment_context(records, bot.ArtifactManager.UPLOAD_DIR,
                file_limits={'max_files': 0})
            assert not errors and all(part['type'] == 'text' for part in small)
            result['web_batch_images_text_paths_and_sync'] = True
            process.reset_mock(); sync.reset_mock()
            duplicate = Future()
            await bot._web_run_file_batch(batch(root, files, idempotency_key='attachments'), 'attachment', h.outbox, duplicate)
            assert duplicate.result()['duplicate'] and not process.called and not sync.called
            result['persisted_idempotency'] = True
            await h.db.clear_all_conversation_memory()
            receipt = Future()
            await bot._web_run_file_batch(batch(root, files, idempotency_key='uploads'), 'server_upload', h.outbox, receipt)
            await asyncio.sleep(.02)
            assert receipt.result()['ok'] and process.await_count == 1
            assert sync.await_count == 3 and not await h.db.get_attachment_records()
            assert all(call.kwargs['ingress_mode'] == 'server_upload' for call in sync.await_args_list)
            assert 'SHA-256:' in process.call_args.args[2]
            assert 'full text\nEND' not in process.call_args.args[2]
            # Telegram synchronization never turns a server upload into an AI attachment.
            from tests.attachment_request_probe import unpack_request
            await h.call(question='use the uploaded paths')
            texts, images, _ = unpack_request(h.requests[-1])
            assert not images and 'full text\nEND' not in '\n'.join(texts)
            assert all(name in '\n'.join(texts) for name, _ in files)
            process.reset_mock(); sync.reset_mock()
            duplicate = Future()
            await bot._web_run_file_batch(batch(root, files, idempotency_key='uploads'), 'server_upload', h.outbox, duplicate)
            assert duplicate.result()['duplicate'] and not process.called and not sync.called
            result['server_upload_only_paths_and_telegram_sync'] = True
            process.reset_mock(); sync.reset_mock()
            mixed = Future()
            await bot._web_run_file_batch(batch(root, files, idempotency_key='mixed',
                modes=json.dumps(['attachment', 'attachment', 'server_upload'])), 'attachment', h.outbox, mixed)
            await asyncio.sleep(.02)
            assert mixed.result()['ok'] and process.await_count == 1 and sync.await_count == 3
            assert [call.kwargs['ingress_mode'] for call in sync.await_args_list] == ['attachment', 'attachment', 'server_upload']
            records = await h.db.get_attachment_records()
            metadata = json.loads(records[-1]['metadata'])
            assert len(metadata['attachments']) == 2 and len(metadata['server_uploads']) == 1
            from xgent_app.web_history import build_history_message
            history = build_history_message(records[-1], bot.ArtifactManager.ROOT_DIR, str(root))
            assert len(history['media']) == 3
            assert 'versions_id' not in history['media'][-1]
            result['mixed_batch_and_history'] = True

        # A real native send must finish before AI; it must not echo a second
        # AI-looking file back into Web while the class-level mirror is active.
        events, entered, release = [], asyncio.Event(), asyncio.Event()
        class NativeTelegram:
            async def send_document(self, chat_id=None, document=None, filename=None, caption=None, **kwargs):
                events.append(('user-file-start', filename, caption))
                entered.set()
                await release.wait()
                events.append(('user-file-sent', filename, document.read()))
                return SimpleNamespace(message_id=900, chat_id=chat_id)
            async def send_message(self, chat_id=None, text=None, **kwargs):
                events.append(('telegram-text', text))
                return SimpleNamespace(message_id=901, chat_id=chat_id)
        async def answer(*args, **kwargs):
            events.append(('ai-reply', args[2]))
        native = NativeTelegram()
        h.drain_frames()
        restore = bot.install_tg_to_web_mirror(native, h.outbox)
        try:
            with patch.object(bot, '_web_real_bot', native), patch.object(bot.telegram_channel(), 'is_open', return_value=False), patch.object(
                bot, 'process_conversation', AsyncMock(side_effect=answer)) as process, patch.object(
                bot, 'build_web_mirror_objects', side_effect=lambda *a, **k: (h.update, h.context, None)):
                receipt = Future()
                original_text = '完整用户文字 <tag> & 图片说明\n第二行不能丢失'
                task = asyncio.create_task(bot._web_run_file_batch(batch(root, [('original.png', image_bytes())],
                    text=original_text, idempotency_key='ordered-user-file'), 'attachment', h.outbox, receipt))
                await asyncio.wait_for(entered.wait(), 5)
                assert receipt.done() and receipt.result()['ok'] and not process.called
                assert original_text in events[0][2]
                snapshot = [frame for frame in h.drain_frames() if frame.get('upload_request_key') == 'ordered-user-file']
                assert len(snapshot) == 1 and snapshot[0]['type'] == 'record_snapshot'
                assert snapshot[0]['text'] == original_text and len(snapshot[0]['media']) == 1
                release.set(); await task
                assert [event[0] for event in events] == ['user-file-start', 'user-file-sent', 'ai-reply']
                assert not any(frame['type'] in {'document', 'photo', 'message'} for frame in h.drain_frames())
                result['telegram_order_full_text_and_no_reverse_echo'] = True
        finally:
            release.set()
            restore()

        # Exercise the actual HTTP-thread-to-event-loop scheduling callback.
        cid = bot.current_scope().conversation_id
        with patch.object(bot, '_web_chat_server', SimpleNamespace(outbox=h.outbox, config=SimpleNamespace(loop=asyncio.get_running_loop()))), patch.object(
            bot, 'process_conversation', AsyncMock()) as process, patch.object(bot, 'build_web_mirror_objects',
                side_effect=lambda *a, **k: (h.update, h.context, None)):
            receipt = bot._web_submit_file_batch(batch(root, [('scheduled.bin', b'\x00scheduled')],
                idempotency_key='scheduled'), 'server_upload', h.outbox, conversation_id=cid)
            accepted = await asyncio.wait_for(asyncio.wrap_future(receipt), 5)
            await asyncio.sleep(.05)
            assert accepted['ok'] and process.await_count == 1, (accepted, h.drain_frames(), await bot.get_conversations().state())
            retry = bot._web_submit_file_batch(batch(root, [('scheduled.bin', b'\x00scheduled')],
                idempotency_key='scheduled'), 'server_upload', h.outbox, conversation_id=cid)
            assert retry.result()['duplicate'] and process.await_count == 1
            result['real_submission_callback_and_retry'] = True

        await h.db.clear_all_conversation_memory()
        bot.UserDataManager.set('text_stitch_mode', 'off')
        async def download(media, destination=None):
            if destination:
                Path(destination).write_bytes(media.data)
                return destination
            return media.data
        with patch.object(bot, 'download_telegram_file', download), patch.object(bot, 'process_conversation', AsyncMock()) as process:
            first = upload_update(h, 'document', 501, b'PK\x03\x04binary', name='data.zip')
            second = upload_update(h, 'document', 502, b'text complete', name='data.txt')
            await bot.handle_document_message(*first)
            await bot.handle_document_message(*second)
            await bot.handle_document_message(*first)
            pending = bot._pending_text_conversations[bot.get_text_conversation_buffer_key(first[0])]
            assert len(pending.attachments) == 2 and not process.called
            assert not await h.db.get_attachment_records()
            await bot.queue_text_conversation(*first, 'instructions for both files')
            # A photo remains an independent request and leaves the buffer intact.
            photo = upload_update(h, 'photo', 503, image_bytes())
            await bot.handle_photo_message(*photo)
            assert process.await_count == 1 and len(pending.attachments) == 2
            process.reset_mock()
            await bot.finish_text_conversation(*first)
            assert process.await_count == 1
            assert 'instructions for both files' in process.call_args.args[2]
            rows = await h.db.get_attachment_records()
            assert len(json.loads(rows[-1]['metadata'])['attachments']) == 2
            assert not bot.has_pending_text_conversation(first[0])
            await bot.handle_document_message(*first)
            assert not bot.has_pending_text_conversation(first[0])
            result['existing_manual_stitch_in_off_mode_and_photo_isolation'] = True
            cancel = upload_update(h, 'document', 504, b'cancel this', name='cancel.txt')
            await bot.handle_document_message(*cancel)
            pending = bot._pending_text_conversations[bot.get_text_conversation_buffer_key(cancel[0])]
            stored = Path(bot.ArtifactManager.UPLOAD_DIR) / pending.attachments[0]['attachment']['path']
            await bot.cancel_text_conversation(cancel[0])
            assert not stored.exists() and process.await_count == 1
            result['cancel_cleans_uncommitted_files'] = True

        # Text received during a slow file download joins the same old buffer.
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(media, destination=None):
            entered.set(); await release.wait()
            Path(destination).write_bytes(media.data)
            return destination
        pair = upload_update(h, 'document', 600, b'slow content', name='slow.txt')
        with patch.object(bot, 'download_telegram_file', slow), patch.object(bot, 'process_conversation', AsyncMock()) as process:
            receiving = asyncio.create_task(bot.handle_document_message(*pair))
            await asyncio.wait_for(entered.wait(), 5)
            await bot.handle_normal_text_conversation(*pair, 'caption sent during download')
            await bot.finish_text_conversation(*pair)
            assert not process.called
            await bot.cancel_text_conversation(pair[0])
            release.set(); await receiving
            assert not bot.has_pending_text_conversation(pair[0]) and not process.called
            result['inflight_text_done_and_cancel_races'] = True

        # Both modes use the same cloud/local limits, with clear Telegram-only notes.
        telegram = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        caps = __import__('xgent_app.file_ingress', fromlist=['FileTransportCapabilities']).FileTransportCapabilities()
        source = root / 'local-send.bin'; source.write_bytes(b'file body')
        local = type(caps).configured('http://127.0.0.1:8081')
        for mode, label in [('attachment', '网页附件'), ('server_upload', '网页上传')]:
            telegram.send_message.reset_mock(); telegram.send_document.reset_mock()
            with patch.object(bot, '_web_real_bot', telegram), patch.object(bot.os.path, 'getsize', return_value=86 * 1024 * 1024), patch.object(
                bot.telegram_channel(), 'is_open', return_value=False):
                await bot.sync_web_attachment_to_telegram('server/archive.zip', 'archive.zip', '', transport_capabilities=caps, ingress_mode=mode)
                assert telegram.send_message.await_count == 1 and not telegram.send_document.called
                notice = telegram.send_message.call_args.kwargs['text']
                assert '50 MB' in notice and label in notice and 'server/archive.zip' in notice
                if mode == 'server_upload':
                    assert '仅向 AI 提供路径' in notice
                result[f'{mode}_oversize_sync_notice_on_telegram'] = True
                telegram.send_message.reset_mock()
                await bot.sync_web_attachment_to_telegram('server/archive.zip', 'archive.zip', '超限也保留我的文字\n第二行',
                    transport_capabilities=caps, ingress_mode=mode)
                assert telegram.send_message.await_count == 2 and not telegram.send_document.called
                assert '超限也保留我的文字\n第二行' in telegram.send_message.await_args_list[0].kwargs['text']
                assert '50 MB' in telegram.send_message.await_args_list[1].kwargs['text']
                result[f'{mode}_oversize_keeps_user_text_before_notice'] = True
            telegram.send_message.reset_mock(); telegram.send_document.reset_mock()
            with patch.object(bot, '_web_real_bot', telegram), patch.object(bot.telegram_channel(), 'is_open', return_value=False):
                await bot.sync_web_attachment_to_telegram(str(source), 'original.bin', 'user caption', transport_capabilities=caps, ingress_mode=mode)
                assert telegram.send_document.await_count == 1 and not telegram.send_message.called
                caption = telegram.send_document.call_args.kwargs['caption']
                assert label in caption and 'user caption' in caption
                if mode == 'server_upload':
                    assert '仅向 AI 提供路径，未发送文件内容' in caption
                result[f'{mode}_small_file_sync_with_summary'] = True
                telegram.send_message.reset_mock(); telegram.send_document.reset_mock()
                await bot.sync_web_attachment_to_telegram(str(source), 'original.bin', 'long description ' * 100,
                    transport_capabilities=caps, ingress_mode=mode)
                assert telegram.send_message.await_count == 1 and telegram.send_document.await_count == 1
                assert label in telegram.send_document.call_args.kwargs['caption']
                if mode == 'server_upload':
                    assert '仅向 AI 提供路径' in telegram.send_document.call_args.kwargs['caption']
                result[f'{mode}_long_caption_keeps_concise_summary'] = True
            telegram.send_message.reset_mock(); telegram.send_document.reset_mock()
            with patch.object(bot, '_web_real_bot', telegram), patch.object(bot.os.path, 'getsize', return_value=86 * 1024 * 1024), patch.object(
                bot.telegram_channel(), 'is_open', return_value=False), patch.object(bot, '_LOCAL_API_HOST_DATA_DIR', str(root/'local-api')):
                await bot.sync_web_attachment_to_telegram(str(source), 'original.bin', '', transport_capabilities=local, ingress_mode=mode)
                assert telegram.send_document.await_count == 1
                assert telegram.send_document.call_args.kwargs['document'].startswith('file://')
                assert label in telegram.send_document.call_args.kwargs['caption']
                assert not list((root/'local-api').iterdir())
                result[f'{mode}_local_large_file_sync_and_cleanup'] = True
            telegram.send_message.reset_mock(); telegram.send_document.reset_mock()
            with patch.object(bot, '_web_real_bot', None):
                await bot.sync_web_attachment_to_telegram('does-not-exist', 'any-size.zip', '', ingress_mode=mode)
                assert not telegram.send_document.called and not telegram.send_message.called
                result[f'{mode}_no_bot_no_sync'] = True
            with patch.object(bot, '_web_real_bot', telegram), patch('xgent_app.interaction.telegram_delivery_allowed', return_value=False):
                await bot.sync_web_attachment_to_telegram('does-not-exist', 'other-conversation.zip', '', ingress_mode=mode)
                assert not telegram.send_document.called and not telegram.send_message.called
                result[f'{mode}_respects_conversation_sync_route'] = True
        media = SimpleNamespace(file_size=21 * 1024 * 1024, get_file=AsyncMock())
        with patch.object(bot.BotConfig, 'API_BASE_URL', ''):
            try:
                await bot.download_telegram_file(media)
            except ValueError as exc:
                assert '20 MB' in str(exc) and not media.get_file.called
            else:
                raise AssertionError('cloud download limit was not checked before getFile')
        result['cloud_download_preflight'] = True
    return result
