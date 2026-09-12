"""Verify generated originals in actual requests, across providers and restarts."""

import tempfile
import unittest
from pathlib import Path

from tests.test_thinking_params import run_in_app


def probe(name, root):
    return run_in_app(
        "import asyncio\n"
        "from tests import generated_media_request_probe as probe\n"
        f"print(json.dumps(asyncio.run(probe.{name}(bot, {str(root)!r}))))"
    )


class GeneratedMediaRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.seed = probe("seed_generated_and_check", cls.root / "replay")
        cls.restart = probe("after_generated_restart", cls.root / "replay")

    def test_generated_uploads_and_media_continuation(self):
        self.assertTrue(self.seed["agent_continuation"])
        self.assertTrue(self.seed["media_input_unchanged"])
        self.assertTrue(self.seed["metadata"])

    def test_second_turn_depth_overflow_and_agent_off_all_formats(self):
        self.assertEqual(10, len(self.seed["formats"]))
        self.assertTrue(all(self.seed["formats"].values()))

    def test_process_restart_recovers_original_bytes_all_formats(self):
        self.assertEqual(10, len(self.restart))
        self.assertTrue(all(self.restart.values()))

    def test_history_and_export_do_not_store_base64(self):
        self.assertTrue(self.seed["index_only_history_export"])

    def test_all_native_renderers_persist_before_delivery_once(self):
        result = probe("check_native_renderers", self.root / "renderers")
        self.assertEqual(15, len(result))
        self.assertTrue(all(result.values()))

    def test_large_original_and_multiple_images_are_never_omitted(self):
        result = probe("check_large_original_and_many_images", self.root / "large")
        self.assertEqual(2, len(result))
        self.assertTrue(all(result.values()))

    def test_separate_native_image_stream_events(self):
        result = probe("check_separate_native_image_events", self.root / "events")
        self.assertEqual(10, len(result))
        self.assertTrue(all(result.values()))

    def test_media_model_stop_and_cancel_races(self):
        result = probe("check_media_stop_races", self.root / "media-stop")
        self.assertEqual(9, len(result))
        self.assertTrue(all(result.values()))

    def test_idle_generated_images_persist_before_delivery_and_cancel(self):
        result = probe("check_idle_generated_images", self.root / "idle")
        self.assertEqual(3, len(result))
        self.assertTrue(all(result.values()))

    def test_delivery_and_association_failures(self):
        result = probe("check_delivery_and_persistence_failures", self.root / "failures")
        self.assertEqual(3, len(result))
        self.assertTrue(all(result.values()))

    def test_limits_unsupported_models_and_missing_originals(self):
        result = probe("check_generated_limits", self.root / "limits")
        self.assertEqual(3, len(result))
        self.assertTrue(all(result.values()))

    def test_stop_cancel_and_partial_images(self):
        result = probe("check_stop_and_cancel", self.root / "stop")
        self.assertEqual(13, len(result))
        self.assertTrue(all(result.values()))

    def test_clear_generation_primary_mirror_and_assembly_races(self):
        result = probe("check_generated_clear_races", self.root / "clear")
        self.assertEqual(8, len(result))
        self.assertTrue(all(result.values()))

    def test_trusted_legacy_migration_and_explicit_failures(self):
        result = probe("check_generated_legacy", self.root / "legacy")
        self.assertEqual(3, len(result))
        self.assertTrue(all(result.values()))

    def test_read_body_is_still_current_agent_loop_only(self):
        result = probe("check_read_behavior_unchanged", self.root / "read")
        self.assertEqual(2, len(result))
        self.assertTrue(all(result.values()))


if __name__ == "__main__":
    unittest.main()
