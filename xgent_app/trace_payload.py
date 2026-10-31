"""Remove transport media before logging/serializing a request or response."""
import hashlib
import re

_MEDIA = re.compile(r'data:(?:image|video|audio)/[\w.+-]+;base64,[A-Za-z0-9+/_=\-]+', re.I)
_ENCODED = re.compile(r'[A-Za-z0-9+/_=\-]{128,}')


def summarize(value, key=''):
    if isinstance(value, dict):
        return {name: summarize(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [summarize(item, key) for item in value]
    if isinstance(value, str):
        if key in {'data', 'base64', 'b64_json'} and _ENCODED.fullmatch(value):
            return '[媒体编码已省略；字符数=' + str(len(value)) + '; sha256=' + hashlib.sha256(value.encode()).hexdigest() + ']'
        return _MEDIA.sub(lambda m: '[媒体编码已省略；字符数=' + str(len(m[0])) + ']', value)
    return value
