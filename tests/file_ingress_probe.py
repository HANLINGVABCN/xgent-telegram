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
            assert receipt.result()['ok'] and process.await_count == 1
            assert not sync.called and not await h.db.get_attachment_records()
            assert 'SHA-256:' in process.call_args.args[2]
            result['server_upload_only_paths'] = True
            process.reset_mock(); sync.reset_mock()
            mixed = Future()
            await bot._web_run_file_batch(batch(root, files, idempotency_key='mixed',
                modes=json.dumps(['attachment', 'attachment', 'server_upload'])), 'attachment', h.outbox, mixed)
            await asyncio.sleep(.02)
            assert mixed.result()['ok'] and process.await_count == 1 and sync.await_count == 2
            records = await h.db.get_attachment_records()
            metadata = json.loads(records[-1]['metadata'])
            assert len(metadata['attachments']) == 2 and len(metadata['server_uploads']) == 1
            from xgent_app.web_history import build_history_message
            history = build_history_message(records[-1], bot.ArtifactManager.ROOT_DIR, str(root))
            assert len(history['media']) == 3
            assert 'versions_id' not in history['media'][-1]
            result['mixed_batch_and_history'] = True

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

        # Oversized Web attachments must produce a Telegram-side notice.
        telegram = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        caps = __import__('xgent_app.file_ingress', fromlist=['FileTransportCapabilities']).FileTransportCapabilities()
        with patch.object(bot, '_web_real_bot', telegram), patch.object(bot.os.path, 'getsize', return_value=86 * 1024 * 1024), patch.object(
            bot.telegram_channel(), 'is_open', return_value=False):
            await bot.sync_web_attachment_to_telegram('server/archive.zip', 'archive.zip', '', transport_capabilities=caps)
            assert telegram.send_message.await_count == 1 and not telegram.send_document.called
            assert '50 MB' in telegram.send_message.call_args.kwargs['text']
            result['oversize_sync_notice_on_telegram'] = True
        source = root / 'local-send.bin'; source.write_bytes(b'file body')
        telegram.send_message.reset_mock(); telegram.send_document.reset_mock()
        with patch.object(bot, '_web_real_bot', telegram), patch.object(bot.telegram_channel(), 'is_open', return_value=False):
            await bot.sync_web_attachment_to_telegram(str(source), 'original.bin', '', transport_capabilities=caps)
            assert telegram.send_document.await_count == 1
            result['small_attachment_sync'] = True
        telegram.send_document.reset_mock()
        local = type(caps).configured('http://127.0.0.1:8081')
        with patch.object(bot, '_web_real_bot', telegram), patch.object(bot.os.path, 'getsize', return_value=86 * 1024 * 1024), patch.object(
            bot.telegram_channel(), 'is_open', return_value=False), patch.object(bot, '_LOCAL_API_HOST_DATA_DIR', str(root/'local-api')):
            await bot.sync_web_attachment_to_telegram(str(source), 'original.bin', '', transport_capabilities=local)
            assert telegram.send_document.await_count == 1
            assert telegram.send_document.call_args.kwargs['document'].startswith('file://')
            assert not list((root/'local-api').iterdir())
            result['local_large_attachment_sync_and_cleanup'] = True
        with patch.object(bot, '_web_real_bot', None):
            await bot.sync_web_attachment_to_telegram('does-not-exist', 'any-size.zip', '')
            result['no_bot_no_sync'] = True
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
