"""Assert actual SDK/HTTP payloads, not just intermediate content builders."""

import tempfile
import unittest
from pathlib import Path

from tests.test_thinking_params import run_in_app


def probe(name, root):
    return run_in_app(
        "import asyncio\n"
        "from tests import attachment_request_probe as probe\n"
        f"print(json.dumps(asyncio.run(probe.{name}(bot, {str(root)!r}))))"
    )


class AttachmentRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.seed = probe("seed_and_check", cls.root / "replay")
        # A fresh Python process, with no in-memory messages, cache, or image data.
        cls.restart = probe("after_restart", cls.root / "replay")

    def test_second_turn_and_beyond_history_window_all_providers(self):
        self.assertEqual(20, len(self.seed["providers"]))
        for key, result in self.seed["providers"].items():
            with self.subTest(path=key):
                self.assertEqual(12, result["images"])
                self.assertTrue(result["text_tail"])
                self.assertTrue(result["caption"])
                self.assertTrue(result["current_question"])

    def test_process_restart_restores_all_originals_for_all_formats(self):
        self.assertEqual(10, len(self.restart))
        for key, result in self.restart.items():
            with self.subTest(path=key):
                self.assertEqual(12, result["images"])
                self.assertTrue(result["text_tail"])

    def test_agent_off_does_not_disable_attachment_context(self):
        self.assertTrue(self.seed["agent_off"])

    def test_chat_display_and_database_have_no_base64_or_inline_full_text(self):
        self.assertTrue(self.seed["index_only_storage"])

    def test_export_does_not_embed_base64_or_inline_full_text(self):
        self.assertTrue(self.seed["index_only_export"])

    def test_renderers_agent_continuation_and_clear(self):
        result = probe("check_runtime_paths", self.root / "runtime")
        self.assertEqual(4, len(result))
        self.assertTrue(all(result.values()), result)

    def test_explicit_failures_across_all_request_paths(self):
        result = probe("check_failures", self.root / "failures")
        self.assertEqual(6, len(result))
        self.assertTrue(all(result.values()), result)

    def test_legacy_migration_and_persisted_model_limits(self):
        result = probe("check_migration", self.root / "migration")
        self.assertEqual(4, len(result))
        self.assertTrue(all(result.values()), result)

    def test_telegram_web_and_album_uploads(self):
        result = probe("check_upload_entrypoints", self.root / "ingress")
        self.assertEqual(4, len(result))
        self.assertTrue(all(result.values()), result)

    def test_document_media_groups_preserve_full_text_images_and_order(self):
        result = probe("check_document_groups", self.root / "document-groups")
        self.assertTrue(result["document_group_order_and_full_content"])

    def test_concurrent_uploads_preserve_receipt_order(self):
        result = probe("check_concurrent_upload_order", self.root / "concurrent-uploads")
        self.assertEqual(3, len(result))
        self.assertTrue(all(result.values()), result)

    def test_idle_requests_include_attachments_and_report_upstream_errors(self):
        result = probe("check_idle_requests", self.root / "idle-requests")
        self.assertEqual(2, len(result))
        self.assertTrue(all(result.values()), result)

    def test_restart_before_album_flush_keeps_all_uploads(self):
        root = self.root / "pending-albums"
        self.assertTrue(probe("seed_pending_albums", root)["persisted_before_any_flush"])
        result = probe("after_pending_albums_restart", root)
        self.assertTrue(result["pending_album_originals_survive_restart"])

    def test_clear_during_download_and_request_assembly(self):
        result = probe("check_clear_races", self.root / "clear-races")
        self.assertEqual(5, len(result))
        self.assertTrue(all(result.values()), result)

    def test_malformed_ingress_and_database_failure_are_explicit(self):
        result = probe("check_malformed_ingress", self.root / "malformed-ingress")
        self.assertEqual(5, len(result))
        self.assertTrue(all(result.values()), result)

    def test_upstream_failures_are_not_saved_as_successful_replies(self):
        result = probe("check_upstream_failures", self.root / "upstream-failures")
        self.assertEqual(4, len(result))
        self.assertTrue(all(result.values()), result)

    def test_serialized_request_limits_and_thinking_retries(self):
        result = probe("check_final_request_limits", self.root / "final-limits")
        self.assertEqual(3, len(result))
        self.assertTrue(all(result.values()), result)


if __name__ == "__main__":
    unittest.main()
