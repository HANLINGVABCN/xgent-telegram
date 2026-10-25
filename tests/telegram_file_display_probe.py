import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.attachment_request_probe import Harness, upload_update
from tests.test_attachments import image_bytes


async def native_images(bot, root):
    from xgent_app.interaction import interaction
    from xgent_app.web_history import build_history_message
    async with Harness(bot, root) as h:
        snapshots, answers = [], []
        async def download(media, destination=None):
            if destination:
                Path(destination).write_bytes(media.data)
                return destination
            return media.data
        async def answer(*args, **kwargs):
            new = [frame for frame in h.drain_frames() if frame['type'] == 'record_snapshot' and frame.get('role') == 'user']
            assert new, 'The user image must be published before the model reply'
            snapshots.extend(new); answers.append(args[2])
        caption = '刚刚更新了一下，再给你发个图片测试\n\n' + '完整附言 ' * 50
        with interaction('telegram', 'chat'), bot.bind_conversation(await bot.get_conversations().resolve()), patch.object(
            bot, '_web_chat_server', SimpleNamespace(outbox=h.outbox)), patch.object(
            bot, 'download_telegram_file', download), patch.object(bot, 'process_conversation', AsyncMock(side_effect=answer)) as process:
            for kind, mid, text in [('photo', 1001, caption), ('document', 1002, caption), ('photo', 1003, '')]:
                pair = upload_update(h, kind, mid, image_bytes(), caption=text, name='photo_from_telegram.png')
                handler = bot.handle_photo_message if kind == 'photo' else bot.handle_document_message
                await handler(*pair)
                frame = snapshots[-1]
                assert frame['text'] == text and frame['file_message']
                assert len(frame['media']) == 1 and frame['media'][0]['kind'] == 'photo'
                assert '已保存到' not in frame['text']
                row = await h.db.get_display_message(frame['record_id'])
                assert '已保存到' in row['content']
                metadata = json.loads(row['metadata'])
                assert metadata['attachments'][0]['caption'] == text
                history = build_history_message(row, bot.ArtifactManager.ROOT_DIR, str(root))
                assert history['content'] == frame['text'] and history['media'] == frame['media']
                assert frame['conversation_id'] == bot.current_scope().conversation_id
            assert process.await_count == 3
            with patch.object(bot, 'ALBUM_FLUSH_QUIET_SECONDS', 60):
                for mid in (1010, 1011):
                    await bot.handle_photo_message(*upload_update(h, 'photo', mid, image_bytes(),
                        group='telegram-display-album', caption=f'相册附言 {mid}'))
                assert process.await_count == 3
                frames = [f for f in h.drain_frames() if f['type'] == 'record_snapshot' and f.get('role') == 'user']
                assert [f['text'] for f in frames] == ['相册附言 1010', '相册附言 1011']
                for frame in frames:
                    assert frame['file_message'] and len(frame['media']) == 1
                # The album's user snapshots have already reached Web. Its AI
                # callback is not permitted to demand another user echo.
                process.side_effect = None
                await bot.flush_album_conversation((1, 'telegram-display-album'))
                assert process.await_count == 4
        return {'native_photo': True, 'image_document': True, 'empty_caption': True,
                'full_caption': True, 'live_before_ai': True, 'album': True, 'history_matches_live': True}
