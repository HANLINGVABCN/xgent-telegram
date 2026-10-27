"""Covers are bounded inert data; original media is loaded only on demand."""
import base64
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from xgent_app.artifact_covers import cover, video_encoder


class ArtifactCoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_image_is_small_reencoded_cover_not_original(self):
        path = self.root / 'original.png'
        Image.new('RGB', (3000, 2000), 'navy').save(path)
        result = cover(path, 'photo', 'image/png')
        raw = base64.b64decode(result['image'].partition(',')[2])
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertLessEqual(image.width, 320)
            self.assertLessEqual(image.height, 180)
        self.assertLess(len(raw), 30000)

    def test_text_snippet_is_bounded_and_html_is_not_executed(self):
        path = self.root / 'test.html'
        path.write_text('<script>bad()</script>' + '中文' * 9000, encoding='utf-8')
        result = cover(path, 'file', 'text/html')
        self.assertEqual(result['kind'], 'text')
        self.assertIn('<script>', result['text'])
        self.assertLessEqual(len(result['text']), 600)
        self.assertTrue(result['truncated'])

    def test_video_without_ffmpeg_is_a_safe_play_placeholder(self):
        path = self.root / 'test.mp4'
        path.write_bytes(b'fake-video')
        with patch('xgent_app.artifact_covers.video_encoder', return_value=None):
            result = cover(path, 'video', 'video/mp4')
        self.assertEqual(result['kind'], 'video')
        self.assertFalse(result['poster_available'])
        self.assertNotIn('image', result)

    def test_changed_file_invalidates_cover(self):
        path = self.root / 'note.txt'
        path.write_text('before', encoding='utf-8')
        self.assertEqual(cover(path, 'file', 'text/plain')['text'], 'before')
        path.write_text('after much longer', encoding='utf-8')
        self.assertEqual(cover(path, 'file', 'text/plain')['text'], 'after much longer')

    def test_unknown_binary_is_not_text_or_base64(self):
        path = self.root / 'data.bin'
        path.write_bytes(b'\0' * 200000)
        result = cover(path, 'file', 'application/octet-stream')
        self.assertNotIn('text', result)
        self.assertNotIn('image', result)

    def test_video_is_a_small_jpeg_cover(self):
        encoder = video_encoder()
        if not encoder:
            self.skipTest('Video encoder unavailable')
        path = self.root / 'clip.mp4'
        subprocess.run([encoder, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'color=c=green:s=640x360:r=5:d=1', '-an', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True, timeout=20)
        result = cover(path, 'video', 'video/mp4')
        raw = base64.b64decode(result['image'].partition(',')[2])
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertLessEqual(image.width, 320)
            self.assertLessEqual(image.height, 180)
