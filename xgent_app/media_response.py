"""Keep provider media fields separate from prose throughout generation."""
from contextvars import ContextVar
from copy import deepcopy
import re

current_media = ContextVar('provider_media_capture', default=None)
_URL = re.compile(r'data:((?:image|audio|video)/[\w.+-]+);base64,([A-Za-z0-9+/_=\-]+)', re.I)


class MediaResponse(str):
    def __new__(cls, text, media):
        result = super().__new__(cls, text)
        result.media = list(media)
        return result

    def __bool__(self):
        return bool(str(self)) or bool(self.media)


class MediaCapture:
    def __init__(self):
        self.parts = []
        self.seen = set()

    def add(self, part, source):
        if not isinstance(part, dict):
            return False
        url = part.get('image_url') or part.get('url')
        if isinstance(url, dict):
            url = url.get('url')
        match = _URL.fullmatch(url) if isinstance(url, str) else None
        inline = part.get('inline_data') or part.get('inlineData') or {}
        data = match[2] if match else inline.get('data') or part.get('b64_json') or part.get('base64')
        mime = match[1] if match else inline.get('mime_type') or inline.get('mimeType') or part.get('mime_type') or ('image/png' if part.get('b64_json') else None)
        if not isinstance(data, str) or not mime:
            return False
        if data not in self.seen:
            self.seen.add(data)
            self.parts.append({'mime_type': mime, 'data': data, 'source_field': source})
        return True

    def reply(self, text):
        return MediaResponse(text, self.parts) if self.parts else text


def separate(data, provider='openai'):
    capture = current_media.get()
    if capture is None or not isinstance(data, dict):
        return data
    data = deepcopy(data)
    if provider == 'openai':
        for choice in data.get('choices') or []:
            for name in ('message', 'delta'):
                message = choice.get(name) or {}
                images = message.pop('images', [])
                for index, part in enumerate(images if isinstance(images, list) else []):
                    capture.add(part, name+'.images.'+str(index))
                content = message.get('content')
                if isinstance(content, list):
                    message['content'] = [part for index, part in enumerate(content)
                                          if not capture.add(part, name+'.content.'+str(index))]
                elif isinstance(content, str) and images:
                    # A quoted placeholder/preview is not an additional generated image.
                    content = _URL.sub('', content)
                    message['content'] = re.sub(r'`\s*`', '', content).strip('`\n ')
    elif provider == 'gemini':
        for candidate in data.get('candidates') or []:
            content = candidate.get('content') or {}
            content['parts'] = [part for index, part in enumerate(content.get('parts') or [])
                                if not capture.add(part, 'content.parts.'+str(index))]
    return data


def reply(text):
    capture = current_media.get()
    return capture.reply(text) if capture else text
