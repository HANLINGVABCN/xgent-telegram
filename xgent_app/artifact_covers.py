"""Small, inert list previews. Never ship original media for a cover request."""
from __future__ import annotations

import base64
import io
import os
import shutil
import stat
import subprocess
from functools import lru_cache
from pathlib import Path

TEXT_SUFFIXES = {'.txt', '.md', '.json', '.csv', '.tsv', '.log', '.py', '.js', '.ts', '.css', '.html', '.xml', '.yaml', '.yml', '.toml', '.ini', '.sh', '.sql', '.svg'}


def cover(path, kind, mime=''):
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return {'kind': 'unavailable', 'label': '文件不可读取'}
        info = path.stat()
        return _cached(str(path), info.st_mtime_ns, info.st_size, kind, mime)
    except OSError:
        return {'kind': 'unavailable', 'label': '文件不可读取'}


def video_encoder():
    executable = shutil.which('ffmpeg')
    if executable:
        return executable
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


@lru_cache(maxsize=128)
def _cached(path, modified, size, kind, mime):
    try:
        if kind in {'photo', 'video'} and mime != 'image/svg+xml':
            from PIL import Image, ImageOps
            if kind == 'video':
                ffmpeg = video_encoder()
                if not ffmpeg:
                    return {'kind': 'video', 'label': '视频 · 点击播放', 'poster_available': False}
                # Restrict the demuxer so a disguised playlist cannot read
                # another local file as a secondary input or contact a URL.
                formats = {'.mp4': 'mov', '.m4v': 'mov', '.mov': 'mov', '.3gp': 'mov',
                           '.webm': 'matroska', '.mkv': 'matroska', '.avi': 'avi',
                           '.flv': 'flv', '.ts': 'mpegts', '.mpeg': 'mpeg', '.mpg': 'mpeg',
                           '.ogg': 'ogg', '.ogv': 'ogg'}
                demuxer = formats.get(Path(path).suffix.lower()) or {
                    'video/mp4': 'mov', 'video/quicktime': 'mov', 'video/webm': 'matroska',
                    'video/x-matroska': 'matroska', 'video/x-msvideo': 'avi',
                    'video/mpeg': 'mpeg', 'video/ogg': 'ogg',
                }.get(mime)
                if not demuxer:
                    return {'kind': 'video', 'label': '视频 · 点击播放', 'poster_available': False}
                result = subprocess.run(
                    [ffmpeg, '-v', 'error', '-nostdin', '-threads', '1', '-protocol_whitelist', 'file,pipe',
                     '-f', demuxer, '-i', path, '-frames:v', '1', '-vf', 'scale=320:180:force_original_aspect_ratio=decrease',
                     '-f', 'image2pipe', '-vcodec', 'mjpeg', '-threads', '1', 'pipe:1'],
                    capture_output=True, timeout=4, check=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                image_source = io.BytesIO(result.stdout)
            else:
                image_source = path
            with Image.open(image_source) as image:
                if image.width * image.height > 40_000_000:
                    return {'kind': kind, 'label': '大图 · 点击查看原图'}
                image.draft('RGB', (320, 180))
                image = ImageOps.exif_transpose(image)
                image.thumbnail((320, 180))
                image = image.convert('RGB')
                output = io.BytesIO()
                image.save(output, 'JPEG', quality=42, optimize=True)
                return {'kind': kind, 'width': image.width, 'height': image.height,
                        'image': 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii')}
        if mime.startswith('text/') or Path(path).suffix.lower() in TEXT_SUFFIXES or kind == 'output':
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
            with os.fdopen(fd, 'rb') as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return {'kind': 'unavailable', 'label': '文件不可读取'}
                raw = handle.read(2048)
            encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
            if encoding != 'utf-16' and b'\x00' in raw:
                return {'kind': 'file', 'label': '文件 · 点击查看'}
            return {'kind': 'text', 'text': raw.decode(encoding, errors='replace')[:600], 'truncated': size > len(raw)}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {'kind': kind, 'label': {'video': '视频 · 点击播放', 'photo': '图片 · 点击查看', 'audio': '音频 · 点击播放'}.get(kind, '暂无轻量预览')}
