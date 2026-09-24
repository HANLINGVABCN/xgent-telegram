"""Capture real provider adapter requests without paid model calls."""

import asyncio
import base64
import hashlib
import io
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from tests.attachment_request_probe import FORMATS, MODEL, URL
from tests.generated_media_request_probe import (
    RENDERERS,
    GeneratedHarness,
    assert_images,
    native_reply,
    originals,
    refs,
)
from tests.test_media_inputs import MP3, MP4, PDF, wav_bytes
from xgent_app import media_inputs
from xgent_app.media_inputs import MediaInputError

MEDIA_MODEL = "explicit-reference-model"
LONG_REFERENCE = "  FILE-HEAD\r\n" + "reference data is complete\n" * 26000 + "FILE-TAIL \t\r\n"
INSTRUCTION = "Use the first and third images to edit the second.\nKeep all other details."


def configure_media(h, fmt):
    h.bot.UserDataManager.get("providers")["media"] = {
        "api_key": "test-key", "base_url": URL, "api_format": fmt, "models": [MEDIA_MODEL],
    }
    h.bot.UserDataManager.set("default_media_provider_key", "media")
    h.bot.UserDataManager.set("default_media_model", MEDIA_MODEL)
    h.bot.get_or_create_stop_event().clear()


def save(h, name, data):
    path = h.root / name
    path.write_bytes(data)
    return str(path)


def request_body(paths, instruction=INSTRUCTION):
    return "\n".join([*(f'file: "{path}"' for path in paths), "prompt: " + instruction])


def protocol(paths):
    return "```media-x\n<<BEGIN_edit_reference_123\n" + request_body(paths) + "\n<<END_edit_reference_123\n```"


def unpack(body):
    texts, blobs, kinds = [], [], []
    for message in body.get("messages", body.get("contents", [])):
        content = message.get("content", message.get("parts", []))
        if isinstance(content, str):
            texts.append(content)
            continue
        for part in content:
            if "text" in part:
                texts.append(part["text"])
            if "image_url" in part:
                header, data = part["image_url"]["url"].split(",", 1)
                kind = header.split(";")[0][5:]
            elif "file" in part:
                header, data = part["file"]["file_data"].split(",", 1)
                kind = header.split(";")[0][5:]
                assert part["file"]["filename"]
            elif "input_audio" in part:
                audio = part["input_audio"]
                kind, data = "audio/" + audio["format"], audio["data"]
            elif "inline_data" in part:
                inline = part["inline_data"]
                kind, data = inline["mime_type"], inline["data"]
            elif part.get("type") in {"image", "document"}:
                source = part["source"]
                assert source["type"] == "base64"
                kind, data = source["media_type"], source["data"]
            else:
                continue
            blobs.append(base64.b64decode(data))
            kinds.append(kind)
    return texts, blobs, kinds


async def check_outbound(bot, root):
    async with GeneratedHarness(bot, root) as h:
        await h.add_upload(originals()[4], "unrelated-upload.png")
        await h.add_upload(b"UNRELATED-ATTACHMENT-TEXT", "unrelated.txt")
        await bot.GlobalRecorder.record_user_message("UNRELATED-CHAT-HISTORY", chat_id=1)
        bot.UserDataManager.set("assistant_prompt", "UNRELATED-PERSONA")
        bot.UserDataManager.set("global_append_prompt", "UNRELATED-GLOBAL-APPEND")
        bot.UserDataManager.set("model_request_limits", {
            f"p/{MODEL}": {"supports_images": False, "context_window": 1, "max_files": 0},
            f"media/{MEDIA_MODEL}": {"max_files": 12, "max_images": 4,
                                     "max_output_tokens": 512, "output_reserve_tokens": 256},
        })
        first, second = originals()[:2]
        image1 = save(h, "\u53c2\u8003 wrong.txt", first)
        image2 = save(h, "second image.jpg", second)
        text = save(h, "full text.txt", LONG_REFERENCE.encode("utf-16"))
        pdf = save(h, "document.bin", PDF)
        audio = save(h, "voice.wav", wav_bytes())
        mp3 = save(h, "voice.mp3", MP3)
        video = save(h, "clip.mp4", MP4)
        result = {}
        for fmt in FORMATS:
            configure_media(h, fmt)
            paths, expected = [image1, text, image2, image1, pdf], [first, second, first, PDF]
            if fmt != "claude":
                paths.extend([audio, mp3])
                expected.extend([wav_bytes(), MP3])
            if fmt in {"gemini", "vertex"}:
                paths.append(video)
                expected.append(MP4)
            before = len(h.requests)
            h.replies = [native_reply(originals()[2:3])]
            generated = await bot.run_media_protocol(request_body(paths))
            assert generated["success"], generated.get("error")
            assert len(h.requests) == before + 1
            body = h.requests[-1]
            texts, blobs, kinds = unpack(body)
            assert blobs == expected, (fmt, "binary order/content differ")
            assert kinds[:4] == ["image/png", "image/jpeg", "image/png", "application/pdf"]
            joined = "\n".join(texts)
            assert joined.count(LONG_REFERENCE) == 1
            assert joined.count(INSTRUCTION) == 1 and texts[-1].endswith(INSTRUCTION)
            assert "file:" not in joined and "prompt:" not in joined
            assert not any(marker in json.dumps(body) for marker in (
                "UNRELATED-", "Conversation attachments", "agent_addon", "read-x",
            ))
            assert all(message["role"] == "user" for message in body.get("messages", body.get("contents", [])))
            if "model" in body:
                assert body["model"] == MEDIA_MODEL
            assert body.get("max_tokens", body.get("generationConfig", {}).get("maxOutputTokens")) == 256
            metadata = generated["input_files"]
            assert [item["path"] for item in metadata] == paths
            assert [item["order"] for item in metadata] == list(range(1, len(paths) + 1))
            assert all(item["sha256"] == hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() for item in metadata)
            assert len(await refs(h)) == 2, "explicit inputs created ordinary attachments"
            result[fmt] = True
        h.replies = [native_reply(originals()[2:3])]
        legacy = await bot.run_default_media_generation("draw with no reference files")
        assert legacy["success"] and not unpack(h.requests[-1])[1]
        result["legacy"] = True
        return result


async def check_failures_and_limits(bot, root):
    async with GeneratedHarness(bot, root) as h:
        image = save(h, "reference.png", originals()[0])
        missing = str(h.root / "absent.png")
        corrupt = save(h, "corrupt.png", b"\x89PNG\r\n\x1a\ncorrupt")
        video = save(h, "clip.mp4", MP4)
        audio = save(h, "voice.wav", wav_bytes())
        result = {}
        for fmt in FORMATS:
            configure_media(h, fmt)
            cases = [
                (request_body([image, missing]), {}, "File #2"),
                (request_body([image, corrupt]), {}, "File #2"),
                ("file: " + image, {}, "prompt:"),
                (request_body([image, image]), {"max_files": 1}, "File count"),
                (request_body([image]), {"max_file_bytes": 1}, "File #1"),
                (request_body([image]), {"supports_images": False}, "File #1"),
                (request_body([image, image]), {"max_images": 1}, "Image count"),
                (request_body([image]), {"max_request_bytes": 1}, "request body"),
                (request_body([image]), {"context_window": 1}, "context window"),
                (request_body([image]), {"max_input_tokens": 1}, "input limit"),
                (request_body([image]), {"max_output_tokens": 16, "output_reserve_tokens": 32}, "Output reservation"),
            ]
            if fmt in {"openai", "openai_compatible", "claude"}:
                cases.append((request_body([image, video]), {}, "File #2"))
            if fmt == "claude":
                cases.append((request_body([image, audio]), {}, "File #2"))
            for body, limits, reason in cases:
                bot.UserDataManager.set("model_request_limits", {f"media/{MEDIA_MODEL}": limits})
                before = len(h.requests)
                failure = await bot.run_media_protocol(body)
                assert not failure["success"] and reason in failure["error"], (fmt, failure, reason)
                assert len(h.requests) == before, "sent a partial request after a preparation failure"
            bot.UserDataManager.set("model_request_limits", {})
            encoded = base64.b64encode(Path(image).read_bytes()).decode("ascii")
            h.error = (400, "unsupported standard input field: " + encoded)
            before = len(h.requests)
            failure = await bot.run_media_protocol(request_body([image]))
            assert not failure["success"] and encoded not in str(failure)
            assert "unsupported standard input field" in failure["error"]
            assert len(h.requests) == before + 1, "failed request retried with fewer files"
            assert unpack(h.requests[-1])[1] == [Path(image).read_bytes()]
            h.error = None
            result[fmt] = True

        configure_media(h, "gemini")
        large_pdf = save(h, "large.pdf", PDF + bytes(600000))
        bot.UserDataManager.set("model_request_limits", {f"media/{MEDIA_MODEL}": {
            "context_window": 4000, "output_reserve_tokens": 64,
        }})
        h.replies = [native_reply(originals()[2:3])]
        generated = await bot.run_media_protocol(request_body([large_pdf]))
        assert generated["success"], generated
        assert unpack(h.requests[-1])[1] == [Path(large_pdf).read_bytes()]
        for converter in (bot.ModelClient._to_openai_content, bot.ModelClient._to_claude_content):
            try:
                converter([{"type": "binary", "mime_type": "video/mp4", "data": "AAAA"}])
            except MediaInputError:
                pass
            else:
                raise AssertionError("unsupported binary part was silently dropped")
        result["native_tokens_deferred"] = True
        return result


async def check_agent_flow(bot, root):
    async with GeneratedHarness(bot, root) as h:
        source = save(h, "external source.png", originals()[0])
        source2 = save(h, "external second.jpg", originals()[1])
        paths = [source, source2, source]
        generated_images = originals()[2:4]
        result = {}
        for fmt in FORMATS:
            for stream, style in RENDERERS:
                await h.db.clear_all_conversation_memory()
                await h.add_upload(originals()[4], "existing-upload.jpg")
                h.configure(fmt, stream=stream, style=style, agent=True)
                configure_media(h, fmt)
                h.replies = [protocol(paths), native_reply(generated_images), "editing complete"]
                before = len(h.requests)
                await h.turn("edit with explicit references")
                assert len(h.requests) == before + 3, (fmt, stream, style, len(h.requests) - before)
                assert_images(h.requests[before], [originals()[4]])
                assert unpack(h.requests[before + 1])[1] == [originals()[0], originals()[1], originals()[0]]
                assert_images(h.requests[before + 2], [originals()[4], *generated_images])
                rows = await h.db.get_global_messages(1000)
                media = [row for row in rows if row["msg_type"] == "media_reply"]
                assert len(media) == 1, (fmt, media)
                metadata = json.loads(media[0]["metadata"])
                assert [item["path"] for item in metadata["media_inputs"]] == paths
                assert len(metadata["attachments"]) == 2
                assert "data:image/" not in json.dumps(rows)
                assert len(await refs(h)) == 3
                h.configure(fmt, agent=False)
                h.replies = ["second user answer"]
                await h.turn("inspect the generated images again")
                assert_images(h.requests[-1], [originals()[4], *generated_images])
                result[f"{fmt}/{stream}/{style}"] = True

        await h.db.clear_all_conversation_memory()
        h.configure(agent=True)
        configure_media(h, "openai")
        missing = str(h.root / "missing.png")
        h.replies = [protocol([source, missing]), "reference input failed"]
        before = len(h.requests)
        await h.turn("report missing input")
        assert len(h.requests) == before + 2
        assert all(body.get("model") != MEDIA_MODEL for body in h.requests[before:])
        rows = await h.db.get_global_messages(1000)
        failures = [row for row in rows if row["msg_type"] == "media_reply"]
        assert len(failures) == 1 and "File #2" in failures[0]["content"]
        assert missing in failures[0]["content"]
        result["failure_feedback"] = True
        return result


async def check_file_loading_races(bot, root):
    async with GeneratedHarness(bot, root) as h:
        path = save(h, "reference.png", originals()[0])
        configure_media(h, "openai")
        result = {}
        for mode in ("stop", "clear", "cancel", "unreadable"):
            bot.get_or_create_stop_event().clear()
            before = len(h.requests)
            started, release = threading.Event(), threading.Event()
            read_file = media_inputs._read_file

            def delayed_read(*args, started=started, release=release, mode=mode, read_file=read_file):
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test timed out")
                if mode == "unreadable":
                    raise PermissionError("access denied")
                return read_file(*args)

            with patch.object(media_inputs, "_read_file", side_effect=delayed_read):
                task = asyncio.create_task(bot.run_media_protocol(request_body([path])))
                try:
                    assert await asyncio.to_thread(started.wait, 10)
                    if mode == "stop":
                        bot.get_or_create_stop_event().set()
                    elif mode == "clear":
                        await h.db.clear_all_conversation_memory()
                    elif mode == "cancel":
                        task.cancel()
                finally:
                    release.set()
                try:
                    payload = await task
                except asyncio.CancelledError:
                    assert mode != "unreadable"
                else:
                    assert mode == "unreadable" and not payload["success"]
                    assert "File #1" in payload["error"] and "access denied" in payload["error"]
            assert len(h.requests) == before
            assert not await refs(h)
            result[mode] = True

        for mode in ("complete_stop", "delivery_failure"):
            await h.db.clear_all_conversation_memory()
            h.configure(agent=True)
            configure_media(h, "openai")
            h.replies = [protocol([path]), native_reply(originals()[2:4]), "completed"]
            generate = bot.run_default_media_generation

            async def finish(*args, generate=generate, mode=mode, **kwargs):
                payload = await generate(*args, **kwargs)
                if mode == "complete_stop":
                    bot.get_or_create_stop_event().set()
                return payload

            async def delivery(*_args, **_kwargs):
                assert len(await refs(h)) == 2, "delivery occurred before persistence"
                raise OSError("delivery failed after save")

            with patch.object(bot, "run_default_media_generation", finish), patch.object(
                bot, "send_generated_media_artifacts", AsyncMock(side_effect=delivery),
            ) as sender:
                await h.turn("finish and preserve generated originals")
                if mode == "complete_stop":
                    sender.assert_not_awaited()
            assert len(await refs(h)) == 2
            rows = await h.db.get_global_messages(1000)
            assert len([row for row in rows if row["msg_type"] == "media_reply"]) == 1
            h.replies.clear()
            h.configure(agent=False)
            bot.get_or_create_stop_event().clear()
            await h.call()
            assert_images(h.requests[-1], originals()[2:4])
            result[mode] = True
        return result


async def check_request_boundary_races(bot, root):
    async with GeneratedHarness(bot, root) as h:
        path = save(h, "reference.png", originals()[0])
        result = {}
        for fmt in FORMATS:
            for phase in ("stop", "clear"):
                configure_media(h, fmt)
                bot.ModelClient._thinking_unsupported.clear()
                bot.UserDataManager.set("thinking_level", "low")
                h.error = (400, "unsupported thinking configuration")
                before = len(h.requests)
                respond = h.http._transport.handler

                async def interrupt_response(request, respond=respond, phase=phase):
                    response = respond(request)
                    if phase == "stop":
                        bot.get_or_create_stop_event().set()
                    else:
                        await h.db.clear_all_conversation_memory()
                    return response

                with patch.object(h.http._transport, "handler", interrupt_response):
                    try:
                        await bot.run_media_protocol(request_body([path]))
                    except asyncio.CancelledError:
                        pass
                    else:
                        raise AssertionError("interrupted media request was retried")
                assert len(h.requests) == before + 1
                result[f"{fmt}/{phase}"] = True
        return result


async def check_large_media_request(bot, root):
    async with GeneratedHarness(bot, root) as h:
        buffer = io.BytesIO()
        Image.new("RGB", (1750, 1750), (50, 100, 180)).save(buffer, format="PNG", compress_level=0)
        data = buffer.getvalue()
        assert len(data) > 8 * 1024 * 1024
        path = save(h, "large-original.png", data)
        result = {}
        for fmt in ("openai", "gemini"):
            configure_media(h, fmt)
            bot.UserDataManager.set("model_request_limits", {f"media/{MEDIA_MODEL}": {
                "max_file_bytes": len(data), "max_request_bytes": 20000000,
            }})
            h.replies = [native_reply(originals()[2:3])]
            payload = await bot.run_media_protocol(request_body([path]))
            assert payload["success"], payload.get("error")
            assert unpack(h.requests[-1])[1] == [data]
            result[fmt] = True
        return result
