import base64
from unittest.mock import AsyncMock, patch

from tests.attachment_request_probe import Harness
from tests.test_attachments import image_bytes
from tests.telegram_rich_probe import RichCapture
from xgent_app.media_response import MediaResponse
from xgent_app.fanout import get_channel_registry


async def exercise(bot, root):
    async with Harness(bot, root) as h:
        native = RichCapture()
        response = MediaResponse('图片文字只出现一次', [
            {'mime_type':'image/png','data':base64.b64encode(image_bytes('PNG', (n*60,50,70))).decode(),
             'source_field':'message.images.'+str(n)} for n in range(2)])
        h.context = bot._SelfTriggerContext(native)
        h.update = bot._SelfTriggerUpdate(native, 1)
        with patch.object(bot, '_web_real_bot', native), patch.object(bot, 'get_web_outbox', return_value=h.outbox), \
             patch.object(bot.ModelClient, 'think_and_reply', AsyncMock(return_value=(response,None))):
            await h.turn('生成两张图')
            channel = bot.telegram_channel()
            assert await channel.wait_idle(5)
            texts = [message['text'] for message in native.messages.values()]
            media = [text for text in texts if '<tg-collage>' in text]
            assert len(media) == 1, texts
            assert media[0].count('图片文字只出现一次') == 1, texts
            assert '系统自动生成' not in ''.join(texts), texts
            assert sum(text.count('回复详情') for text in texts) == 1, texts
            assert '回复详情' in media[0], texts
        await get_channel_registry().aclose_all()
        return {'media_messages':len(media),'details':1}
