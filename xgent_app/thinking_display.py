"""Provider-returned visible thinking, isolated from answer/tool/model history."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import time
import uuid

_current = ContextVar('web_thinking_capture', default=None)


class ThinkingCapture:
    def __init__(self, outbox=None, message_id=None):
        self.outbox, self.message_id = outbox, message_id
        self.parts = []
        self.has_content = False
        self.elapsed = 0.0
        self.start = None
        self.streaming = True
        self.status = 'running'
        self.revision = 0
        self.last_emit = 0.0
        self.capture_id = uuid.uuid4().hex
        self.provider_elapsed = None

    def append(self, text):
        if not isinstance(text, str) or not text:
            return
        from .web_history import _section_ns
        namespace = _section_ns() or {}
        redact = namespace.get('redact_sensitive_text')
        if redact:
            text = redact(text)
        if self.status != 'running':
            self.status = 'running'
        if self.streaming and self.start is None and text.strip():
            self.start = time.monotonic()
        self.parts.append(text)
        self.has_content = self.has_content or bool(text.strip())
        self.revision += 1
        self.emit()

    def finish(self, status='completed'):
        if self.start is not None:
            self.elapsed += time.monotonic() - self.start
            self.start = None
        if status != 'completed' or self.status == 'running':
            self.status = status
        self.revision += 1
        self.emit(force=True)

    def snapshot(self):
        if not self.has_content:
            return None
        return {'version': 1, 'capture_id': self.capture_id, 'text': ''.join(self.parts), 'status': self.status,
                'elapsed_seconds': self.elapsed + (time.monotonic() - self.start if self.start is not None else 0)
                if self.streaming else self.provider_elapsed, 'revision': self.revision}

    def emit(self, force=False):
        now = time.monotonic()
        if self.outbox is None or not self.has_content or (not force and now-self.last_emit < .3):
            return
        self.last_emit = now
        self.outbox.put({'type': 'thinking_state', 'message_id': self.message_id, 'thinking': self.snapshot()})


def capture_stream(function):
    @wraps(function)
    async def wrapped(*args, thinking_sink=None, media_sink=None, **kwargs):
        from .media_response import MediaCapture, current_media
        media = media_sink or MediaCapture()
        iterator = function(*args, **kwargs).__aiter__()
        try:
            while True:
                token = _current.set(thinking_sink)
                media_token = current_media.set(media)
                try:
                    value = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                finally:
                    _current.reset(token)
                    current_media.reset(media_token)
                yield value
        except (asyncio.CancelledError, GeneratorExit):
            if thinking_sink:
                thinking_sink.finish('stopped')
            raise
        except Exception:
            if thinking_sink:
                thinking_sink.finish('failed')
            raise
        finally:
            if thinking_sink:
                thinking_sink.finish()
            close = getattr(iterator, 'aclose', None)
            if close:
                await close()
    return wrapped


def capture_complete(function):
    @wraps(function)
    async def wrapped(*args, thinking_sink=None, media_sink=None, **kwargs):
        from .media_response import MediaCapture, current_media
        media = media_sink or MediaCapture()
        media_token = current_media.set(media)
        token = _current.set(thinking_sink)
        if thinking_sink:
            thinking_sink.streaming = False
        try:
            value = await function(*args, **kwargs)
            if thinking_sink:
                thinking_sink.finish('failed' if value[1] else 'completed')
            return media.reply(value[0]), value[1]
        except asyncio.CancelledError:
            if thinking_sink:
                thinking_sink.finish('stopped')
            raise
        except Exception:
            if thinking_sink:
                thinking_sink.finish('failed')
            raise
        finally:
            _current.reset(token)
            current_media.reset(media_token)
    return wrapped


def observe(data, provider='openai'):
    """Accept only known visible text fields; never signatures or encrypted data."""
    sink = _current.get()
    if sink is None or not isinstance(data, dict):
        return False
    before = len(sink.parts)
    duration = data.get('reasoning_duration_seconds')
    if isinstance(duration, (int, float)) and 0 <= duration < 86400:
        sink.provider_elapsed = float(duration)
    answer = False
    if provider == 'openai':
        for choice in (data.get('choices') or [])[:1]:
            part = choice.get('delta') or choice.get('message') or {}
            value = part.get('reasoning_content') or part.get('reasoning')
            if isinstance(value, str):
                sink.append(value)
            elif isinstance(part.get('reasoning_details'), list):
                for detail in part['reasoning_details']:
                    if isinstance(detail, dict) and detail.get('type') in {'reasoning.text', 'reasoning.summary'}:
                        sink.append(detail.get('text') or detail.get('summary'))
            answer = bool(part.get('content'))
    elif provider == 'gemini':
        for candidate in (data.get('candidates') or [])[:1]:
            for part in candidate.get('content', {}).get('parts', []):
                if part.get('thought') is True:
                    sink.append(part.get('text'))
                else:
                    answer = answer or bool(part.get('text'))
    elif provider == 'claude':
        for part in data.get('content') or []:
            if part.get('type') == 'thinking':
                sink.append(part.get('thinking'))
            elif part.get('type') == 'text':
                answer = True
        delta = data.get('delta') or {}
        if delta.get('type') == 'thinking_delta':
            sink.append(delta.get('thinking'))
        elif delta.get('type') == 'text_delta':
            answer = True
        if data.get('type') == 'content_block_stop':
            sink.finish()
    if answer:
        sink.finish()
    return len(sink.parts) > before


def mark_failed():
    sink = _current.get()
    if sink is not None:
        sink.finish('failed')
