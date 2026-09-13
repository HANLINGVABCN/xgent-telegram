"""Lossless attachment references, legacy migration, and request preflight."""

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from xgent_app.attachments import (
    ATTACHMENT_CONTEXT_MARKER,
    AttachmentContextError,
    create_attachment_reference,
    create_generated_image_references,
    decode_full_text,
    is_attachment_record,
    prepare_attachment_context,
    with_attachment_context,
    with_current_question,
)
from xgent_app.context_limits import (
    ConversationRequest,
    estimate_input_tokens,
    limits_from_model_metadata,
    reserved_output_tokens,
    validate_limits,
    validate_request_body,
)


def image_bytes(fmt="PNG", color="red", *, size=(13, 17), **save_options):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=fmt, **save_options)
    return output.getvalue()


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "uploads"
        self.root.mkdir()
        self.counter = 0

    def reference(self, data, name="file.txt", **kwargs):
        self.counter += 1
        path = self.root / "2026-09-12" / f"123456_{self.counter:08x}_{name}"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return create_attachment_reference(
            {"abs_path": str(path)}, self.root, name, data, **kwargs,
        )

    def record(self, *refs, row_id=1, **metadata):
        return {
            "id": row_id, "role": "user", "msg_type": "user_file",
            "content": "[upload index only]",
            "metadata": json.dumps({"attachments": list(refs), **metadata}),
        }

    def restore(self, *refs):
        return prepare_attachment_context([self.record(*refs)], self.root)

    def test_text_over_both_old_limits_is_complete(self):
        text = " \r\n" + "abcdefghijklmnop" * 12000 + "\nEND-OF-FILE-19417  \n"
        parts, updates, errors = self.restore(self.reference(text.encode()))
        self.assertFalse(errors)
        self.assertFalse(updates)
        self.assertEqual(
            f"[File content begins]\n{text}\n[File content ends]", parts[-1]["text"],
        )

    def test_full_caption_and_source_context_are_preserved(self):
        caption = " \n" + "caption " * 150 + "\r\nLAST CAPTION LINE \n"
        ref = self.reference(b"content", caption=caption, context_prefix="[forward origin]")
        parts, _, errors = self.restore(ref)
        self.assertFalse(errors)
        self.assertIn(caption, parts[0]["text"])
        self.assertIn("[forward origin]", parts[0]["text"])
        self.assertEqual(caption, ref["caption"])

    def test_text_encodings_decode_the_entire_file(self):
        text = "\u4e2d\u6587 text \r\n" * 13000 + "TAIL"
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32", "gb18030"):
            with self.subTest(encoding=encoding):
                data = text.encode(encoding)
                decoded, chosen = decode_full_text(data)
                self.assertEqual(text, decoded)
                self.assertEqual(text, decode_full_text(data, chosen)[0])

    def test_actual_image_mime_and_original_bytes_override_filename(self):
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"),
                          ("WEBP", "image/webp"), ("GIF", "image/gif")):
            with self.subTest(fmt=fmt):
                data = image_bytes(fmt)
                ref = self.reference(data, "misnamed.txt", mime_type="text/plain")
                self.assertEqual("image", ref["kind"])
                self.assertEqual(mime, ref["mime_type"])
                parts, _, errors = self.restore(ref)
                self.assertFalse(errors)
                self.assertEqual(data, base64.b64decode(parts[-1]["data"]))
                self.assertEqual((13, 17), (ref["width"], ref["height"]))

    def test_binary_and_corrupt_images_leave_blocking_references(self):
        for data, name in ((b"%PDF-1.7 fake", "test.pdf"), (b"\x00\x01", "test.bin"),
                           (b"not an image", "test.png"), (b"PK\x03\x04bad", "test.zip"),
                           (image_bytes("JPEG")[:-3], "truncated.jpg")):
            with self.subTest(name=name):
                ref = self.reference(data, name)
                self.assertEqual("invalid", ref["kind"])
                parts, _, errors = self.restore(ref)
                self.assertFalse(parts)
                self.assertTrue(errors)
                self.assertIn(name, errors[0])

    def test_missing_original_blocks_instead_of_omitting(self):
        ref = self.reference(b"original")
        (self.root / ref["path"]).unlink()
        parts, _, errors = self.restore(ref)
        self.assertFalse(parts)
        self.assertIn("missing or unreadable", errors[0])

    def test_changed_original_blocks_even_when_size_is_unchanged(self):
        ref = self.reference(b"original")
        (self.root / ref["path"]).write_bytes(b"replaced")
        _, _, errors = self.restore(ref)
        self.assertIn("has changed", errors[0])

    def test_reference_cannot_escape_upload_root(self):
        ref = self.reference(b"original")
        ref["path"] = "../outside.txt"
        _, _, errors = self.restore(ref)
        self.assertIn("outside", errors[0])

    def test_repeated_reference_is_injected_once(self):
        ref = self.reference(image_bytes(), "picture.png")
        parts, _, errors = prepare_attachment_context(
            [self.record(ref), self.record(copy.deepcopy(ref), row_id=2)], self.root,
        )
        self.assertFalse(errors)
        self.assertEqual(1, sum(part["type"] == "image" for part in parts))

    def test_distinct_uploads_with_identical_bytes_keep_both_captions(self):
        refs = [self.reference(image_bytes(), "same.png", caption=caption)
                for caption in ("first caption", "second caption")]
        parts, _, errors = self.restore(*refs)
        self.assertFalse(errors)
        self.assertEqual(2, sum(part["type"] == "image" for part in parts))
        self.assertIn("first caption", parts[0]["text"])
        self.assertIn("second caption", parts[2]["text"])

    def test_no_recent_attachment_or_album_count_limit(self):
        refs = [self.reference(image_bytes(color=(i, 0, 0)), f"{i}.png", order=i)
                for i in range(32)]
        parts, _, errors = self.restore(*reversed(refs))
        self.assertFalse(errors)
        self.assertEqual(32, sum(part["type"] == "image" for part in parts))
        actual = [part["data"] for part in parts if part["type"] == "image"]
        expected = [base64.b64encode((self.root / ref["path"]).read_bytes()).decode()
                    for ref in refs]
        self.assertEqual(expected, actual)

    def test_album_order_survives_out_of_order_download_completion(self):
        refs = [self.reference(f"CONTENT-{i}".encode(), f"{i}.txt") for i in range(3)]
        records = [
            self.record(refs[2], row_id=1, attachment_group="album", attachment_order=103),
            self.record(refs[0], row_id=2, attachment_group="album", attachment_order=101),
            self.record(refs[1], row_id=3, attachment_group="album", attachment_order=102),
        ]
        parts, _, errors = prepare_attachment_context(records, self.root)
        self.assertFalse(errors)
        text = "\n".join(part["text"] for part in parts)
        self.assertLess(text.index("CONTENT-0"), text.index("CONTENT-1"))
        self.assertLess(text.index("CONTENT-1"), text.index("CONTENT-2"))

    def test_receipt_order_survives_out_of_order_persistence(self):
        refs = [self.reference(f"CONTENT-{i}".encode(), f"{i}.txt") for i in range(3)]
        records = [
            self.record(refs[2], row_id=1, attachment_received_at_ns=300),
            self.record(refs[0], row_id=2, attachment_received_at_ns=100),
            self.record(refs[1], row_id=3, attachment_received_at_ns=200),
        ]
        parts, _, errors = prepare_attachment_context(records, self.root)
        self.assertFalse(errors)
        text = "\n".join(part["text"] for part in parts)
        self.assertLess(text.index("CONTENT-0"), text.index("CONTENT-1"))
        self.assertLess(text.index("CONTENT-1"), text.index("CONTENT-2"))

    def test_album_uses_earliest_receipt_and_legacy_uses_saved_timestamp(self):
        refs = [self.reference(f"CONTENT-{i}".encode(), f"{i}.txt") for i in range(4)]
        records = [
            self.record(refs[2], attachment_group="album", attachment_order=102,
                        attachment_received_at_ns=300),
            self.record(refs[3], attachment_received_at_ns=200),
            self.record(refs[1], attachment_group="album", attachment_order=101,
                        attachment_received_at_ns=100),
            {**self.record(refs[0]), "timestamp": 0.00000005},
        ]
        parts, _, errors = prepare_attachment_context(records, self.root)
        self.assertFalse(errors)
        text = "\n".join(part["text"] for part in parts)
        offsets = [text.index(f"CONTENT-{i}") for i in range(4)]
        self.assertEqual(sorted(offsets), offsets)

    def legacy(self, ref, kind="\u6587\u4ef6"):
        return {
            "id": 14, "role": "user", "msg_type": "user_file", "metadata": None,
            "content": f"[{kind}] {ref['name']}\uff0c\u5df2\u4fdd\u5b58\u5230 "
                       f"{(self.root / ref['path']).as_posix()}\u3002\u8bf4\u660e\uff1acaption",
        }

    def test_trusted_legacy_upload_is_backfilled_losslessly(self):
        text = "legacy " * 20000 + "LEGACY-TAIL"
        ref = self.reference(text.encode())
        parts, updates, errors = prepare_attachment_context([self.legacy(ref)], self.root)
        self.assertFalse(errors)
        self.assertEqual(14, updates[0][0])
        self.assertIsNone(updates[0][1])
        self.assertIn(text, parts[-1]["text"])
        migrated = updates[0][2]["attachments"][0]
        self.assertTrue(migrated["legacy"])
        self.assertEqual(ref["path"], migrated["path"])

    def test_arbitrary_text_cannot_create_legacy_attachment_associations(self):
        record = self.legacy(self.reference(b"secret"))
        record["msg_type"] = "user_text"
        parts, updates, errors = prepare_attachment_context([record], self.root)
        self.assertEqual(([], [], []), (parts, updates, errors))

    def test_unverifiable_or_unlinked_legacy_uploads_are_reported(self):
        record = self.legacy(self.reference(b"hello"))
        record["content"] = record["content"].replace("2026-09-12/", "")
        for content in (record["content"], "[\u56fe\u7247]: old caption",
                        "[\u6587\u4ef6] missing.txt"):
            with self.subTest(content=content):
                record["content"] = content
                _, _, errors = prepare_attachment_context([record], self.root)
                self.assertTrue(errors)

    def test_incomplete_legacy_album_is_not_partially_supplied(self):
        record = self.legacy(self.reference(image_bytes(), "a.png"), "\u56fe\u7247")
        record["msg_type"] = "user_photo"
        record["content"] = "[\u76f8\u518c] \u51712\u5f20\u56fe\u7247\n" + record["content"]
        parts, _, errors = prepare_attachment_context([record], self.root)
        self.assertFalse(parts)
        self.assertIn("album index is incomplete", errors[0])

    def test_configuration_uploads_are_excluded(self):
        records = [
            {"id": 1, "role": "user", "msg_type": "user_file",
             "content": "[\u63d0\u4f9b\u5546\u914d\u7f6e\u6587\u4ef6] keys.json"},
            {"id": 2, "role": "user", "msg_type": "user_file",
             "content": "[file] prompt.txt", "metadata": {"attachment_purpose": "configuration"}},
        ]
        self.assertEqual(([], [], []), prepare_attachment_context(records, self.root))

    def test_corrupt_metadata_is_explicitly_reported(self):
        for metadata in ("{broken", "[]", {"attachments": []}, {"attachments": {}},
                         {"attachments": [{"order": True}]}):
            with self.subTest(metadata=metadata):
                record = self.record(self.reference(b"hello"))
                record["metadata"] = metadata
                _, _, errors = prepare_attachment_context([record], self.root)
                self.assertTrue(errors)

    def test_damaged_reference_fields_never_escape_as_unhandled_errors(self):
        original = self.reference(image_bytes(), "a.png")
        for key, value in (
            ("mime_type", None), ("mime_type", "text/plain"), ("width", "13"),
            ("height", 0), ("version", True), ("size", None), ("sha256", {}),
            ("caption", []), ("context_prefix", None),
        ):
            with self.subTest(key=key, value=value):
                ref = {**original, key: value}
                parts, _, errors = self.restore(ref)
                self.assertFalse(parts)
                self.assertIn("a.png", errors[0])
        ref = self.reference(b"hello")
        ref["encoding"] = 123
        self.assertTrue(self.restore(ref)[2])
        record = self.record(original, attachment_order="1")
        self.assertTrue(prepare_attachment_context([record], self.root)[2])
        for received_at in (None, True, -1, "123"):
            record = self.record(original, attachment_received_at_ns=received_at)
            self.assertTrue(prepare_attachment_context([record], self.root)[2])

    def test_reassembly_does_not_duplicate_or_mutate_history(self):
        original = [{"role": "user", "content": "current question"}]
        parts = [{"type": "text", "text": "complete content"}]
        once = with_attachment_context(original, parts)
        twice = with_attachment_context(once, parts)
        self.assertEqual(once, twice)
        self.assertEqual(2, len(twice))
        self.assertNotIn(ATTACHMENT_CONTEXT_MARKER, original[0])
        self.assertEqual(original, with_attachment_context(twice, []))

    def test_current_question_is_preserved_after_other_history(self):
        history = [{"role": "user", "content": "older upload"},
                   {"role": "assistant", "content": "answer"}]
        actual = with_current_question(history, "latest question")
        self.assertEqual(history, actual[:2])
        self.assertEqual("latest question", actual[-1]["content"])
        repeated = with_current_question(actual + [{"role": "assistant", "content": "ok"}],
                                         "latest question")
        self.assertEqual("user", repeated[-1]["role"])
        self.assertEqual(5, len(repeated))


class GeneratedAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.generated = self.root / "generated_media"
        self.uploads.mkdir()
        self.generated.mkdir()
        self.counter = 0

    def artifact(self, data=None, **details):
        self.counter += 1
        path = self.generated / "2026-09-13" / f"123456_{self.counter:08x}_assistant_image.png"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(image_bytes() if data is None else data)
        return {
            "path": str(path), "mime_type": "image/png", "source": "chat_native_media",
            "provider_name": "provider", "model_name": "model", "prompt": "generation prompt",
            **details,
        }

    def record(self, *refs, row_id=1, **metadata):
        return {
            "id": row_id, "role": "assistant", "msg_type": "ai_reply",
            "content": "generated image index",
            "metadata": {"attachments": list(refs), **metadata},
        }

    def restore(self, records):
        return prepare_attachment_context(records, self.uploads, self.generated)

    def legacy(self, artifact, *, role="assistant", msg_type="ai_reply"):
        return {
            "id": 1, "role": role, "msg_type": msg_type, "metadata": None,
            "content": (
                "generated\n\n"
                f"\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a\u672c\u56fe\u7247"
                f"\u5df2\u81ea\u52a8\u5b58\u5165 {Path(artifact['path']).as_posix()}"
                "\uff0c\u9700\u8981\u65f6\u8bf7read\u4ee5\u8fd4\u56de\u4e0a\u4e0b\u6587"
                "\uff0c\u65e0\u8bc6\u56fe\u80fd\u529b\u65f6\u8bf7\u52ffread\u4ee5\u514d\u62a5\u9519\u3011"
            ),
        }

    def test_generated_reference_records_actual_format_dimensions_checksum_and_source(self):
        data = image_bytes("JPEG")
        artifact = self.artifact(data, source="external_media_module")
        ref = create_generated_image_references([artifact], self.generated)[0]
        self.assertEqual("generated_media", ref["storage"])
        self.assertEqual("image/jpeg", ref["mime_type"])
        self.assertEqual((13, 17), (ref["width"], ref["height"]))
        self.assertEqual(hashlib.sha256(data).hexdigest(), ref["sha256"])
        self.assertEqual(len(data), ref["size"])
        self.assertEqual("external_media_module", ref["source"])
        for key in ("provider_name", "model_name", "prompt"):
            self.assertEqual(artifact[key], ref[key])
        parts, updates, errors = self.restore([self.record(ref)])
        self.assertFalse(errors or updates)
        self.assertEqual(data, base64.b64decode(parts[-1]["data"]))
        self.assertIn("AI-generated image", parts[0]["text"])

    def test_upload_and_generated_storage_remain_distinct_and_ordered(self):
        artifacts = [self.artifact(image_bytes(color=color)) for color in ("red", "green")]
        refs = create_generated_image_references(artifacts, self.generated)
        data = image_bytes("JPEG")
        path = self.uploads / refs[0]["path"]
        path.parent.mkdir()
        path.write_bytes(data)
        upload = create_attachment_reference(
            {"abs_path": str(path)}, self.uploads, "uploaded.jpg", data,
        )
        self.assertNotEqual(upload["id"], refs[0]["id"])
        records = [
            self.record(*reversed(refs), attachment_received_at_ns=200),
            {
                **self.record(upload, row_id=2, attachment_received_at_ns=100),
                "role": "user", "msg_type": "user_photo",
            },
            self.record(refs[0], row_id=3, attachment_received_at_ns=300),
        ]
        parts, _, errors = self.restore(records)
        self.assertFalse(errors)
        self.assertEqual(
            [data, *[Path(artifact["path"]).read_bytes() for artifact in artifacts]],
            [base64.b64decode(part["data"]) for part in parts if part["type"] == "image"],
        )

    def test_original_over_eight_megabytes_is_fully_included(self):
        data = image_bytes(size=(1800, 1800), compress_level=0)
        self.assertGreater(len(data), 8 * 1024 * 1024)
        refs = create_generated_image_references([self.artifact(data)], self.generated)
        parts, _, errors = self.restore([self.record(*refs)])
        self.assertFalse(errors)
        self.assertEqual(data, base64.b64decode(parts[-1]["data"]))

    def test_no_recent_generated_image_count_limit(self):
        data = [image_bytes(color=(i, 40, 90)) for i in range(32)]
        refs = create_generated_image_references([self.artifact(item) for item in data], self.generated)
        parts, _, errors = self.restore([self.record(*reversed(refs))])
        self.assertFalse(errors)
        self.assertEqual(data, [base64.b64decode(p["data"]) for p in parts if p["type"] == "image"])

    def test_missing_changed_and_corrupt_originals_are_explicit(self):
        artifact = self.artifact()
        refs = create_generated_image_references([artifact], self.generated)
        path = Path(artifact["path"])
        path.unlink()
        self.assertIn("missing or unreadable", self.restore([self.record(*refs)])[2][0])
        path.write_bytes(b"corrupted")
        self.assertIn("has changed", self.restore([self.record(*refs)])[2][0])
        with self.assertRaisesRegex(AttachmentContextError, "Cannot parse"):
            create_generated_image_references([artifact], self.generated)

    def test_generated_storage_rejects_paths_outside_root_and_unavailable_roots(self):
        artifact = self.artifact()
        ref = create_generated_image_references([artifact], self.generated)[0]
        for path in (str(self.uploads / "secret.png"), "../secret.png"):
            with self.assertRaisesRegex(AttachmentContextError, "outside"):
                create_generated_image_references([{**artifact, "path": path}], self.generated)
        errors = prepare_attachment_context([self.record(ref)], self.uploads)[2]
        self.assertIn("unavailable attachment storage", errors[0])
        for key, value in (("storage", "arbitrary"), ("source", {}), ("model_name", [])):
            with self.subTest(key=key):
                self.assertTrue(self.restore([self.record({**ref, key: value})])[2])

    def test_trusted_legacy_images_from_both_sources_are_restored_and_backfilled(self):
        for role, msg_type, source in (
            ("assistant", "ai_reply", "chat_native_media"),
            ("media_module", "media_reply", "external_media_module"),
        ):
            record = self.legacy(self.artifact(), role=role, msg_type=msg_type)
            self.assertTrue(is_attachment_record(record))
            parts, updates, errors = self.restore([record])
            self.assertFalse(errors)
            ref = updates[0][2]["attachments"][0]
            self.assertTrue(ref["legacy"])
            self.assertEqual(source, ref["source"])
            self.assertEqual(image_bytes(), base64.b64decode(parts[-1]["data"]))

    def test_ordinary_chat_and_new_echoed_notices_do_not_create_associations(self):
        record = self.legacy(self.artifact())
        cases = [
            {**record, "role": "user", "msg_type": "user_text"},
            {**record, "role": "system", "msg_type": "agent_result"},
            {**record, "content": f"Please examine {self.generated / 'image.png'}"},
            {**record, "metadata": {"generated_media_processed": True}},
        ]
        for case in cases:
            self.assertFalse(is_attachment_record(case))
            self.assertEqual(([], [], []), self.restore([case]))
        self.assertEqual(([], [], []), self.restore([]))

    def test_identifiable_but_unrecoverable_legacy_records_are_reported(self):
        artifact = self.artifact()
        record = self.legacy(artifact)
        for path in (self.generated / "wrong-name.png", self.uploads / "outside.png"):
            altered = self.legacy({**artifact, "path": str(path)})
            self.assertTrue(self.restore([altered])[2])
        Path(artifact["path"]).unlink()
        self.assertTrue(self.restore([record])[2])
        record["content"] = record["content"].replace("read", "invalid-read")
        self.assertTrue(self.restore([record])[2])

    def test_new_legacy_notice_and_mixed_multiline_order(self):
        first, second = self.artifact(), self.artifact(image_bytes(color='green'))
        old = self.legacy(first)
        new = self.legacy(second)
        start = new['content'].index('\uff0c\u9700\u8981\u65f6\u8bf7read')
        new['content'] = new['content'][:start] + (
            '\uff0c\u539f\u56fe\u81ea\u52a8\u8fdb\u5165\u5f53\u524d\u672a\u6e05\u7a7a'
            '\u5bf9\u8bdd\u7684\u6bcf\u8f6e\u4e0a\u4e0b\u6587\uff0c\u65e0\u9700\u518d\u6b21read\u3011'
        )
        record = {**old, 'content': old['content'] + '\r\n' + new['content']}
        parts, updates, errors = self.restore([record])
        self.assertFalse(errors)
        self.assertEqual(2, len(updates[0][2]['attachments']))
        self.assertEqual([Path(first['path']).read_bytes(), Path(second['path']).read_bytes()],
                         [base64.b64decode(p['data']) for p in parts if p['type'] == 'image'])

    def test_quoted_legacy_notices_are_not_attachment_records(self):
        record = self.legacy(self.artifact())
        notice = record['content'].splitlines()[-1]
        for content in (f'Example: {notice}', f'> {notice}', f'```text\n{notice}\n```',
                        f'~~~\n{notice}\n~~~'):
            candidate = {**record, 'content': content}
            self.assertFalse(is_attachment_record(candidate))
            self.assertEqual(([], [], []), self.restore([candidate]))

    def test_audio_and_video_do_not_gain_persistent_image_references(self):
        refs = create_generated_image_references([
            {"path": "/unused/audio.wav", "mime_type": "audio/wav"},
            {"path": "/unused/video.mp4", "mime_type": "video/mp4"},
        ], self.generated)
        self.assertEqual([], refs)


class RequestLimitTests(unittest.TestCase):
    def history(self, limits):
        return ConversationRequest([
            {"role": "user", "content": [
                {"type": "text", "text": "TEXT" * 80},
                {"type": "image", "data": "ORIGINAL", "mime_type": "image/png",
                 "width": 13, "height": 17},
            ]},
        ], validate_limits(limits), "system prompt")

    def test_known_metadata_and_modalities_are_used_without_name_guesses(self):
        self.assertEqual(
            {"context_window": 64000, "supports_images": False},
            limits_from_model_metadata({
                "context_length": 64000, "architecture": {"input_modalities": ["text"]},
            }),
        )
        self.assertEqual(
            {"max_input_tokens": 1000000, "max_output_tokens": 8192},
            limits_from_model_metadata({"inputTokenLimit": 1000000, "outputTokenLimit": 8192}),
        )
        self.assertEqual({}, limits_from_model_metadata({"id": "a-model-name"}))
        self.assertEqual(
            {"max_images": 0, "supports_images": False},
            limits_from_model_metadata({"max_images": 0, "supports_images": False}),
        )

    def test_unknown_capacity_does_not_trim_or_reject(self):
        history = self.history({})
        before = copy.deepcopy(history)
        validate_request_body({"messages": history}, history)
        self.assertEqual(before, history)
        self.assertIsNone(reserved_output_tokens({}, None))

    def test_output_reservation_is_explicit_and_bounded(self):
        self.assertEqual(4096, reserved_output_tokens({"context_window": 128000}, None))
        self.assertEqual(1024, reserved_output_tokens({"max_output_tokens": 1024}, None))
        self.assertEqual(7000, reserved_output_tokens({"output_reserve_tokens": 7000}, None))
        self.assertEqual(9000, reserved_output_tokens({"max_output_tokens": 1024}, 9000))

    def test_full_request_rejects_each_known_limit_without_mutation(self):
        cases = [
            ({"max_images": 0}, "Image count"),
            ({"supports_images": False}, "does not support images"),
            ({"max_request_bytes": 10}, "request body"),
            ({"context_window": 200}, "context window"),
            ({"max_input_tokens": 100}, "input limit"),
            ({"max_output_tokens": 10}, "Output reservation"),
        ]
        for limits, reason in cases:
            with self.subTest(limits=limits):
                history = self.history(limits)
                before = copy.deepcopy(history)
                with self.assertRaisesRegex(AttachmentContextError, reason):
                    validate_request_body({"messages": history, "max_tokens": 20}, history)
                self.assertEqual(before, history)

    def test_system_prompt_and_final_thinking_output_budget_are_counted(self):
        history = self.history({"context_window": 20000})
        tokens = estimate_input_tokens(history, history.system_prompt, history.limits)
        history.limits["context_window"] = tokens + 500
        validate_request_body({"max_tokens": 500}, history)
        with self.assertRaisesRegex(AttachmentContextError, "output reservation"):
            validate_request_body({"max_tokens": 501}, history)

    def test_invalid_limit_configuration_never_silently_disables_checks(self):
        for limits in ([], None, {"max_input_tokens": "1000"}, {"context_window": True},
                       {"output_reserve_tokens": 0}, {"supports_images": "yes"},
                       {"context_window": None}, {"max_output_tokens": None},
                       {"image_token_budget": None}, {"context_widow": 1000}):
            with self.subTest(limits=limits):
                with self.assertRaises(AttachmentContextError):
                    validate_limits(limits)


if __name__ == "__main__":
    unittest.main()
