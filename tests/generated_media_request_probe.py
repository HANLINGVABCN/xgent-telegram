"""Generated-image runtime scenarios with captured SDK/HTTP request bodies."""

import asyncio
import base64
import json
import threading
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from tests.attachment_request_probe import (
    FORMATS, LONG_TEXT, MODEL, URL, Harness, unpack_request,
)
from tests.test_attachments import image_bytes
from xgent_app.attachments import AttachmentContextError, prepare_attachment_context
from xgent_app.context_limits import estimate_input_tokens


RENDERERS = ((False, "foreground"), (True, "foreground"), (True, "background"))
MEDIA_BLOCK = "```media-x\n<<BEGIN_image_probe\nTwo original images\n<<END_image_probe\n```"


def originals():
    return [
        image_bytes(fmt, (30 + i * 35, 100, 140))
        for i, fmt in enumerate(("PNG", "JPEG", "WEBP", "PNG", "JPEG"))
    ]


def data_url(data, mime="image/png"):
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def native_reply(images, text="generated originals", echo_first=False, split_events=False):
    return {
        "images": images, "text": text, "echo_first": echo_first,
        "split_events": split_events,
    }


def assert_images(body, expected, mimes=None):
    texts, actual, actual_mimes = unpack_request(body)
    assert actual == expected, ("original image bytes, order or count differ", len(actual), len(expected))
    if mimes is not None:
        assert actual_mimes == mimes, actual_mimes
    assert "_conversation_attachments" not in json.dumps(body)
    return "\n".join(texts)


async def refs(h):
    return [
        ref for row in await h.db.get_attachment_records()
        for ref in json.loads(row["metadata"]).get("attachments", [])
    ]


class GeneratedHarness(Harness):
    def respond(self, request):
        if not self.replies or not isinstance(self.replies[0], dict):
            return super().respond(request)
        body = json.loads(request.content)
        self.requests.append(body)
        self.request_bytes.append(request.content)
        reply = self.replies.pop(0)
        # Deliberately mislabel the data URLs; persistence must inspect the bytes.
        urls = [data_url(data, "image/jpeg") for data in reply["images"]]
        text = reply["text"]
        is_stream = bool(body.get("stream") or "streamGenerateContent" in str(request.url))
        if "contents" in body:
            payload = {"candidates": [{
                "content": {"role": "model", "parts": [
                    {"text": text},
                    *[{"inlineData": {"mimeType": "image/jpeg", "data": url.split(",")[1]}}
                      for url in urls],
                ]}, "finishReason": "STOP",
            }]}
        elif str(request.url).endswith("/messages"):
            text = "\n".join([text, *urls])
            payload = (
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
                if is_stream else
                {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
            )
        else:
            if reply["echo_first"] and urls:
                text += "\n" + urls[0]
            message = {
                "role": "assistant", "content": text,
                "images": [{"image_url": {"url": url}} for url in urls],
            }
            payload = {
                "id": "generated", "created": 1, "model": MODEL,
                "object": "chat.completion.chunk" if is_stream else "chat.completion",
                "choices": [{
                    "index": 0, "delta" if is_stream else "message": message,
                    "finish_reason": None if is_stream else "stop",
                }],
            }
        if is_stream:
            events = [payload]
            if reply.get("split_events"):
                if "contents" in body:
                    events = [
                        {"candidates": [{"content": {"role": "model", "parts": [part]}}]}
                        for part in payload["candidates"][0]["content"]["parts"]
                    ]
                elif str(request.url).endswith("/messages"):
                    events = [
                        {"type": "content_block_delta", "delta": {
                            "type": "text_delta", "text": part + "\n",
                        }}
                        for part in [reply["text"], *urls]
                    ]
                else:
                    events = [
                        {**payload, "choices": [{"index": 0, "delta": delta}]}
                        for delta in [
                            {"role": "assistant", "content": text},
                            *[{"images": [image]} for image in message["images"]],
                        ]
                    ]
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content="".join("data: " + json.dumps(event) + "\n\n" for event in events)
                        + "data: [DONE]\n\n",
            )
        return httpx.Response(200, json=payload)

    def configure(self, fmt="openai", stream=False, style="foreground", agent=False):
        self.bot.UserDataManager.get("providers")["p"]["api_format"] = fmt
        for name, value in (
            ("stream_mode", stream), ("stream_style", style), ("agent_mode", agent),
            ("default_media_provider_key", "p"), ("default_media_model", MODEL),
        ):
            self.bot.UserDataManager.set(name, value)

    async def generated_turn(self, images, **kwargs):
        self.replies = [native_reply(images, **kwargs)]
        await self.turn("generate original images")


async def seed_generated_and_check(bot, root):
    async with GeneratedHarness(bot, root) as h:
        images = originals()
        await h.add_upload(LONG_TEXT.encode(), "complete.txt")
        await h.add_upload(images[0], "uploaded.png")
        await h.generated_turn(images[1:3], echo_first=True)
        assert len(await h.db.get_attachment_records()) == 3

        h.configure(agent=True)
        h.replies = [MEDIA_BLOCK, native_reply(images[3:]), "media continuation"]
        before = len(h.requests)
        await h.turn("generate with media model")
        assert len(h.requests) == before + 3
        assert_images(h.requests[before], images[:3])
        assert_images(h.requests[before + 1], [])
        assert LONG_TEXT not in json.dumps(h.requests[before + 1])
        assert_images(h.requests[before + 2], images)
        result = {"media_input_unchanged": True, "agent_continuation": True}

        h.configure(agent=False)
        h.replies = ["second user turn"]
        await h.turn("what is in the old images?")
        assert_images(h.requests[-1], images)
        for i in range(35):
            await bot.GlobalRecorder.record_user_message(f"later question {i}", chat_id=1)
            await bot.GlobalRecorder.record_ai_reply(f"later answer {i}", chat_id=1)
        assert len(await h.db.get_conversation_messages(2)) == 2
        result["formats"] = {}
        for fmt in FORMATS:
            for stream in (False, True):
                response, error = await h.call(fmt, stream, "old images outside the window")
                assert response == "ok" and error is None
                joined = assert_images(
                    h.requests[-1], images,
                    ["image/png", "image/jpeg", "image/webp", "image/png", "image/jpeg"],
                )
                assert joined.count(LONG_TEXT) == 1
                assert unpack_request(h.requests[-1])[0][-1] == "old images outside the window"
                result["formats"][f"{fmt}/{stream}"] = True
        references = await refs(h)
        assert [ref.get("storage", "uploads") for ref in references] == [
            "uploads", "uploads", "generated_media", "generated_media",
            "generated_media", "generated_media",
        ]
        assert [ref["source"] for ref in references[2:]] == [
            "chat_native_media", "chat_native_media", "external_media_module", "external_media_module",
        ]
        assert [ref["order"] for ref in references[2:]] == [0, 1, 0, 1]
        for ref in references[2:]:
            assert (ref["width"], ref["height"]) == (13, 17)
            assert len(ref["sha256"]) == 64 and ref["provider_name"] == "p"
            assert ref["model_name"] == MODEL
        serialized = json.dumps(await h.db.get_global_messages(1000))
        assert "data:image/" not in serialized
        assert all(base64.b64encode(data).decode() not in serialized for data in images)
        await bot.cmd_export_all(h.update, h.context)
        exports = list((h.root / "storage" / "exports").rglob("*.zip"))
        assert exports
        with zipfile.ZipFile(exports[-1]) as archive:
            exported = b"\n".join(archive.read(name) for name in archive.namelist())
        assert b"data:image/" not in exported
        assert all(base64.b64encode(data) not in exported for data in images)
        result["index_only_history_export"] = True
        result["metadata"] = True
        return result


async def after_generated_restart(bot, root):
    async with GeneratedHarness(bot, root) as h:
        assert len(await h.db.get_attachment_records()) == 4
        assert bot.UserDataManager.get("agent_mode") is False
        result = {}
        for fmt in FORMATS:
            for stream in (False, True):
                await h.call(fmt, stream)
                assert_images(h.requests[-1], originals())
                result[f"{fmt}/{stream}"] = True
        return result


async def check_native_renderers(bot, root):
    async with GeneratedHarness(bot, root) as h:
        result = {}
        images = originals()[1:3]
        send = bot.send_generated_media_artifacts
        for fmt in FORMATS:
            for stream, style in RENDERERS:
                await h.db.clear_all_conversation_memory()
                h.configure(fmt, stream, style)
                delivered = []

                async def deliver(context, chat_id, artifacts, **kwargs):
                    records = await h.db.get_attachment_records()
                    assert len(records) == 1
                    assert len(json.loads(records[0]["metadata"])["attachments"]) == len(images)
                    assert "data:image/" not in records[0]["content"]
                    delivered.extend(Path(item["path"]).read_bytes() for item in artifacts)
                    await send(context, chat_id, artifacts, **kwargs)

                with patch.object(bot, "send_generated_media_artifacts", deliver):
                    await h.generated_turn(images)
                assert delivered == images, (fmt, stream, style)
                rows = await h.db.get_global_messages(1000)
                assert sum(row["msg_type"] == bot.MessageType.AI_REPLY for row in rows) == 1
                cid = bot.UserDataManager.get("current_chat_id")
                mirror = await h.db.get_chat_messages(cid)
                assert sum(row["role"] == "assistant" for row in mirror) == 1
                assert "data:image/" not in json.dumps(mirror)
                await h.call(fmt, stream)
                assert_images(h.requests[-1], images, ["image/jpeg", "image/webp"])
                result[f"{fmt}/{stream}/{style}"] = True
        return result


async def check_large_original_and_many_images(bot, root):
    async with GeneratedHarness(bot, root) as h:
        large = image_bytes(size=(1800, 1800), compress_level=0)
        assert len(large) > 8 * 1024 * 1024
        small = [image_bytes(color=(i, 50, 100)) for i in range(12)]
        h.configure(agent=True)
        h.replies = [MEDIA_BLOCK, native_reply([large, *small]), "all original bytes"]
        await h.turn("large media output")
        assert len(await refs(h)) == 13
        assert_images(h.requests[-1], [large, *small])
        h.configure(agent=False)
        for fmt in FORMATS:
            h.requests.clear()
            h.request_bytes.clear()
            await h.call(fmt, False)
            assert_images(h.requests[-1], [large, *small])
        return {"large_original": True, "all_images_in_order_once": True}


async def check_separate_native_image_events(bot, root):
    async with GeneratedHarness(bot, root) as h:
        images = originals()[1:3]
        result = {}
        for fmt in FORMATS:
            for style in ("foreground", "background"):
                await h.db.clear_all_conversation_memory()
                h.configure(fmt, stream=True, style=style)
                await h.generated_turn(images, split_events=True, echo_first=True)
                assert len(await refs(h)) == 2, (fmt, style)
                assert "data:image/" not in json.dumps(await h.db.get_global_messages(1000))
                await h.call(fmt, True)
                assert_images(h.requests[-1], images)
                result[f"{fmt}/{style}"] = True
        return result


async def check_media_stop_races(bot, root):
    async with GeneratedHarness(bot, root) as h:
        images = originals()[1:3]
        generate = bot.run_default_media_generation
        result = {}
        for stream, style in RENDERERS:
            for phase in ("pending", "complete_stop", "complete_cancel"):
                await h.db.clear_all_conversation_memory()
                h.configure(stream=stream, style=style, agent=True)
                h.replies = [MEDIA_BLOCK, native_reply(images), "must not continue"]
                started, closed = asyncio.Event(), asyncio.Event()
                owner = None

                async def media(prompt):
                    if phase == "pending":
                        started.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            closed.set()
                    payload = await generate(prompt)
                    if phase == "complete_stop":
                        bot.get_or_create_stop_event().set()
                    else:
                        asyncio.get_running_loop().call_soon(owner.cancel)
                    return payload

                before = len(h.requests)
                with patch.object(bot, "run_default_media_generation", media), patch.object(
                    bot, "send_generated_media_artifacts", AsyncMock(),
                ) as delivery:
                    owner = asyncio.create_task(h.turn("media stop race"))
                    if phase == "pending":
                        await asyncio.wait_for(started.wait(), 10)
                        bot.get_or_create_stop_event().set()
                    try:
                        await asyncio.wait_for(owner, 10)
                    except asyncio.CancelledError:
                        assert phase == "complete_cancel"
                    else:
                        assert phase != "complete_cancel"
                delivery.assert_not_awaited()
                completed = phase != "pending"
                assert len(h.requests) == before + (2 if completed else 1)
                assert len(await refs(h)) == (2 if completed else 0)
                if not completed:
                    assert closed.is_set()
                assert "data:image/" not in json.dumps(await h.db.get_global_messages(1000))
                h.replies.clear()
                h.configure(agent=False)
                await h.call()
                assert_images(h.requests[-1], images if completed else [])
                result[f"{stream}/{style}/{phase}"] = True
        return result


async def check_idle_generated_images(bot, root):
    async with GeneratedHarness(bot, root) as h:
        images = originals()[1:3]
        result = {}
        bot.UserDataManager.set("idle_message_interval", 3600)
        think = bot.ModelClient.think_and_reply
        send = bot.send_generated_media_artifacts
        for phase in ("normal", "delivery_failure", "cancel"):
            await h.db.clear_all_conversation_memory()
            await h.add_upload(originals()[0], "uploaded.png")
            await h.db.set_config("last_idle_notice_time", 0)
            h.replies = [native_reply(images)]
            owner = None

            async def deliver(*args, **kwargs):
                assert len(await refs(h)) == 3
                if phase == "delivery_failure":
                    raise OSError("idle delivery failed")
                return await send(*args, **kwargs)

            async def reply(*args, **kwargs):
                response = await think(*args, **kwargs)
                if phase == "cancel":
                    asyncio.get_running_loop().call_soon(owner.cancel)
                return response

            with patch.object(
                h.db, "get_last_user_message_time", AsyncMock(return_value=bot.time.time() - 7200),
            ), patch.object(bot.ModelClient, "think_and_reply", reply), patch.object(
                bot, "send_generated_media_artifacts", deliver,
            ):
                owner = asyncio.create_task(bot.check_and_send_idle_message(h.context))
                try:
                    await asyncio.wait_for(owner, 10)
                except asyncio.CancelledError:
                    assert phase == "cancel"
                else:
                    assert phase != "cancel"
            assert_images(h.requests[-1], [originals()[0]])
            assert len(await refs(h)) == 3
            assert "data:image/" not in json.dumps(await h.db.get_global_messages(1000))
            await h.call()
            assert_images(h.requests[-1], [originals()[0], *images])
            result[phase] = True
        return result


async def check_delivery_and_persistence_failures(bot, root):
    async with GeneratedHarness(bot, root) as h:
        result = {}
        images = originals()[1:3]
        for stream, style in RENDERERS:
            await h.db.clear_all_conversation_memory()
            h.configure(stream=stream, style=style)
            with patch.object(
                bot, "send_generated_media_artifacts", AsyncMock(side_effect=OSError("delivery failed")),
            ) as delivery:
                await h.generated_turn(images)
            delivery.assert_awaited_once()
            rows = await h.db.get_attachment_records()
            assert len(rows) == 1 and len(await refs(h)) == 2
            await h.call()
            assert_images(h.requests[-1], images)
        result["native_delivery_failure_keeps_originals_once"] = True
        await h.db.clear_all_conversation_memory()
        h.configure(agent=True)
        h.replies = [MEDIA_BLOCK, native_reply(images), "continue with originals"]
        h.drain_frames()
        with patch.object(
            bot, "send_generated_media_artifacts", AsyncMock(side_effect=OSError("media delivery failed")),
        ):
            await h.turn("media delivery failure")
        assert_images(h.requests[-1], images)
        assert "media delivery failed" in json.dumps(h.drain_frames())
        result["media_delivery_failure_keeps_originals"] = True

        for media in (False, True):
            await h.db.clear_all_conversation_memory()
            h.configure(agent=media)
            h.replies = ([MEDIA_BLOCK, native_reply(images), "must not continue"] if media
                         else [native_reply(images)])
            record = h.db.record_global_message

            async def fail_association(*args, **kwargs):
                if (kwargs.get("metadata") or {}).get("attachments"):
                    raise OSError("ASSOCIATION-DISK-FAILURE")
                return await record(*args, **kwargs)

            h.drain_frames()
            before = len(h.requests)
            with patch.object(h.db, "record_global_message", fail_association), patch.object(
                bot, "send_generated_media_artifacts", AsyncMock(),
            ) as delivery:
                await h.turn("database failure")
            delivery.assert_not_awaited()
            assert len(h.requests) == before + (2 if media else 1)
            assert not await h.db.get_attachment_records()
            assert "ASSOCIATION-DISK-FAILURE" in json.dumps(h.drain_frames())
            h.replies.clear()
        result["association_failure_blocks_delivery_and_continuation"] = True
        return result


async def check_generated_limits(bot, root):
    async with GeneratedHarness(bot, root) as h:
        images = originals()[1:3]
        await h.generated_turn(images)
        result = {}
        for limits in (
            {"context_window": 100}, {"max_input_tokens": 100}, {"max_images": 1},
            {"max_request_bytes": 1000}, {"supports_images": False},
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
                        raise AssertionError(("invalid full request was sent", fmt, stream, limits))
                    assert len(h.requests) == before
        result["preflight_all_formats"] = True
        bot.UserDataManager.set("model_request_limits", {})
        history, _ = await bot.ModelClient._prepare_conversation_request(
            "p", URL, MODEL, "system instructions", await h.history("current question"), None,
        )
        tokens = estimate_input_tokens(history, history.system_prompt, {})
        for fmt in FORMATS:
            for stream in (False, True):
                bot.UserDataManager.set("model_request_limits", {
                    f"p/{MODEL}": {"context_window": tokens + 4096, "max_output_tokens": 4096},
                })
                await h.call(fmt, stream)
                assert_images(h.requests[-1], images)
                body = h.requests[-1]
                assert (body.get("max_tokens") or body.get("generationConfig", {}).get("maxOutputTokens")) == 4096
                bot.UserDataManager.set("model_request_limits", {
                    f"p/{MODEL}": {
                        "context_window": tokens + 4096, "max_output_tokens": 4096,
                        "max_request_bytes": len(h.request_bytes[-1]) - 1,
                    },
                })
                before = len(h.requests)
                try:
                    await h.call(fmt, stream)
                except AttachmentContextError:
                    pass
                else:
                    raise AssertionError("serialized request-size limit was ignored")
                assert len(h.requests) == before
                bot.UserDataManager.set("model_request_limits", {})
                h.error = (413, "GENERATED-CAPACITY-ERROR")
                before = len(h.requests)
                try:
                    await h.call(fmt, stream)
                except AttachmentContextError as exc:
                    assert "GENERATED-CAPACITY-ERROR" in str(exc)
                else:
                    raise AssertionError("upstream capacity error was ignored")
                assert len(h.requests) == before + 1
                assert_images(h.requests[-1], images)
                h.error = None
        result["complete_requests_output_reservations_and_upstream_errors"] = True

        reference = (await refs(h))[0]
        path = Path(bot.ArtifactManager.GENERATED_MEDIA_DIR) / reference["path"]
        original = path.read_bytes()
        for content in (None, b"damaged image"):
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            before = len(h.requests)
            try:
                await h.call()
            except AttachmentContextError as exc:
                assert path.name in str(exc)
            else:
                raise AssertionError("missing or damaged original silently omitted")
            assert len(h.requests) == before
            path.write_bytes(original)
        result["missing_and_damaged_originals_block"] = True
        return result


async def check_stop_and_cancel(bot, root):
    async with GeneratedHarness(bot, root) as h:
        images = originals()[1:3]
        raw = "\n".join(data_url(data) for data in images)
        result = {}
        for stream, style in RENDERERS:
            for completed in (False, True):
                await h.db.clear_all_conversation_memory()
                h.configure(stream=stream, style=style, agent=True)
                started = asyncio.Event()
                closed = asyncio.Event()

                async def nonstream(*_args, **_kwargs):
                    if completed:
                        bot.get_or_create_stop_event().set()
                        return raw, None
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        closed.set()

                async def chunks(*_args, **_kwargs):
                    if completed:
                        bot.get_or_create_stop_event().set()
                        yield raw
                        return
                    started.set()
                    try:
                        await asyncio.Event().wait()
                        yield ""
                    finally:
                        closed.set()

                target = "think_and_reply_stream" if stream else "think_and_reply"
                with patch.object(bot.ModelClient, target, chunks if stream else nonstream):
                    task = asyncio.create_task(h.turn("stop generating"))
                    if not completed:
                        await asyncio.wait_for(started.wait(), 10)
                        bot.get_or_create_stop_event().set()
                    await asyncio.wait_for(task, 10)
                assert len(await refs(h)) == (2 if completed else 0), (stream, style, completed)
                if not completed:
                    assert closed.is_set()
                assert "data:image/" not in json.dumps(await h.db.get_global_messages(1000))
                await h.call()
                assert_images(h.requests[-1], images if completed else [])
                result[f"stop/{stream}/{style}/{completed}"] = True

        for stream, style in RENDERERS:
            await h.db.clear_all_conversation_memory()
            h.configure(stream=stream, style=style)
            waiting = asyncio.Event()
            closed = asyncio.Event()
            owner = None

            async def nonstream(*_args, **_kwargs):
                asyncio.get_running_loop().call_soon(owner.cancel)
                return raw, None

            async def chunks(*_args, **_kwargs):
                try:
                    yield raw
                    waiting.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()

            target = "think_and_reply_stream" if stream else "think_and_reply"
            with patch.object(bot.ModelClient, target, chunks if stream else nonstream):
                owner = asyncio.create_task(h.turn("force cancel after complete images"))
                if stream:
                    await asyncio.wait_for(waiting.wait(), 10)
                    owner.cancel()
                try:
                    await asyncio.wait_for(owner, 10)
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancellation was swallowed")
            assert len(await refs(h)) == 2, ("completed image lost on cancellation", stream, style)
            if stream:
                assert closed.is_set(), "upstream stream was left running"
            await h.call()
            assert_images(h.requests[-1], images)
            result[f"cancel/{stream}/{style}"] = True

        for style in ("foreground", "background"):
            for has_complete in (False, True):
                await h.db.clear_all_conversation_memory()
                h.configure(stream=True, style=style)
                complete = data_url(images[1]) if has_complete else ""
                incomplete = data_url(images[0][:-40])

                async def chunks(*_args, **_kwargs):
                    yield complete + "\n" + incomplete
                    bot.get_or_create_stop_event().set()
                    await asyncio.Event().wait()

                h.drain_frames()
                with patch.object(bot.ModelClient, "think_and_reply_stream", chunks):
                    await asyncio.wait_for(h.turn("stop with an incomplete image"), 10)
                assert len(await refs(h)) == int(has_complete)
                assert "data:image/" not in json.dumps(await h.db.get_global_messages(1000))
                if not has_complete:
                    frames = json.dumps(h.drain_frames(), ensure_ascii=False)
                    assert "\u5df2\u5b8c\u6210\u7684\u56fe\u7247\u4fdd\u7559" not in frames
                await h.call()
                assert_images(h.requests[-1], [images[1]] if has_complete else [])
                result[f"incomplete/{style}/{has_complete}"] = True
        return result


async def check_generated_clear_races(bot, root):
    async with GeneratedHarness(bot, root) as h:
        images = originals()[1:3]
        result = {}
        await h.add_upload(originals()[0], "uploaded.png")
        await h.generated_turn(images)
        stale_history = await bot.build_model_conversation_history(await h.history("before clear"))
        references = await refs(h)
        await bot.cmd_delete_chat(h.update, h.context)
        assert not await h.db.get_attachment_records()
        for ref in references:
            storage = (bot.ArtifactManager.GENERATED_MEDIA_DIR if ref.get("storage") == "generated_media"
                       else bot.ArtifactManager.UPLOAD_DIR)
            assert (Path(storage) / ref["path"]).is_file()
        await h.call(history=stale_history)
        assert_images(h.requests[-1], [])
        result["clear_unlinks_both_roots_without_deleting"] = True

        for media in (False, True):
            for phase in ("generation", "primary_save", "mirror_save"):
                await h.db.clear_all_conversation_memory()
                h.configure(agent=media)
                h.replies = ([MEDIA_BLOCK, native_reply(images), "must not continue"]
                             if media else [native_reply(images)])
                original_think = bot.ModelClient.think_and_reply
                original_record = h.db.record_global_message
                original_mirror = h.db.add_chat_message
                cleared = False

                async def clear_once():
                    nonlocal cleared
                    assert not cleared
                    cleared = True
                    await bot.cmd_delete_chat(h.update, h.context)

                async def think(*args, **kwargs):
                    response = await original_think(*args, **kwargs)
                    if response[0] and "data:image/" in response[0] and phase == "generation":
                        await clear_once()
                    return response

                async def record(*args, **kwargs):
                    if (kwargs.get("metadata") or {}).get("attachments") and phase == "primary_save":
                        await clear_once()
                    return await original_record(*args, **kwargs)

                async def mirror(*args, **kwargs):
                    if kwargs.get("attachment_generation") is not None and phase == "mirror_save":
                        await clear_once()
                    return await original_mirror(*args, **kwargs)

                h.drain_frames()
                with patch.object(bot.ModelClient, "think_and_reply", think), patch.object(
                    h.db, "record_global_message", record,
                ), patch.object(h.db, "add_chat_message", mirror), patch.object(
                    bot, "send_generated_media_artifacts", AsyncMock(),
                ) as delivery:
                    await h.turn(f"clear during {phase}")
                assert cleared
                delivery.assert_not_awaited()
                assert not await h.db.get_attachment_records(), (media, phase)
                assert not await h.db.get_chat_messages(bot.UserDataManager.get("current_chat_id"))
                assert "data:image/" not in json.dumps(await h.db.get_global_messages(1000))
                h.replies.clear()
                await h.call()
                assert_images(h.requests[-1], [])
                result[f"{media}/{phase}"] = True

        h.configure()
        await h.generated_turn(images)
        before = len(h.requests)
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def assemble(records, upload_root, generated_root=None):
            assembled = prepare_attachment_context(records, upload_root, generated_root)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10)
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
            except AttachmentContextError:
                pass
            else:
                raise AssertionError("cleared generated images were sent")
        assert len(h.requests) == before
        result["clear_during_assembly"] = True
        return result


def legacy_notice(bot, path):
    return (
        "\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a\u672c\u56fe\u7247\u5df2\u81ea\u52a8\u5b58\u5165 "
        + bot.to_display_path(path)
        + "\uff0c\u9700\u8981\u65f6\u8bf7read\u4ee5\u8fd4\u56de\u4e0a\u4e0b\u6587"
        + "\uff0c\u65e0\u8bc6\u56fe\u80fd\u529b\u65f6\u8bf7\u52ffread\u4ee5\u514d\u62a5\u9519\u3011"
    )


async def check_generated_legacy(bot, root):
    async with GeneratedHarness(bot, root) as h:
        images = originals()[1:3]
        saved = [
            bot.ArtifactManager.save_generated_media("assistant_image.png", data, "image/png")
            for data in images
        ]
        unlinked = bot.ArtifactManager.save_generated_media(
            "assistant_image.png", originals()[0], "image/png",
        )
        await bot.GlobalRecorder.record_user_message(legacy_notice(bot, unlinked["abs_path"]), chat_id=1)
        await bot.GlobalRecorder.record_ai_reply(legacy_notice(bot, unlinked["abs_path"]), chat_id=1)
        for index, (role, kind) in enumerate((
            ("assistant", bot.MessageType.AI_REPLY), ("media_module", bot.MessageType.MEDIA_REPLY),
        )):
            await bot.GlobalRecorder.record(
                kind, role, legacy_notice(bot, saved[index]["abs_path"]), chat_id=1,
            )
        await h.call()
        assert_images(h.requests[-1], images)
        assert len(await refs(h)) == 2
        assert all(ref["legacy"] for ref in await refs(h))
        result = {"trusted_legacy_only_no_directory_scan": True}

        rows = await h.db.get_attachment_records()
        await h.db.clear_all_conversation_memory()
        await h.call()
        assert_images(h.requests[-1], [])
        assert all(Path(item["abs_path"]).is_file() for item in saved)
        assert not await h.db.backfill_attachment_metadata(rows[0]["id"], None, {"attachments": []})
        result["cleared_records_never_reassociated"] = True

        for path in (str(Path(saved[0]["abs_path"]).with_name("123456_000000ff_assistant_image.png")),
                     str(h.root / "untrusted.png")):
            await h.db.clear_all_conversation_memory()
            await bot.GlobalRecorder.record(
                bot.MessageType.MEDIA_REPLY, "media_module", legacy_notice(bot, path), chat_id=1,
            )
            before = len(h.requests)
            try:
                await h.call()
            except AttachmentContextError:
                pass
            else:
                raise AssertionError("unrecoverable legacy generation silently omitted")
            assert len(h.requests) == before
        result["unrecoverable_and_untrusted_legacy_explicit"] = True
        return result


async def check_read_behavior_unchanged(bot, root):
    async with GeneratedHarness(bot, root) as h:
        await h.add_upload(LONG_TEXT.encode(), "persistent-upload.txt")
        await h.generated_turn(originals()[1:3])
        bot.UserDataManager.set("global_depth", 30)
        h.configure(agent=True)
        read_path = h.root / "read-only.txt"
        read_path.write_text("READ-BODY-ONLY-IN-CURRENT-AGENT-LOOP\n", encoding="utf-8")
        block = f"```read-x:{read_path}\n<<BEGIN_read_probe\n<<END_read_probe\n```"
        h.replies = [block, "no quoted body"]
        before = len(h.requests)
        await h.turn("read this file")
        assert len(h.requests) == before + 2
        joined = assert_images(h.requests[-1], originals()[1:3])
        assert "READ-BODY-ONLY-IN-CURRENT-AGENT-LOOP" in joined
        assert LONG_TEXT in joined
        rows = await h.db.get_global_messages(1000)
        assert "READ-BODY-ONLY-IN-CURRENT-AGENT-LOOP" not in json.dumps(rows)
        await h.turn("a new user turn")
        joined = assert_images(h.requests[-1], originals()[1:3])
        assert "READ-BODY-ONLY-IN-CURRENT-AGENT-LOOP" not in joined
        assert str(read_path) in joined and LONG_TEXT in joined
        assert len(await refs(h)) == 3
        return {"read_body_transient_not_persistent": True, "attachments_stay_persistent": True}
