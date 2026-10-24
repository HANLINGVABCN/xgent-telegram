"""Disk-backed ingress. Telegram limits never constrain Web reception."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class FileTransportCapabilities:
    mode: str = 'cloud'
    download_limit: int | None = 20 * 1024 * 1024
    document_upload_limit: int = 50 * 1024 * 1024
    photo_upload_limit: int = 10 * 1024 * 1024

    @classmethod
    def configured(cls, api_url='', mode=None):
        mode = (mode or os.getenv('TELEGRAM_API_MODE', 'auto')).lower()
        local = mode == 'local_bot_api' or (mode == 'auto' and
            urlparse(api_url).hostname in {'127.0.0.1', 'localhost', '::1'})
        if local:
            return cls('local_bot_api', None, 2000 * 1024 * 1024, 2000 * 1024 * 1024)
        return cls()


def fingerprint(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        before = os.fstat(handle.fileno())
        size = 0
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    sig = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if size != before.st_size or sig(before) != sig(after) or sig(after) != sig(Path(path).stat()):
        raise ValueError('文件在读取期间变化，请写入完成后重试')
    return size, digest.hexdigest()


def inspect_file(path, name, mime_type=None):
    from .attachments import AttachmentContextError, inspect_payload
    size = Path(path).stat().st_size
    with open(path, 'rb') as handle:
        sample = handle.read(CHUNK_SIZE)
    import filetype
    detected = filetype.guess(sample)
    mime = detected.mime if detected else (mime_type or mimetypes.guess_type(name)[0] or 'application/octet-stream')
    try:
        with Image.open(path) as image:
            info = {'kind': 'image', 'mime_type': Image.MIME.get(image.format, mime),
                    'width': image.width, 'height': image.height}
            image.verify()
        if size > 8 * 1024 * 1024 or info['width'] * info['height'] > 16_000_000:
            info['path_only'] = True
        else:
            with Image.open(path) as image:
                image.load()
        return info
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        pass
    if size <= 8 * 1024 * 1024:
        try:
            return inspect_payload(Path(path).read_bytes(), name, mime)
        except AttachmentContextError:
            pass
    return {'kind': 'binary', 'mime_type': mime, 'path_only': True}


def describe_file(reference, root):
    path = Path(root) / reference['path']
    text = (f"[服务器文件 / Server file: {reference['name']}]\nOriginal: {path}\n"
            f"Size: {reference['size']} bytes\nType: {reference['mime_type']}\n"
            f"SHA-256: {reference['sha256']}\n"
            '文件内容未直接展开；需要时通过 read-x 或文件工具读取。')
    if reference.get('caption'):
        text += '\nUser caption:\n' + reference['caption']
    if reference.get('context_prefix'):
        text += '\n' + reference['context_prefix']
    return text


def cleanup_payloads(payloads, upload_root):
    root = Path(upload_root).resolve()
    for payload in payloads:
        ref = payload['attachment']
        target = (root / ref['path']).resolve()
        if target != root and target.is_relative_to(root):
            target.unlink(missing_ok=True)
        from .attachment_versions import AttachmentVersions
        store = AttachmentVersions(root, ref)
        directory = store.directory.resolve()
        if directory.parent == store.root.resolve() and directory.exists():
            shutil.rmtree(directory)


def read_multipart(stream, length, content_type, directory):
    """Incremental multipart parser. No cap on file counts or bytes.

    Text metadata has a separate 1 MiB bound. The receiver owns the returned
    temporary directory until asynchronous handoff is complete.
    """
    message = Message()
    message['Content-Type'] = content_type
    boundary = message.get_boundary()
    if message.get_content_type() != 'multipart/form-data' or not boundary or len(boundary) > 200:
        raise ValueError('无效的 multipart boundary')
    marker = b'\r\n--' + boundary.encode('ascii')
    buffer = bytearray()
    remaining = length

    def fill():
        nonlocal remaining
        chunk = stream.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise ValueError('上传中断：请求体不完整')
        remaining -= len(chunk)
        buffer.extend(chunk)

    def line():
        while b'\r\n' not in buffer:
            if len(buffer) > CHUNK_SIZE or not remaining:
                raise ValueError('无效的 multipart 头')
            fill()
        end = buffer.index(b'\r\n')
        result = bytes(buffer[:end])
        del buffer[:end + 2]
        return result

    temporary = tempfile.TemporaryDirectory(prefix='incoming-', dir=directory)
    result = {'files': [], 'text': '', '_temporary': temporary}
    try:
        if line() != b'--' + boundary.encode('ascii'):
            raise ValueError('无效的 multipart 起始边界')
        final = False
        while not final:
            headers = Message()
            total = 0
            while True:
                raw = line()
                total += len(raw)
                if total > CHUNK_SIZE:
                    raise ValueError('multipart 头过大')
                if not raw:
                    break
                key, sep, value = raw.decode('utf-8', 'replace').partition(':')
                if not sep:
                    raise ValueError('无效的 multipart 头')
                headers[key] = value.strip()
            name = headers.get_param('name', header='content-disposition')
            filename = headers.get_filename()
            file_part = filename is not None and name in {'file', 'files'}
            handle = open(Path(temporary.name) / str(len(result['files'])), 'wb') if file_part else None
            content = bytearray()
            digest = hashlib.sha256()
            size = 0

            def write(data):
                nonlocal size
                size += len(data)
                if handle:
                    handle.write(data)
                    digest.update(data)
                else:
                    if size > 1024 * 1024:
                        raise ValueError('上传说明字段过大')
                    content.extend(data)

            try:
                while True:
                    pos = buffer.find(marker)
                    if pos >= 0:
                        while len(buffer) < pos + len(marker) + 2 and remaining:
                            fill()
                        suffix = bytes(buffer[pos + len(marker):pos + len(marker) + 2])
                        if suffix in {b'\r\n', b'--'}:
                            if suffix == b'--':
                                while len(buffer) < pos + len(marker) + 4 and remaining:
                                    fill()
                                tail = bytes(buffer[pos + len(marker) + 2:pos + len(marker) + 4])
                                if tail not in {b'', b'\r\n'}:
                                    write(buffer[:pos + 2])
                                    del buffer[:pos + 2]
                                    continue
                            write(buffer[:pos])
                            del buffer[:pos + len(marker) + 2]
                            final = suffix == b'--'
                            break
                        write(buffer[:pos + 2])
                        del buffer[:pos + 2]
                        continue
                    elif len(buffer) > len(marker) + 2:
                        count = len(buffer) - len(marker) - 2
                        write(buffer[:count])
                        del buffer[:count]
                    if not remaining:
                        raise ValueError('上传中断：缺少 multipart 结束边界')
                    fill()
            finally:
                if handle:
                    handle.close()
            if file_part:
                result['files'].append({'filename': filename or 'upload', 'path': str(Path(temporary.name) / str(len(result['files']))),
                                        'size': size, 'sha256': digest.hexdigest(), 'mime_type': headers.get_content_type()})
            elif name in {'text', 'conversation_id', 'idempotency_key', 'batch_id', 'captions', 'modes'}:
                result[name] = content.decode('utf-8', 'strict')
        while remaining:
            fill()
            buffer.clear()
        if not result['files']:
            raise ValueError('缺少文件')
        return result
    except BaseException:
        temporary.cleanup()
        raise
