"""Native-image handoff stays durable across stop and delivery races."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from xgent_app.generated_media import GeneratedMediaReply


class GeneratedMediaReplyTests(unittest.IsolatedAsyncioTestCase):
    def reply(self, artifacts=None):
        extract = Mock(return_value=("clean reply", artifacts if artifacts is not None else [
            {"mime_type": "image/png", "path": "original.png"},
        ]))
        persist = AsyncMock()
        return GeneratedMediaReply(extract, persist), extract, persist

    async def test_extracts_and_persists_once_even_after_delivery_failure(self):
        reply, extract, persist = self.reply()
        first = await reply.prepare("raw image")
        self.assertTrue(reply.recorded)
        second = await reply.prepare("raw image", partial=True)
        self.assertEqual(first, second)
        extract.assert_called_once_with("raw image", partial=False)
        persist.assert_awaited_once_with("clean reply", first[1], False)

    async def test_concurrent_preparation_shares_the_same_handoff(self):
        reply, extract, persist = self.reply()
        results = await asyncio.gather(*(reply.prepare("raw") for _ in range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        extract.assert_called_once()
        persist.assert_awaited_once()

    async def test_text_and_audio_do_not_acquire_image_associations(self):
        for artifacts in ([], [{"mime_type": "audio/wav", "path": "audio.wav"}]):
            reply, _extract, persist = self.reply(artifacts)
            await reply.prepare("raw")
            persist.assert_not_awaited()
            self.assertFalse(reply.recorded)

    async def test_stop_keeps_completed_images_and_passes_partial_flag(self):
        reply, extract, persist = self.reply()
        await reply.prepare("completed image plus incomplete text", stopped=True, partial=True)
        extract.assert_called_once_with("completed image plus incomplete text", partial=True)
        self.assertTrue(persist.await_args.args[2])
        self.assertTrue(reply.recorded)

    async def test_failed_persistence_is_explicit_and_does_not_extract_again(self):
        reply, extract, persist = self.reply()
        persist.side_effect = OSError("disk full")
        for _ in range(2):
            with self.assertRaisesRegex(OSError, "disk full"):
                await reply.prepare("raw")
        self.assertFalse(reply.recorded)
        extract.assert_called_once()
        persist.assert_awaited_once()

    async def test_cancellation_waits_for_completed_image_handoff(self):
        reply, _extract, persist = self.reply()
        started, release = asyncio.Event(), asyncio.Event()

        async def store(*_args):
            started.set()
            await release.wait()

        persist.side_effect = store
        task = asyncio.create_task(reply.prepare("raw"))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(reply.recorded)
        await reply.prepare("raw")
        persist.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
