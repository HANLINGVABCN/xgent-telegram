"""Lossless, request-local media file parsing and preparation."""

import asyncio
import base64
import io
import os
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from tests.test_attachments import image_bytes
from xgent_app import media_inputs
from xgent_app.context_limits import limits_from_model_metadata, validate_limits
from xgent_app.media_inputs import (
    MediaInputError,
    load_media_files,
    native_binary_kind,
    parse_media_request,
    redact_media_data,
)
from xgent_app.protocols import ProtocolParser

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + bytes(413)
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00\x00\x00\x08mdat"


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        writer.writeframes(bytes(1600))
    return buffer.getvalue()


class MediaParserTests(unittest.TestCase):
    def test_legacy_and_structured_prompt_only(self):
        self.assertEqual("draw a garden", parse_media_request("\n draw a garden\n").prompt)
        self.assertEqual((), parse_media_request("draw\nfile: is text here").files)
        parsed = parse_media_request("\n PROMPT \uff1a edit image\nKeep all other details.\n")
        self.assertEqual("edit image\nKeep all other details.\n", parsed.prompt)
        self.assertEqual((), parsed.files)

    def test_paths_colons_quotes_order_and_explicit_duplicates(self):
        windows = "C:\\images\\\u6d4b\u8bd5 image.png"
        unix = "/data/\u53c2\u8003 image.png"
        body = f' file : "{windows}"\nfile\uff1a \'{unix}\'\nfile: {windows}\nprompt: edit\n'
        self.assertEqual((windows, unix, windows), parse_media_request(body).files)

    def test_prompt_tail_is_opaque(self):
        tail = "edit\nfile: not a path\nprompt: literal\n```run-x\n<<BEGIN_nested_123\nx\n<<END_nested_123\n```"
        result = parse_media_request("file: /data/a.png\nprompt: " + tail)
        self.assertEqual(tail, result.prompt)
        self.assertEqual(("/data/a.png",), result.files)

    def test_invalid_fields_fail_instead_of_becoming_a_prompt(self):
        cases = (
            "", " \n", "file: /data/x", "file:\nprompt: edit", "prompt: \n \n",
            "file: /data/x\nunknown: value\nprompt: edit",
            "file: /data/x\nedit this image", "file: relative.png\nprompt: edit",
            "file: https://example.invalid/a.png\nprompt: edit",
            "file: /data/*.png\nprompt: edit", 'file: "/data/x\nprompt: edit',
            "file: /data/a\x00.png\nprompt: edit", "file: ''\nprompt: edit",
        )
        for body in cases:
            with self.subTest(body=body), self.assertRaises(MediaInputError):
                parse_media_request(body)

    def test_unclosed_outer_protocol_is_not_executable(self):
        for ending in ("", "\n<<END_media_test_123", "\n<<END_wrong_tag\n```"):
            body = "```media-x\n<<BEGIN_media_test_123\nfile: /data/x\nprompt: edit" + ending
            self.assertEqual([], ProtocolParser.extract_protocol_blocks(body))
        complete = "```media-x\n<<BEGIN_media_test_123\nfile: /data/x\nprompt: edit\n<<END_media_test_123\n```"
        blocks = ProtocolParser.extract_protocol_blocks(complete)
        self.assertEqual(1, len(blocks))
        self.assertEqual(("/data/x",), parse_media_request(blocks[0]["body"]).files)


class MediaFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def save(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return str(path)

    async def test_originals_true_mime_order_duplicates_and_full_text(self):
        image = image_bytes("PNG", (10, 20, 30))
        image_path = self.save("\u56fe\u50cf wrong.txt", image)
        text = "  TEXT-START\r\n" + "\u5b8c\u6574\u6587\u672c\n" * 120000 + "TEXT-TAIL \t\r\n"
        text_path = self.save("\u5168\u6587.txt", text.encode("utf-16"))
        parts, metadata = await load_media_files([image_path, text_path, image_path], "openai", {})
        self.assertEqual([1, 2, 3], [item["order"] for item in metadata])
        self.assertEqual(["image/png", "text/plain", "image/png"], [item["mime_type"] for item in metadata])
        self.assertEqual([image, image], [base64.b64decode(p["data"]) for p in parts if p["type"] == "image"])
        self.assertIn(text, "\n".join(p["text"] for p in parts if p["type"] == "text"))
        self.assertTrue(all("data" not in item for item in metadata))
        self.assertEqual(image, Path(image_path).read_bytes())

    async def test_large_image_has_no_read_x_limit(self):
        buffer = io.BytesIO()
        Image.frombytes("RGB", (1750, 1750), os.urandom(1750 * 1750 * 3)).save(buffer, format="PNG")
        data = buffer.getvalue()
        self.assertGreater(len(data), 8 * 1024 * 1024)
        path = self.save("large.png", data)
        parts, _ = await load_media_files([path], "claude", {})
        self.assertEqual(data, base64.b64decode(parts[-1]["data"]))

    async def test_native_type_matrix_and_original_bytes(self):
        for name, data, mime, supported in (
            ("document.bin", PDF, "application/pdf", ("openai", "openai_compatible", "claude", "gemini", "vertex")),
            ("voice.wav", wav_bytes(), "audio/x-wav", ("openai", "openai_compatible", "gemini", "vertex")),
            ("voice.mp3", MP3, "audio/mpeg", ("openai", "openai_compatible", "gemini", "vertex")),
            ("video.mp4", MP4, "video/mp4", ("gemini", "vertex")),
            ("raw.bin", b"\x00\x02\xff\x00", "application/octet-stream", ("gemini", "vertex")),
        ):
            path = self.save(name, data)
            for fmt in ("openai", "openai_compatible", "claude", "gemini", "vertex"):
                with self.subTest(name=name, fmt=fmt):
                    if fmt not in supported:
                        with self.assertRaisesRegex(MediaInputError, "File #1"):
                            await load_media_files([path], fmt, {})
                    else:
                        parts, metadata = await load_media_files([path], fmt, {})
                        self.assertEqual(data, base64.b64decode(parts[-1]["data"]))
                        self.assertEqual(mime, metadata[0]["mime_type"])
                        self.assertTrue(native_binary_kind(fmt, mime))

    async def test_failed_file_reports_index_path_and_reason(self):
        valid = self.save("valid.txt", b"complete reference")
        cases = [str(self.root / "missing.png"), str(self.root),
                 self.save("broken.png", b"\x89PNG\r\n\x1a\ninvalid image"),
                 self.save("broken.txt", b"\x00\x00\x00\x01")]
        for path in cases:
            with self.subTest(path=path), self.assertRaises(MediaInputError) as error:
                await load_media_files([valid, path], "gemini", {})
            self.assertIn("File #2", str(error.exception))
            self.assertIn(path, str(error.exception))

    async def test_model_file_limits_and_image_capability(self):
        path = self.save("reference.png", image_bytes("PNG", (10, 20, 30)))
        for limits in ({"max_files": 0}, {"max_file_bytes": 1}, {"supports_images": False}):
            with self.subTest(limits=limits), self.assertRaises(MediaInputError):
                await load_media_files([path], "openai", limits)

    async def test_file_changed_while_reading_is_rejected(self):
        path = self.save("changing.txt", b"complete reference")
        before = os.stat(path)
        after = SimpleNamespace(st_mode=before.st_mode, st_size=before.st_size + 1,
                                st_mtime_ns=before.st_mtime_ns + 1)
        with (patch.object(media_inputs.os, "fstat", side_effect=[before, after]),
              self.assertRaisesRegex(MediaInputError, "File changed")):
            await load_media_files([path], "openai", {})

    async def test_stop_during_loading_never_loads_next_file(self):
        path = self.save("reference.txt", b"file content")
        started, release = threading.Event(), threading.Event()
        stopped = False
        read = media_inputs._read_file

        def slow_read(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test timed out")
            return read(*args)

        with patch.object(media_inputs, "_read_file", side_effect=slow_read) as loader:
            task = asyncio.create_task(load_media_files([path, path], "openai", {}, lambda: stopped))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                stopped = True
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(1, loader.call_count)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    async def test_fifo_is_rejected_without_blocking(self):
        path = self.root / "pipe"
        os.mkfifo(path)
        with self.assertRaises(MediaInputError):
            await asyncio.wait_for(load_media_files([str(path)], "gemini", {}), 2)

    def test_error_redaction_omits_known_and_inline_payloads(self):
        self.assertNotIn("YWJjZA==", redact_media_data("bad YWJjZA==", [{"data": "YWJjZA=="}]))
        self.assertNotIn("data:application/pdf", redact_media_data("data:application/pdf;base64,YWJjZA=="))

    def test_file_limit_configuration_validation_and_discovery(self):
        self.assertEqual({"max_files": 0, "max_file_bytes": 1024},
                         limits_from_model_metadata({"max_files": 0, "max_file_bytes": 1024}))
        self.assertEqual({"max_files": 2}, validate_limits({"max_files": 2}))
        for limits in ({"max_files": -1}, {"max_files": True}, {"max_file_bytes": 0}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                validate_limits(limits)


if __name__ == "__main__":
    unittest.main()
