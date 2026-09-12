"""Lossless attachment references, legacy migration, and request preflight."""

import base64
import copy
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
    decode_full_text,
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


def image_bytes(fmt="PNG", color="red"):
    output = io.BytesIO()
    Image.new("RGB", (13, 17), color).save(output, format=fmt)
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
