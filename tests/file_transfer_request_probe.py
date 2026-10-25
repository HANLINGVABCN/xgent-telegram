import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.attachment_request_probe import Harness
from xgent_app.file_transfers import CHUNK_BYTES, FileTransfers


async def large_upload(bot, root):
    async with Harness(bot, root) as h:
        store = FileTransfers(root / 'transfers')
        size = 51 * 1024 * 1024
        manifest = {'idempotency_key': 'real-large-request', 'conversation_id': bot.current_scope().conversation_id,
                    'mode': 'attachment', 'text': '大文件超过50MB也需要保存',
                    'files': [{'filename': 'large.zip', 'size': size}]}
        identifier = store.begin(manifest)['transfer_id']
        try:
            for offset in range(0, size, CHUNK_BYTES):
                data = (b'PK\x03\x04' if offset == 0 else b'') + b'x' * (min(CHUNK_BYTES, size-offset) - (4 if offset == 0 else 0))
                await asyncio.to_thread(store.append, identifier, 0, offset, io.BytesIO(data), len(data))
            with patch.object(bot, '_web_chat_server', SimpleNamespace(outbox=h.outbox, config=SimpleNamespace(loop=asyncio.get_running_loop()))), patch.object(
                bot, 'process_conversation', AsyncMock()) as process, patch.object(bot, 'sync_web_attachment_to_telegram', AsyncMock()) as sync, patch.object(
                bot, 'build_web_mirror_objects', side_effect=lambda *a, **k: (h.update, h.context, None)):
                store.complete(identifier, lambda data, mode, cid: bot._web_submit_file_batch(data, mode, h.outbox, conversation_id=cid))
                for _ in range(1000):
                    state = store.status(identifier)
                    if state['state'] in {'accepted', 'failed'}:
                        break
                    await asyncio.sleep(.02)
                assert state['state'] == 'accepted', state
                await asyncio.sleep(.05)
                assert process.await_count == 1 and sync.await_count == 1
                refs = __import__('json').loads((await h.db.get_attachment_records())[-1]['metadata'])['attachments']
                assert len(refs) == 1 and refs[0]['size'] == size and refs[0]['path_only']
                assert (Path(bot.ArtifactManager.UPLOAD_DIR) / refs[0]['path']).stat().st_size == size
                assert state['message']['content'] == manifest['text']
                assert store.begin(manifest)['state'] == 'accepted'
                assert process.await_count == 1
            return {'51mb_saved': True, 'one_ai_turn': True, 'bot_sync_uses_shared_rules': True,
                    'no_duplicate_on_retry': True, 'full_text_preserved': True}
        finally:
            store.close()
