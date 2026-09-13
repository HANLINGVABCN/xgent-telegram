"""Runtime scenarios run in isolated processes by test_attachment_requests."""

import asyncio
import base64
import contextlib
import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI

from tests.test_attachments import image_bytes
from xgent_app.attachments import AttachmentContextError, prepare_attachment_context
from xgent_app.context_limits import estimate_input_tokens


FORMATS = ("openai", "openai_compatible", "gemini", "vertex", "claude")
MODEL = "vision-model"
URL = "https://provider.invalid/v1"
LONG_TEXT = "START\r\n" + ("complete file content, no clipping.\n" * 6000) + "LONG-TEXT-TAIL  \r\n"
CAPTION = " \n" + ("full caption " * 90) + "CAPTION-TAIL \n"
SECOND_TEXT = "\u4e2d\u6587 UTF-16 text\n" * 1100 + "SECOND-FILE-TAIL"


def expected_images():
    return [
        image_bytes(("PNG", "JPEG", "WEBP")[index % 3], (index * 10, 80, 200))
        for index in range(12)
    ]


def unpack_request(body):
    texts, images, mimes = [], [], []
    messages = body.get("messages") or body.get("contents") or []
    for message in messages:
        content = message.get("content", message.get("parts", []))
        if isinstance(content, str):
            texts.append(content)
            continue
        for part in content:
            if "text" in part:
                texts.append(part["text"])
            if "image_url" in part:
                header, data = part["image_url"]["url"].split(",", 1)
                mimes.append(header[5:].split(";")[0])
                images.append(base64.b64decode(data))
            elif "inline_data" in part:
                mimes.append(part["inline_data"]["mime_type"])
                images.append(base64.b64decode(part["inline_data"]["data"]))
            elif part.get("type") == "image":
                mimes.append(part["source"]["media_type"])
                images.append(base64.b64decode(part["source"]["data"]))
    return texts, images, mimes


def assert_complete(body, question=None):
    texts, images, mimes = unpack_request(body)
    joined = "\n".join(texts)
    assert joined.count(LONG_TEXT) == 1, "long text absent, truncated, or duplicated"
    assert joined.count(SECOND_TEXT) == 1, "second text absent or duplicated"
    assert CAPTION in joined, "caption truncated"
    assert images == expected_images(), "original image bytes/order/count differ"
    assert mimes == [("image/png", "image/jpeg", "image/webp")[i % 3] for i in range(12)]
    assert "_conversation_attachments" not in json.dumps(body)
    if question is not None:
        assert texts[-1] == question, (question, texts[-1][:100])
    return {"images": len(images), "text_tail": True, "caption": True, "current_question": True}


class Harness:
    def __init__(self, bot, root):
        self.bot = bot
        self.root = Path(root)
        self.requests = []
        self.request_bytes = []
        self.replies = []
        self.error = None
        self.errors = []
        self.sse_error = None
        self.force_sse = False
        self.empty_stream = False
        self.stack = contextlib.ExitStack()

    async def __aenter__(self):
        bot = self.bot
        self.root.mkdir(parents=True, exist_ok=True)
        self.stack.enter_context(patch.object(bot.BotConfig, "DB_FILE", str(self.root / "memory.db")))
        self.stack.enter_context(patch.object(bot.ArtifactManager, "ROOT_DIR", str(self.root / "storage")))
        self.stack.enter_context(patch.object(bot.ArtifactManager, "UPLOAD_DIR",
                                             str(self.root / "storage" / "uploads")))
        self.stack.enter_context(patch.object(bot.ArtifactManager, "GENERATED_MEDIA_DIR",
                                             str(self.root / "storage" / "generated_media")))
        self.stack.enter_context(patch.object(bot.PromptFileManager, "PROMPTS_DIR",
                                             str(self.root / "prompts")))
        bot.BotMemoryDB._instance = None
        bot.BotMemoryDB._lock = asyncio.Lock()
        bot.UserDataManager._initialized = False
        bot.UserDataManager._db = None
        bot.UserDataManager._data = {}
        bot.UserDataManager._init_lock = asyncio.Lock()
        bot.ModelClient._discovered_model_limits = {}
        bot.ModelClient._thinking_unsupported.clear()
        self.db = await bot.BotMemoryDB.get_instance()
        await self.db.save_provider("p", URL, "test-key", [MODEL], api_format="openai")
        for name, value in (
            ("active_provider", "p"), ("default_model", MODEL), ("global_depth", 2),
            ("agent_mode", False), ("stream_mode", False), ("thinking_level", "auto"),
        ):
            await self.db.set_config(name, value)
        await bot.UserDataManager.init()
        bot.PromptFileManager.init()
        transport = httpx.MockTransport(self.respond)
        self.http = httpx.AsyncClient(transport=transport)
        self.sdk = AsyncOpenAI(
            api_key="test-key", base_url=URL, max_retries=0,
            http_client=httpx.AsyncClient(transport=transport),
        )
        self.stack.enter_context(patch.object(bot.ModelClient, "_http_client", self.http))
        self.stack.enter_context(patch.object(bot.PortalManager, "get_portal", return_value=self.sdk))
        self.outbox = bot.WebOutbox()
        self.subscription = self.stack.enter_context(self.outbox.subscribe())
        self.update, self.context, _ = bot.build_web_conversation_objects(1, self.outbox)
        return self

    async def __aexit__(self, *exc):
        self.bot.cancel_pending_album_conversations()
        await self.http.aclose()
        await self.sdk.close()
        await self.db.close()
        self.bot.BotMemoryDB._instance = None
        self.bot.UserDataManager._initialized = False
        self.bot.UserDataManager._db = None
        self.stack.close()

    def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.request_bytes.append(request.content)
        error = self.errors.pop(0) if self.errors else self.error
        if error:
            return httpx.Response(error[0], json={"error": {"message": error[1]}})
        text = self.replies.pop(0) if self.replies else "ok"
        gemini = {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
        if self.force_sse or body.get("stream") or "streamGenerateContent" in str(request.url):
            data = {
                "id": "test", "object": "chat.completion.chunk", "created": 1, "model": MODEL,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "candidates": [gemini], "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": text},
            }
            events = "" if self.empty_stream else "data: " + json.dumps(data) + "\n\n"
            if self.sse_error:
                events += "event: error\ndata: " + json.dumps({
                    "type": "error", "error": {
                        "message": self.sse_error, "type": "invalid_request_error",
                        "code": "context_length_exceeded",
                    },
                }) + "\n\n"
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=events + "data: [DONE]\n\n",
            )
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "candidates": [gemini], "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        })

    async def add_upload(self, data, name, caption="", **metadata):
        saved = self.bot.ArtifactManager.save_binary_upload(name, data)
        ref = self.bot.ArtifactManager.attachment_reference(saved, name, data, caption)
        index = self.bot.ArtifactManager.build_index_message(
            "image" if ref["kind"] == "image" else "file", name, saved["rel_path"],
        )
        await self.bot.GlobalRecorder.record_attachment_message(
            index, self.bot.MessageType.USER_FILE, 1, [ref], metadata=metadata,
        )
        return ref

    async def seed(self):
        await self.add_upload(LONG_TEXT.encode(), "long.txt", CAPTION)
        await self.add_upload(SECOND_TEXT.encode("utf-16"), "unicode.txt")
        for index, data in enumerate(expected_images()):
            ref = await self.add_upload(data, f"original-{index}.bin")
        await self.bot.GlobalRecorder.record_attachment_message(
            "[duplicate association]", self.bot.MessageType.USER_FILE, 1, [ref],
        )

    async def history(self, question):
        return self.bot.with_current_question(await self.db.get_conversation_messages(2), question)

    async def call(self, fmt="openai", stream=False, question="current question", history=None):
        history = await self.history(question) if history is None else history
        args = ("p", "test-key", URL, MODEL, "system instructions", history)
        kwargs = {"api_format": fmt, "conversation_context": True}
        if stream:
            text = "".join([part async for part in self.bot.ModelClient.think_and_reply_stream(
                *args, **kwargs,
            )])
            return text, None
        return await self.bot.ModelClient.think_and_reply(*args, **kwargs)

    async def turn(self, question):
        await self.bot.GlobalRecorder.record_user_message(question, chat_id=1)
        await self.bot.process_conversation(self.update, self.context, question)

    def drain_frames(self):
        frames = []
        while (frame := self.subscription.get(timeout=0)) is not None:
            frames.append(frame)
        return frames


def upload_update(h, kind, message_id, data, group=None, caption=CAPTION, name=None):
    update, context, _ = h.bot.build_web_conversation_objects(1, h.outbox)
    msg = update.message
    msg.message_id = message_id
    msg.caption = caption
    msg.media_group_id = group
    if kind == "photo":
        msg.photo = [SimpleNamespace(data=data)]
    else:
        msg.document = SimpleNamespace(
            data=data, file_name=name or f"upload-{message_id}.bin",
            mime_type="application/octet-stream", file_size=len(data),
        )
    return update, context


async def seed_and_check(bot, root):
    result = {"providers": {}}
    async with Harness(bot, root) as h:
        await h.seed()
        for turn in (2, 40):
            for i in range(turn):
                await bot.GlobalRecorder.record_user_message(f"ordinary turn {i}", chat_id=1)
                await bot.GlobalRecorder.record_ai_reply("ordinary reply", chat_id=1)
            assert len(await h.db.get_conversation_messages(2)) == 2
            for fmt in FORMATS:
                for stream in (False, True):
                    question = f"turn {turn}, {fmt}, stream={stream}"
                    response, error = await h.call(fmt, stream, question)
                    assert response == "ok" and error is None, (fmt, response, error)
                    result["providers"][f"{turn}/{fmt}/{stream}"] = assert_complete(
                        h.requests[-1], question,
                    )
        result["agent_off"] = bot.UserDataManager.get("agent_mode") is False
        assert "Conversation attachments" in bot.build_conversation_system_prompt(False)
        records = await h.db.get_attachment_records()
        display = await h.db.get_display_history(1000)
        serialized = json.dumps([records, display])
        assert "LONG-TEXT-TAIL" not in serialized
        assert base64.b64encode(expected_images()[0]).decode() not in serialized
        result["index_only_storage"] = True
        await bot.cmd_export_all(h.update, h.context)
        exports = list((h.root / "storage" / "exports").rglob("*.zip"))
        assert exports
        with zipfile.ZipFile(exports[-1]) as archive:
            exported = b"\n".join(archive.read(name) for name in archive.namelist())
        assert b"LONG-TEXT-TAIL" not in exported
        assert base64.b64encode(expected_images()[0]) not in exported
        result["index_only_export"] = True
    return result


async def after_restart(bot, root):
    async with Harness(bot, root) as h:
        assert len(await h.db.get_attachment_records()) == 15
        result = {}
        for fmt in FORMATS:
            for stream in (False, True):
                response, error = await h.call(fmt, stream, "after process restart")
                assert response == "ok" and error is None
                result[f"{fmt}/{stream}"] = assert_complete(h.requests[-1], "after process restart")
        return result


async def check_runtime_paths(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        await h.seed()
        for stream, style in ((False, "foreground"), (True, "foreground"), (True, "background")):
            bot.UserDataManager.set("stream_mode", stream)
            bot.UserDataManager.set("stream_style", style)
            before = len(h.requests)
            await h.turn(f"renderer {stream}/{style}")
            assert len(h.requests) == before + 1
            assert_complete(h.requests[-1], f"renderer {stream}/{style}")
        result["all_renderers"] = True
        bot.UserDataManager.set("stream_mode", False)
        bot.UserDataManager.set("agent_mode", True)
        h.replies = [
            "```run-x\n<<BEGIN_attachment_probe\nmock-operation\n<<END_attachment_probe\n```",
            "ok",
        ]
        fake_run = AsyncMock(return_value={
            "success": True, "output": "TOOL-RESULT-327", "command": "mock-operation", "exit_code": 0,
        })
        with patch.object(bot.AgentExecutor, "run_command", fake_run), patch.object(
            bot.AgentExecutor, "read_path_for_model", AsyncMock(side_effect=AssertionError("no reads")),
        ):
            before = len(h.requests)
            await h.turn("Agent continuation")
        fake_run.assert_awaited_once()
        assert len(h.requests) == before + 2, len(h.requests) - before
        for body in h.requests[before:]:
            assert_complete(body)
        assert "TOOL-RESULT-327" in "\n".join(unpack_request(h.requests[-1])[0])
        result["agent_continuation"] = True

        bot.UserDataManager.set("agent_mode", False)
        original = await h.db.get_attachment_records()
        old_history = await bot.build_model_conversation_history(await h.history("before clear"))
        generation = await h.db.get_attachment_generation()
        refs = [ref for row in original for ref in json.loads(row["metadata"])["attachments"]]
        await bot.cmd_delete_chat(h.update, h.context)
        assert not await h.db.get_attachment_records()
        assert all((Path(bot.ArtifactManager.UPLOAD_DIR) / ref["path"]).is_file() for ref in refs)
        response, error = await h.call(history=old_history)
        assert response == "ok" and error is None
        texts, images, _ = unpack_request(h.requests[-1])
        assert not images and "LONG-TEXT-TAIL" not in "\n".join(texts)
        result["clear_unlinks_without_deleting"] = True
        try:
            await bot.GlobalRecorder.record_attachment_message(
                "[late upload]", bot.MessageType.USER_FILE, 1, [refs[0]], generation,
            )
        except AttachmentContextError:
            result["clear_rejects_inflight_upload"] = True
        else:
            raise AssertionError("upload from before clear was resurrected")
        assert not await h.db.get_attachment_records()
    return result


async def check_failures(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        await h.seed()
        for limits in (
            {"context_window": 100}, {"max_input_tokens": 100}, {"max_images": 2},
            {"max_request_bytes": 1000}, {"supports_images": False},
            {"context_window": "invalid"},
        ):
            bot.UserDataManager.set("model_request_limits", {f"p/{MODEL}": limits})
            for fmt in FORMATS:
                for stream in (False, True):
                    before = len(h.requests)
                    try:
                        await h.call(fmt, stream)
                    except AttachmentContextError:
                        pass
                    else:
                        raise AssertionError((fmt, stream, "invalid full request was sent", limits))
                    assert len(h.requests) == before
        result["all_provider_preflight_failures"] = True
        bot.UserDataManager.set("model_request_limits", {})
        await h.call()
        assert_complete(h.requests[-1], "current question")
        result["unknown_capacity_sends_everything"] = True

        for fmt in FORMATS:
            for stream in (False, True):
                h.error = (413, "full request exceeds request size or context capacity")
                before = len(h.requests)
                try:
                    await h.call(fmt, stream)
                except AttachmentContextError as exc:
                    assert "full request exceeds" in str(exc), (fmt, stream, str(exc))
                else:
                    raise AssertionError((fmt, stream, "HTTP error was treated as an AI answer"))
                assert len(h.requests) == before + 1
                assert_complete(h.requests[-1], "current question")
        h.error = None
        result["upstream_capacity_errors_do_not_shrink"] = True

        row = (await h.db.get_attachment_records())[0]
        ref = json.loads(row["metadata"])["attachments"][0]
        path = Path(bot.ArtifactManager.UPLOAD_DIR) / ref["path"]
        original = path.read_bytes()
        for changed in (None, b"changed original"):
            if changed is None:
                path.unlink()
            else:
                path.write_bytes(changed)
            before = len(h.requests)
            try:
                await h.call()
            except AttachmentContextError as exc:
                assert "long.txt" in str(exc)
            else:
                raise AssertionError("missing/changed original was silently omitted")
            assert len(h.requests) == before
            path.write_bytes(original)
        result["missing_and_changed_originals_block"] = True

        with patch.object(h.db, "record_global_message", AsyncMock(side_effect=OSError("disk full"))):
            try:
                await bot.GlobalRecorder.record_attachment_message(
                    "[upload]", bot.MessageType.USER_FILE, 1, [ref],
                )
            except AttachmentContextError:
                result["persistence_failure_blocks"] = True
            else:
                raise AssertionError("attachment persistence failure was ignored")

        bot.UserDataManager.set("model_request_limits", {f"p/{MODEL}": {"max_images": 0}})
        before = len(h.requests)
        history_before = await h.db.get_global_messages(1000)
        for stream, style in ((False, "foreground"), (True, "foreground"), (True, "background")):
            bot.UserDataManager.set("stream_mode", stream)
            bot.UserDataManager.set("stream_style", style)
            await h.turn("preflight failure must not be recorded as an AI answer")
        history_after = await h.db.get_global_messages(1000)
        assert len(h.requests) == before
        assert sum(row["msg_type"] == bot.MessageType.AI_REPLY for row in history_before) == sum(
            row["msg_type"] == bot.MessageType.AI_REPLY for row in history_after
        )
        result["renderers_do_not_record_success_on_failure"] = True
    return result


async def check_migration(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        saved = bot.ArtifactManager.save_binary_upload("legacy.txt", LONG_TEXT.encode())
        index = bot.ArtifactManager.build_index_message("\u6587\u4ef6", "legacy.txt", saved["rel_path"])
        await bot.GlobalRecorder.record_user_message(index, bot.MessageType.USER_FILE, 1)
        await h.call()
        texts, _, _ = unpack_request(h.requests[-1])
        assert LONG_TEXT in "\n".join(texts)
        migrated = (await h.db.get_attachment_records())[0]
        assert json.loads(migrated["metadata"])["attachments"][0]["legacy"]
        result["trusted_legacy_backfilled"] = True

        await h.db.clear_all_conversation_memory()
        await bot.GlobalRecorder.record_user_message(index, bot.MessageType.USER_FILE, 1)
        records = await h.db.get_attachment_records()
        _, updates, errors = prepare_attachment_context(records, bot.ArtifactManager.UPLOAD_DIR)
        assert not errors and updates
        await h.db.clear_all_conversation_memory()
        rowid, previous, metadata = updates[0]
        assert not await h.db.backfill_attachment_metadata(rowid, previous, metadata)
        assert not await h.db.get_attachment_records()
        result["migration_cannot_revive_cleared_records"] = True

        await bot.GlobalRecorder.record_user_message(
            "[\u56fe\u7247]: unlinked old upload", bot.MessageType.USER_PHOTO, 1,
        )
        before = len(h.requests)
        try:
            await h.call()
        except AttachmentContextError as exc:
            assert "Legacy upload" in str(exc)
            result["unrecoverable_legacy_reported"] = True
        else:
            raise AssertionError("unlinked legacy image was silently ignored")
        assert len(h.requests) == before

        await h.db.clear_all_conversation_memory()
        await bot.ModelClient._remember_model_limits(URL, [
            {"id": MODEL, "context_length": 1000000,
             "architecture": {"input_modalities": ["text", "image"]}},
        ])
        assert (await h.db.get_config("discovered_model_limits"))[f"{URL}|{MODEL}"]["context_window"] == 1000000
        bot.ModelClient._discovered_model_limits.clear()
        await bot.UserDataManager._load_from_db()
        await h.call()
        assert h.requests[-1]["max_tokens"] == 4096
        result["discovered_limits_persist"] = True
    return result


async def check_upload_entrypoints(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        png, webp, jpeg = image_bytes("PNG"), image_bytes("WEBP"), image_bytes("JPEG")
        msg = h.update.message
        msg.caption = CAPTION
        msg.document = SimpleNamespace(file_name="photo.bin", mime_type="application/octet-stream",
                                       file_size=len(png))
        with patch.object(bot, "download_telegram_file", AsyncMock(return_value=png)):
            await bot.handle_document_message(h.update, h.context)
        assert unpack_request(h.requests[-1])[1] == [png]
        result["telegram_image_document"] = True

        msg.photo = [object()]
        msg.media_group_id = None
        with patch.object(bot, "download_telegram_file", AsyncMock(return_value=webp)):
            await bot.handle_photo_message(h.update, h.context)
        assert unpack_request(h.requests[-1])[1] == [png, webp]
        result["telegram_photo"] = True

        with patch.object(bot, "build_web_mirror_objects", side_effect=lambda chat_id, outbox, *a, **k:
                          bot.build_web_conversation_objects(chat_id, outbox)), patch.object(
            bot, "_web_deliver_file_to_tg", AsyncMock(),
        ):
            await bot._web_run_photo_conversation("misnamed.png", jpeg, CAPTION, h.outbox)
            await bot._web_run_file_conversation("image.unknown", png, CAPTION, h.outbox)
        assert unpack_request(h.requests[-1])[1] == [png, webp, jpeg, png]
        result["web_photo_and_image_document"] = True

        await h.db.clear_all_conversation_memory()
        updates = []
        for message_id in (103, 101, 102):
            update, context, _ = bot.build_web_conversation_objects(1, h.outbox)
            update.message.message_id = message_id
            update.message.caption = f"caption-{message_id}"
            update.message.media_group_id = "album-test"
            update.message.photo = [SimpleNamespace(
                data=image_bytes("PNG", (message_id, 20, 30)),
                delay={103: 0, 101: 0.05, 102: 0.02}[message_id],
            )]
            updates.append((update, context))

        async def download(photo):
            await asyncio.sleep(photo.delay)
            return photo.data

        before = len(h.requests)
        with patch.object(bot, "download_telegram_file", download), patch.object(
            bot, "ALBUM_FLUSH_QUIET_SECONDS", 60,
        ):
            await asyncio.gather(*(bot.handle_photo_message(update, context) for update, context in updates))
            assert len(h.requests) == before
            assert len(await h.db.get_attachment_records()) == 3
            await bot.handle_photo_message(*updates[0])
            assert len(await h.db.get_attachment_records()) == 3
            pending = bot._pending_album_conversations[(1, "album-test")]
            await bot.flush_album_conversation((1, "album-test"))
            await asyncio.gather(pending.flush_task, return_exceptions=True)
            assert pending.flush_task.done()
        assert len(h.requests) == before + 1
        texts, images, _ = unpack_request(h.requests[-1])
        assert images == [image_bytes("PNG", (i, 20, 30)) for i in (101, 102, 103)]
        assert all(f"caption-{i}" in "\n".join(texts) for i in (101, 102, 103))
        result["album_persisted_before_flush_ordered_and_deduplicated"] = True
    return result


async def check_document_groups(bot, root):
    async with Harness(bot, root) as h:
        png, jpeg = image_bytes("PNG"), image_bytes("JPEG")
        contents = {201: png, 202: LONG_TEXT.encode(), 203: jpeg}
        updates = [upload_update(h, "document", i, contents[i], "documents")
                   for i in (203, 201, 202)]
        delays = {"upload-201.bin": 0.04, "upload-202.bin": 0.02, "upload-203.bin": 0}

        async def download(doc):
            await asyncio.sleep(delays[doc.file_name])
            return doc.data

        with patch.object(bot, "download_telegram_file", download), patch.object(
            bot, "ALBUM_FLUSH_QUIET_SECONDS", 60,
        ):
            await asyncio.gather(*(bot.handle_document_message(*pair) for pair in updates))
            assert len(await h.db.get_attachment_records()) == 3
            assert not h.requests
            await bot.handle_document_message(*updates[0])
            assert len(await h.db.get_attachment_records()) == 3
            await bot.flush_album_conversation((1, "documents"))
        assert len(h.requests) == 1
        texts, images, mimes = unpack_request(h.requests[-1])
        joined = "\n".join(texts)
        assert images == [png, jpeg] and mimes == ["image/png", "image/jpeg"]
        assert LONG_TEXT in joined and CAPTION in joined
        assert joined.index("[Attachment: upload-201") < joined.index("[Attachment: upload-202")
        assert joined.index("[Attachment: upload-202") < joined.index("[Attachment: upload-203")
        return {"document_group_order_and_full_content": True}


async def check_concurrent_upload_order(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        first, second = image_bytes("PNG"), image_bytes("JPEG")
        for kind in ("photo", "document"):
            await h.db.clear_all_conversation_memory()
            started, release = asyncio.Event(), asyncio.Event()

            async def download(item):
                if item.data == first:
                    started.set()
                    await release.wait()
                return item.data

            handler = bot.handle_photo_message if kind == "photo" else bot.handle_document_message
            with patch.object(bot, "download_telegram_file", download), patch.object(
                bot, "process_conversation", AsyncMock(),
            ):
                task = asyncio.create_task(handler(*upload_update(h, kind, 101, first)))
                try:
                    await asyncio.wait_for(started.wait(), 10)
                    await handler(*upload_update(h, kind, 102, second))
                finally:
                    release.set()
                    await asyncio.wait_for(task, 10)
            rows = await h.db.get_attachment_records()
            refs = [json.loads(row["metadata"])["attachments"][0] for row in rows]
            assert [ref["source_message_id"] for ref in refs] == [102, 101]
            await h.call()
            assert unpack_request(h.requests[-1])[1] == [first, second]
            result[kind] = True

        await h.db.clear_all_conversation_memory()
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        build_photo = bot.build_photo_multimodal_payload

        def slow_photo(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10), "test did not release photo persistence"
            return build_photo(*args, **kwargs)

        with patch.object(bot, "build_photo_multimodal_payload", slow_photo), patch.object(
            bot, "build_web_mirror_objects", side_effect=lambda chat_id, outbox, *a, **k:
            bot.build_web_conversation_objects(chat_id, outbox),
        ), patch.object(bot, "_web_deliver_file_to_tg", AsyncMock()), patch.object(
            bot, "process_conversation", AsyncMock(),
        ):
            task = asyncio.create_task(bot._web_run_photo_conversation(
                "first.png", first, CAPTION, h.outbox,
            ))
            try:
                await asyncio.wait_for(started.wait(), 10)
                await bot._web_run_file_conversation("second.txt", LONG_TEXT.encode(), "", h.outbox)
            finally:
                release.set()
                await asyncio.wait_for(task, 10)
        rows = await h.db.get_attachment_records()
        assert [json.loads(row["metadata"])["attachments"][0]["name"] for row in rows] == [
            "second.txt", "first.png",
        ]
        await h.call()
        texts, images, _ = unpack_request(h.requests[-1])
        joined = "\n".join(texts)
        assert joined.index("[Attachment: first.png]") < joined.index("[Attachment: second.txt]")
        assert images == [first] and LONG_TEXT in joined
        result["web_photo_and_text"] = True
    return result


async def check_idle_requests(bot, root):
    async with Harness(bot, root) as h:
        await h.seed()
        bot.UserDataManager.set("idle_message_interval", 3600)
        with patch.object(
            h.db, "get_last_user_message_time", AsyncMock(return_value=bot.time.time() - 7200),
        ):
            await bot.check_and_send_idle_message(h.context)
            assert len(h.requests) == 1
            assert_complete(h.requests[-1])
            await h.db.set_config("last_idle_notice_time", 0)
            h.drain_frames()
            h.error = (413, "payload too large")
            await bot.check_and_send_idle_message(h.context)
            assert len(h.requests) == 2
            assert_complete(h.requests[-1])
            frames = json.dumps(h.drain_frames(), ensure_ascii=False)
            assert "\u7a7a\u95f2\u63d0\u9192\u5931\u8d25" in frames
            assert "payload too large" in frames
            assert "\u672a\u8c03\u7528\u6a21\u578b" not in frames
            rows = await h.db.get_conversation_messages(100)
            assert not any("payload too large" in row["content"] for row in rows)
        return {"full_idle_request": True, "upstream_failure_reported": True}


async def seed_pending_albums(bot, root):
    async with Harness(bot, root) as h:
        with patch.object(bot, "download_telegram_file", AsyncMock(side_effect=lambda item: item.data)), \
                patch.object(bot, "ALBUM_FLUSH_QUIET_SECONDS", 60):
            for i in (103, 101, 102):
                await bot.handle_photo_message(*upload_update(
                    h, "photo", i, image_bytes("PNG", (i, 20, 30)), "pending-photos",
                ))
            for i, data in ((203, image_bytes("JPEG")), (201, image_bytes("WEBP")),
                            (202, LONG_TEXT.encode())):
                await bot.handle_document_message(*upload_update(
                    h, "document", i, data, "pending-documents",
                ))
        assert len(await h.db.get_attachment_records()) == 6
        assert not h.requests
        assert len(bot._pending_album_conversations) == 2
        return {"persisted_before_any_flush": True}


async def after_pending_albums_restart(bot, root):
    async with Harness(bot, root) as h:
        assert not bot._pending_album_conversations
        assert len(await h.db.get_attachment_records()) == 6
        await h.call(question="after restart before album reply")
        texts, images, _ = unpack_request(h.requests[-1])
        assert images == [
            *[image_bytes("PNG", (i, 20, 30)) for i in (101, 102, 103)],
            image_bytes("WEBP"), image_bytes("JPEG"),
        ]
        assert LONG_TEXT in "\n".join(texts)
        return {"pending_album_originals_survive_restart": True}


async def check_clear_races(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        for kind in ("photo", "document"):
            for group in (None, "clear-during-download"):
                started, release = asyncio.Event(), asyncio.Event()

                async def download(item):
                    started.set()
                    await release.wait()
                    return item.data

                handler = bot.handle_photo_message if kind == "photo" else bot.handle_document_message
                before = len(h.requests)
                with patch.object(bot, "download_telegram_file", download):
                    task = asyncio.create_task(handler(*upload_update(
                        h, kind, 101, image_bytes(), group,
                    )))
                    try:
                        await asyncio.wait_for(started.wait(), 10)
                        await bot.cmd_delete_chat(h.update, h.context)
                    finally:
                        release.set()
                        await asyncio.wait_for(task, 10)
                assert not await h.db.get_attachment_records()
                assert not bot._pending_album_conversations
                assert len(h.requests) == before
                result[f"{kind}/{group is not None}"] = True

        await h.seed()
        before = len(h.requests)
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def assemble(records, upload_root, generated_root=None):
            assembled = prepare_attachment_context(records, upload_root, generated_root)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10), "test did not release assembly"
            return assembled

        with patch.object(bot, "prepare_attachment_context", assemble):
            task = asyncio.create_task(h.call())
            try:
                await asyncio.wait_for(started.wait(), 10)
                await bot.cmd_delete_chat(h.update, h.context)
            finally:
                release.set()
            try:
                await asyncio.wait_for(task, 10)
            except AttachmentContextError as exc:
                assert "\u6e05\u7a7a" in str(exc)
            else:
                raise AssertionError("cleared attachments were sent after assembly")
        assert len(h.requests) == before
        assert not await h.db.get_attachment_records()
        await h.call()
        assert not unpack_request(h.requests[-1])[1]
        result["clear_during_assembly"] = True
    return result


async def check_malformed_ingress(bot, root):
    async with Harness(bot, root) as h:
        result = {}
        for kind, data, name in (
            ("photo", b"not an image", "photo.jpg"),
            ("document", image_bytes("JPEG")[:-3], "truncated.jpg"),
            ("document", b"%PDF-1.7 unsupported", "file.pdf"),
            ("document", b"\x00\x01\x02", "binary.bin"),
        ):
            await h.db.clear_all_conversation_memory()
            before = len(h.requests)
            handler = bot.handle_photo_message if kind == "photo" else bot.handle_document_message
            h.drain_frames()
            with patch.object(bot, "download_telegram_file", AsyncMock(return_value=data)):
                await handler(*upload_update(h, kind, 100, data, name=name))
            rows = await h.db.get_attachment_records()
            assert len(rows) == 1
            assert json.loads(rows[0]["metadata"])["attachments"][0]["kind"] == "invalid"
            assert len(h.requests) == before
            assert "\u65e0\u6cd5\u5b8c\u6574" in json.dumps(h.drain_frames(), ensure_ascii=False)
            try:
                await h.call()
            except AttachmentContextError:
                pass
            else:
                raise AssertionError("a later turn silently omitted the invalid upload")
            assert len(h.requests) == before
            result[name] = True
        await h.db.clear_all_conversation_memory()
        with patch.object(h.db, "get_attachment_records", AsyncMock(side_effect=OSError("disk failure"))):
            before = len(h.requests)
            try:
                await h.call()
            except AttachmentContextError as exc:
                assert "disk failure" in str(exc)
            else:
                raise AssertionError("database read failure was treated as an AI answer")
            assert len(h.requests) == before
        result["database_read_failure_is_explicit"] = True
        return result


async def check_upstream_failures(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        await h.seed()
        for fmt in FORMATS:
            providers = bot.UserDataManager.get("providers")
            providers["p"]["api_format"] = fmt
            for stream, style in ((False, "foreground"), (True, "foreground"), (True, "background")):
                bot.UserDataManager.set("stream_mode", stream)
                bot.UserDataManager.set("stream_style", style)
                h.error = (413, "CAPACITY-ERROR complete request is too large")
                h.drain_frames()
                before = len(h.requests)
                await h.turn("upstream HTTP failure")
                assert len(h.requests) == before + 1
                assert_complete(h.requests[-1], "upstream HTTP failure")
                assert "CAPACITY-ERROR" in json.dumps(h.drain_frames())
                if stream:
                    h.error = None
                    h.sse_error = "STREAM-CAPACITY-ERROR"
                    before = len(h.requests)
                    await h.turn("upstream stream error after partial text")
                    assert len(h.requests) == before + 1
                    assert "STREAM-CAPACITY-ERROR" in json.dumps(h.drain_frames())
                    h.sse_error = None
            h.error = None
        rows = await h.db.get_global_messages(1000)
        assert not any(row["msg_type"] == bot.MessageType.AI_REPLY for row in rows)
        result["http_and_sse_errors_visible_in_all_renderers"] = True
        result["no_failed_request_recorded_as_ai_reply"] = True

        for fmt in FORMATS:
            h.empty_stream = True
            before = len(h.requests)
            try:
                await h.call(fmt, True)
            except AttachmentContextError:
                pass
            else:
                raise AssertionError((fmt, "empty stream was accepted as a reply"))
            assert len(h.requests) == before + 1
            assert_complete(h.requests[-1], "current question")
        h.empty_stream = False
        result["empty_streams_are_explicit_failures"] = True

        h.force_sse = True
        h.sse_error = "NONSTREAM-SSE-ERROR"
        before = len(h.requests)
        try:
            await h.call("openai_compatible", False)
        except AttachmentContextError as exc:
            assert "NONSTREAM-SSE-ERROR" in str(exc)
        else:
            raise AssertionError("HTTP 200 SSE error was ignored after partial text")
        assert len(h.requests) == before + 1
        result["nonstream_sse_error_is_not_a_success"] = True
    return result


async def check_final_request_limits(bot, root):
    result = {}
    async with Harness(bot, root) as h:
        await h.seed()
        history, _ = await bot.ModelClient._prepare_conversation_request(
            "p", URL, MODEL, "system instructions", await h.history("current question"), None,
        )
        input_tokens = estimate_input_tokens(history, history.system_prompt, {})
        base_limits = {"context_window": input_tokens + 4096, "max_output_tokens": 4096,
                       "max_images": 12}
        for fmt in FORMATS:
            for stream in (False, True):
                bot.UserDataManager.set("model_request_limits", {f"p/{MODEL}": base_limits})
                await h.call(fmt, stream)
                body = h.requests[-1]
                assert_complete(body, "current question")
                assert (body.get("max_tokens") or body.get("generationConfig", {}).get("maxOutputTokens")) == 4096
                byte_count = len(h.request_bytes[-1])
                for delta in (0, -1):
                    limits = {**base_limits, "max_request_bytes": byte_count + delta}
                    bot.UserDataManager.set("model_request_limits", {f"p/{MODEL}": limits})
                    before = len(h.requests)
                    if delta == 0:
                        await h.call(fmt, stream)
                        assert len(h.requests) == before + 1
                    else:
                        try:
                            await h.call(fmt, stream)
                        except AttachmentContextError:
                            pass
                        else:
                            raise AssertionError((fmt, stream, "oversized serialized body was sent"))
                        assert len(h.requests) == before
        result["all_formats_final_body_bytes_and_output_reservations"] = True

        bot.UserDataManager.set("thinking_level", "high")
        budget = bot.THINKING_LEVEL_SPECS["high"]["budget"]
        output = budget + bot.CLAUDE_THINKING_ANSWER_HEADROOM
        for stream in (False, True):
            for limits in (
                {"context_window": input_tokens + 4096, "max_output_tokens": output},
                {"context_window": input_tokens + output, "max_output_tokens": output - 1},
            ):
                bot.UserDataManager.set("model_request_limits", {f"p/{MODEL}": limits})
                before = len(h.requests)
                try:
                    await h.call("claude", stream)
                except AttachmentContextError:
                    pass
                else:
                    raise AssertionError("the final thinking output budget was not checked")
                assert len(h.requests) == before
            limits = {"context_window": input_tokens + output, "max_output_tokens": output}
            bot.UserDataManager.set("model_request_limits", {f"p/{MODEL}": limits})
            await h.call("claude", stream)
            assert h.requests[-1]["max_tokens"] == output
            assert h.requests[-1]["thinking"]["budget_tokens"] == budget
            assert_complete(h.requests[-1], "current question")
        result["final_claude_thinking_budget_is_checked"] = True

        for fmt in FORMATS:
            for stream in (False, True):
                bot.ModelClient._thinking_unsupported.clear()
                bot.UserDataManager.set("model_request_limits", {})
                h.errors = [(400, "thinking / reasoning_effort is not supported")]
                before = len(h.requests)
                await h.call(fmt, stream)
                assert len(h.requests) == before + 2
                for body in h.requests[before:]:
                    assert_complete(body, "current question")
        result["existing_thinking_retry_preserves_every_attachment"] = True
    return result
