import asyncio
from unittest.mock import patch

from xgent_app.thinking_display import ThinkingCapture, capture_stream, capture_complete, observe


def test_provider_thinking_is_isolated_and_accumulates_real_phase_time():
    async def source():
        observe({'choices': [{'delta': {'reasoning_content': '最新'}}]})
        yield ''
        observe({'choices': [{'delta': {'reasoning_content': '一行\n尾行', 'content': '正文'}}]})
        yield '正文'
        observe({'candidates': [{'content': {'parts': [{'thought': True, 'text': '再次思考'}]}}]}, 'gemini')
        observe({'type': 'content_block_delta', 'delta': {'type': 'signature_delta', 'signature': 'PRIVATE'}}, 'claude')
        yield '结束'
    capture = ThinkingCapture()
    async def run():
        return [part async for part in capture_stream(source)(thinking_sink=capture)]
    with patch('xgent_app.thinking_display.time.monotonic', side_effect=lambda: 100):
        assert asyncio.run(run()) == ['', '正文', '结束']
    snapshot = capture.snapshot()
    assert snapshot['text'] == '最新一行\n尾行再次思考'
    assert snapshot['status'] == 'completed' and snapshot['elapsed_seconds'] == 0
    assert 'PRIVATE' not in str(snapshot)


def test_nonstream_does_not_invent_duration_and_missing_content_has_no_block():
    async def source():
        observe({'content': [{'type': 'thinking', 'thinking': '可见摘要'},
                             {'type': 'text', 'text': '正文'}]}, 'claude')
        return '正文', None
    capture = ThinkingCapture()
    assert asyncio.run(capture_complete(source)(thinking_sink=capture)) == ('正文', None)
    assert capture.snapshot()['elapsed_seconds'] is None
    assert ThinkingCapture().snapshot() is None


def test_stop_keeps_thinking_and_does_not_execute_protocol_text():
    async def source():
        observe({'choices': [{'delta': {'reasoning_content': '```run-x\nrm NEVER\n```'}}]})
        yield ''
        raise asyncio.CancelledError
    capture = ThinkingCapture()
    async def run():
        try:
            async for _ in capture_stream(source)(thinking_sink=capture):
                pass
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
    assert capture.snapshot()['status'] == 'stopped'
    assert 'run-x' in capture.snapshot()['text']
