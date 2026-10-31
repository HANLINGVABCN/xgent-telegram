"""Exercise real providers, persistence and Telegram finalization with fake IO."""
import html
import json
from unittest.mock import patch

import httpx

from tests.attachment_request_probe import Harness, unpack_request
from tests.telegram_rich_probe import RichCapture
from xgent_app.fanout import get_channel_registry


class RefusalHarness(Harness):
    refused = True
    detail = 'RAW REFUSAL DETAIL'

    def respond(self, request):
        if not self.refused:
            return super().respond(request)
        body = json.loads(request.content)
        self.requests.append(body)
        self.request_bytes.append(request.content)
        streaming = body.get('stream') or 'streamGenerateContent' in str(request.url)
        payload = {'id': 'original-response', 'model': 'vision-model', 'object': 'chat.completion.chunk' if streaming else 'chat.completion',
                   'created': 1, 'choices': [{'index': 0, 'finish_reason': 'prohibited_content',
                                            'delta' if streaming else 'message': {'role': 'assistant', 'content': None,
                                                                                 'refusal': self.detail}}]}
        if 'contents' in body:
            payload = {'candidates': [], 'promptFeedback': {'blockReason': 'SAFETY', 'blockReasonMessage': self.detail}}
        elif 'max_tokens' in body and 'temperature' not in body:
            payload = ({'type': 'message_delta', 'delta': {'stop_reason': 'refusal'}, 'refusal': self.detail}
                       if streaming else {'content': [], 'stop_reason': 'refusal', 'refusal': self.detail})
        if streaming:
            events = 'data: ' + json.dumps(payload) + '\n\ndata: {"choices": [], "usage": {}}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, text=events, headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json=payload)


async def exercise(bot, root):
    results = {}
    async with RefusalHarness(bot, root) as h:
        bot.UserDataManager.set('global_depth', 50)
        native = RichCapture()
        h.context = bot._SelfTriggerContext(native)
        h.update = bot._SelfTriggerUpdate(native, 1)
        with patch.object(bot, '_web_real_bot', native), patch.object(bot, 'get_web_outbox', return_value=h.outbox):
            for fmt in ('openai_compatible', 'openai', 'gemini', 'claude'):
                bot.UserDataManager.get('providers')['p']['api_format'] = fmt
                for stream, style in ((False, 'foreground'), (True, 'foreground'), (True, 'background')):
                    await h.db.clear_all_conversation_memory()
                    bot.UserDataManager.set('stream_mode', stream)
                    bot.UserDataManager.set('stream_style', style)
                    native.messages.clear()
                    h.refused = True
                    h.drain_frames()
                    await h.turn('request refused by upstream')
                    assert await bot.telegram_channel().wait_idle(5)
                    rows = await h.db.get_global_messages(100, active_context=True)
                    errors = [row for row in rows if row['msg_type'] == 'runtime_error']
                    assert len(errors) == 1, rows
                    error = errors[0]
                    reason = {'gemini': 'SAFETY', 'claude': 'refusal'}.get(fmt, 'prohibited_content')
                    assert reason in error['content'] and 'RAW REFUSAL DETAIL' in error['content'], error
                    assert '上游模型拒绝生成' not in error['content']
                    assert not any(row['msg_type'] == 'ai_reply' for row in rows), rows
                    messages = [message for message in native.messages.values() if 'RAW REFUSAL DETAIL' in message['text']]
                    assert len(messages) == 1, native.messages
                    assert not messages[0]['reply_markup'], messages
                    assert '<pre' not in messages[0]['text'] and '<code' not in messages[0]['text'], messages
                    assert reason in html.unescape(messages[0]['text'])
                    metadata = json.loads(error['metadata']) if isinstance(error['metadata'], str) else error['metadata']
                    saved = metadata['telegram_native_parts']['ids']
                    assert saved and all(mid in native.messages for mid in saved), metadata
                    frames = html.unescape(json.dumps(h.drain_frames(), ensure_ascii=False))
                    assert 'RAW REFUSAL DETAIL' in frames, frames
                    from xgent_app.web_history import build_history_message
                    legacy = {**error, 'metadata': {**metadata, 'display': {
                        'content': '<pre>' + html.escape(error['content']) + '</pre>', 'parse_mode': 'HTML'}}}
                    restored = build_history_message(legacy, bot.ArtifactManager.ROOT_DIR, str(root))
                    assert restored['content'] == error['content'] and restored['parse_mode'] is None, restored
                    assert restored['web_display']['plain_text'] == error['content'], restored
                    h.refused = False
                    await h.turn('explain the raw error')
                    assert await bot.telegram_channel().wait_idle(5)
                    texts, _, _ = unpack_request(h.requests[-1])
                    assert any(error['content'] in text for text in texts), texts
                    assert all(mid in native.messages for mid in saved), native.messages
                    results[f'{fmt}/{stream}/{style}'] = True
            bot.UserDataManager.get('providers')['p']['api_format'] = 'openai_compatible'
            bot.UserDataManager.set('stream_mode', False)
            await h.db.clear_all_conversation_memory()
            native.messages.clear()
            h.refused = True
            h.detail = 'RAW REFUSAL DETAIL ' + '长错误，保留原文。' * 1000 + ' ORIGINAL-END'
            h.drain_frames()
            await h.turn('long upstream failure')
            assert await bot.telegram_channel().wait_idle(5)
            rows = await h.db.get_global_messages(100, active_context=True)
            error = next(row for row in rows if row['msg_type'] == 'runtime_error')
            assert 'RAW REFUSAL DETAIL' in error['content'], error
            assert len(error['content'].removeprefix('⚠️ ')) == 2000, error
            assert 'ORIGINAL-END' not in error['content'], error
            frames = h.drain_frames()
            visible = [frame for frame in frames if frame.get('msg_type') == 'runtime_error' and frame.get('web_display')]
            assert visible and all(error['content'] in html.unescape(frame['web_display']['segments'][0]['text']) for frame in visible), frames
            assert error['content'] in html.unescape(''.join(message['text'] for message in native.messages.values())), native.messages
            from xgent_app.web_history import build_history_message
            history = build_history_message(error, bot.ArtifactManager.ROOT_DIR, str(root))
            assert error['content'] in html.unescape(history['content']), history
            assert history['content'] == error['content'] and history['web_display']['plain_text'] == error['content'], history
            results['long_error_live_and_history'] = True
            h.refused = False
            h.error = (500, 'ORIGINAL HTTP 500 EOF')
            for stream, style in ((False, 'foreground'), (True, 'foreground'), (True, 'background')):
                await h.db.clear_all_conversation_memory()
                native.messages.clear()
                bot.UserDataManager.set('stream_mode', stream)
                bot.UserDataManager.set('stream_style', style)
                await h.turn('HTTP error')
                assert await bot.telegram_channel().wait_idle(5)
                rows = await h.db.get_global_messages(100, active_context=True)
                error = next(row for row in rows if row['msg_type'] == 'runtime_error')
                assert 'ORIGINAL HTTP 500 EOF' in error['content']
                assert sum('ORIGINAL HTTP 500 EOF' in message['text'] for message in native.messages.values()) == 1
                results[f'http/{stream}/{style}'] = True
        await get_channel_registry().aclose_all()
    return results
