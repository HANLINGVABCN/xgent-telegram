from __future__ import annotations

import unittest

from tests.test_external_sync import SectionsProbeMixin


class DurableRecordIntegrationTests(SectionsProbeMixin, unittest.TestCase):
    def test_exports_are_saved_and_recorded_before_delivery(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio, io, zipfile
from pathlib import Path
from unittest.mock import AsyncMock
from xgent_app.web_bridge import WebOutbox, build_web_command_objects

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    await ns["GlobalRecorder"].record_user_message("export fixture")
    await db.import_providers({"test": {
        "base_url": "https://invalid.example/v1", "api_key": "fixture-only-key",
        "models": ["test-model"], "type": "openai",
    }})
    await ns["UserDataManager"].reload_providers()
    await ns["GlobalRecorder"].record_token_usage(
        "token fixture", 1, model="test-model", usage={"input_tokens": 10, "output_tokens": 3},
    )
    ns["check_authorized_user_middleware"] = AsyncMock(return_value=True)
    deliveries, paths = [], []

    for name, command, fail in (
        ("cmd_export_all", "/export", False),
        ("send_provider_config_export", "/provider_config", False),
        ("cmd_token_stats", "/stats", False),
        ("cmd_export_all", "/export", True),
        ("send_provider_config_export", "/provider_config", True),
        ("cmd_token_stats", "/stats", True),
    ):
        update, context, bot = build_web_command_objects(1, WebOutbox(), command, None)
        async def send_document(*args, document=None, filename=None, **kwargs):
            path = Path(document.name)
            history = await ns["_web_read_history"](0)
            found = [m for m in history if any(
                item.get("path") == str(path) and item.get("download_url")
                for item in m["media"]
            )]
            raw = document.read()
            deliveries.append({
                "linked_before_send": len(found) == 1,
                "open_original": path.is_file() and raw == path.read_bytes(),
                "name": filename,
                "zip_valid": zipfile.is_zipfile(io.BytesIO(raw)) if filename.endswith(".zip") else True,
                "json_valid": json.loads(raw)["providers"]["test"]["api_key"] == "fixture-only-key"
                              if filename.endswith(".json") else True,
                "html_valid": b"<html" in raw.lower() if filename.endswith(".html") else True,
            })
            paths.append(str(path))
            if fail:
                raise OSError("fixture delivery failure")
        bot.send_document = send_document
        await ns[name](update, context)

    before = await ns["_web_read_history"](0)
    media_before = [item for m in before for item in m["media"]]
    db_path = db.db_path
    await db.close()
    ns["BotMemoryDB"]._instance = ns["BotMemoryDB"](db_path)
    db = await ns["BotMemoryDB"].get_instance()
    after = await ns["_web_read_history"](0)
    media_after = [item for m in after for item in m["media"]]
    saved_id = next(m["id"] for m in after if m["media"])
    await db.clear_all_conversation_memory()
    print(json.dumps({
        "deliveries": deliveries, "links": len(media_before),
        "restart_equal": media_before == media_after,
        "cleared": await ns["_web_read_history"](0) == [],
        "revoked": await ns["_web_read_history_message"](saved_id) is None,
        "originals_kept": all(Path(path).is_file() for path in paths),
        "no_fake_delivery_success": not any("\u5df2\u6210\u529f\u5bfc\u51fa" in m["content"] for m in before),
    }))
    await db.close()

async def run():
    try:
        await main()
    finally:
        if ns["BotMemoryDB"]._instance:
            await ns["BotMemoryDB"]._instance.close()

asyncio.run(run())
''')
        self.assertEqual(6, len(result["deliveries"]))
        for delivery in result["deliveries"]:
            for key in ("linked_before_send", "open_original", "zip_valid", "json_valid", "html_valid"):
                self.assertTrue(delivery[key], (key, delivery["name"]))
        self.assertEqual(6, result["links"])
        for key in ("restart_equal", "cleared", "revoked", "originals_kept", "no_fake_delivery_success"):
            self.assertTrue(result[key], key)

    def test_generated_mixed_media_has_display_order_without_new_audio_context(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio, io
from PIL import Image

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), "red").save(buffer, format="PNG")
    artifacts = []
    for name, data, mime in (
        ("assistant_audio.wav", b"audio fixture", "audio/wav"),
        ("assistant_image.png", buffer.getvalue(), "image/png"),
        ("assistant_video.mp4", b"video fixture", "video/mp4"),
    ):
        saved = ns["ArtifactManager"].save_generated_media(name, data, mime)
        artifacts.append({"path": saved["abs_path"], "mime_type": mime})
    metadata = await ns["generated_image_metadata"](artifacts, await db.get_attachment_generation())
    await ns["GlobalRecorder"].record_media_reply("mixed media", 1, metadata=metadata)
    message = (await ns["_web_read_history"](0))[-1]
    print(json.dumps({
        "display_order": [item["kind"] for item in message["media"]],
        "unique_paths": len({item["path"] for item in message["media"]}),
        "context_images_only": len(metadata["attachments"]) == 1
                               and metadata["attachments"][0]["mime_type"] == "image/png",
        "all_downloadable": all(item.get("download_url") for item in message["media"]),
    }))
    await db.close()

asyncio.run(main())
''')
        self.assertEqual(["audio", "photo", "video"], result["display_order"])
        self.assertEqual(3, result["unique_paths"])
        self.assertTrue(result["context_images_only"])
        self.assertTrue(result["all_downloadable"])

    def test_clear_command_notifies_web_and_keeps_original(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from xgent_app.web_bridge import WebOutbox, build_web_command_objects

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    saved = ns["ArtifactManager"].save_export("fixture.txt", b"original")
    await ns["GlobalRecorder"].record_system_message("export", 1, metadata={
        "display_media": [ns["display_media_reference"](saved["abs_path"])],
    })
    row_id = (await ns["_web_read_history"](0))[0]["id"]
    outbox = WebOutbox()
    ns["get_web_outbox"] = lambda: outbox
    ns["check_authorized_user_middleware"] = AsyncMock(return_value=True)
    update, context, _ = build_web_command_objects(1, outbox, "/delete", None)
    with outbox.subscribe() as stream:
        await ns["cmd_delete_chat"](update, context)
        frames = []
        while (frame := stream.get(timeout=0)) is not None:
            frames.append(frame)
    print(json.dumps({
        "reset_first": frames[0]["type"] == "history_reset",
        "single_reset": sum(frame["type"] == "history_reset" for frame in frames) == 1,
        "history_empty": await ns["_web_read_history"](0) == [],
        "revoked": await ns["_web_read_history_message"](row_id) is None,
        "original_kept": Path(saved["abs_path"]).read_bytes() == b"original",
    }))
    await db.close()

async def run():
    try:
        await main()
    finally:
        if ns["BotMemoryDB"]._instance:
            await ns["BotMemoryDB"]._instance.close()

asyncio.run(run())
''')
        for key, value in result.items():
            self.assertTrue(value, key)

    def test_clear_rejects_old_display_only_generated_media(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from pathlib import Path

async def main():
    await ns["UserDataManager"].init()
    db = await ns["BotMemoryDB"].get_instance()
    saved = ns["ArtifactManager"].save_generated_media("assistant_audio.wav", b"audio", "audio/wav")
    metadata = await ns["generated_image_metadata"](
        [{"path": saved["abs_path"], "mime_type": "audio/wav"}],
        await db.get_attachment_generation(),
    )
    await db.clear_all_conversation_memory()
    rejected = False
    try:
        await db.record_global_message(
            chat_id=1, user_id=0, msg_type="media_reply", role="media_module",
            content="old result", metadata=metadata,
        )
    except ValueError:
        rejected = True
    print(json.dumps({
        "rejected": rejected, "no_model_attachment": "attachments" not in metadata,
        "history_empty": await ns["_web_read_history"](0) == [],
        "original_kept": Path(saved["abs_path"]).is_file(),
    }))
    await db.close()

async def run():
    try:
        await main()
    finally:
        if ns["BotMemoryDB"]._instance:
            await ns["BotMemoryDB"]._instance.close()

asyncio.run(run())
''')
        for key, value in result.items():
            self.assertTrue(value, key)


if __name__ == "__main__":
    unittest.main()
