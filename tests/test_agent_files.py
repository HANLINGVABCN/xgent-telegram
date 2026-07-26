from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from xgent_app.agent_files import (
    write_base64_protocol_file,
    write_text_protocol_file,
)


class AgentFilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_write_preserves_executor_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_text("hello", encoding="utf-8")
            executor = Mock()
            executor.write_file = AsyncMock(
                return_value={"path": str(path), "existed": True}
            )

            result = await write_text_protocol_file(
                {"path": "a.txt", "body": "hello"}, executor=executor
            )

            executor.write_file.assert_awaited_once_with("a.txt", "hello")
            self.assertEqual(result["filename"], "a.txt")
            self.assertEqual(result["size"], 5)
            self.assertTrue(result["existed"])

    async def test_base64_write_accepts_whitespace_and_writes_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.bin"
            executor = Mock()
            executor.resolve_write_path.return_value = str(target)
            payload = base64.b64encode(b"abc\x00").decode("ascii")

            result = await write_base64_protocol_file(
                {"path": "a.bin", "body": payload[:3] + "\n" + payload[3:]},
                executor=executor,
            )

            self.assertEqual(target.read_bytes(), b"abc\x00")
            self.assertEqual(result["size"], 4)
            self.assertFalse(result["existed"])

    async def test_base64_empty_payload_keeps_existing_error(self):
        executor = Mock()
        with self.assertRaisesRegex(ValueError, "base64 内容为空"):
            await write_base64_protocol_file(
                {"path": "a.bin", "body": "  \n"}, executor=executor
            )


if __name__ == "__main__":
    unittest.main()
