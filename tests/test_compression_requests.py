import tempfile
import unittest
from pathlib import Path

from tests.test_thinking_params import run_in_app


def probe(name, root):
    return run_in_app(
        'import asyncio\nfrom tests import compression_request_probe as probe\n'
        f'print(json.dumps(asyncio.run(probe.{name}(bot, {str(root)!r}))))'
    )


class CompressionRequestTests(unittest.TestCase):
    def test_all_providers_round_trips_and_fresh_process(self):
        with tempfile.TemporaryDirectory() as directory:
            results = probe('round_trips', Path(directory))
            self.assertEqual(5, len(results))
            self.assertTrue(all(results.values()))
            self.assertTrue(all(probe('restart', Path(directory)).values()))

    def test_failures_preserve_context_before_clear_and_archive_after_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            results = probe('failures', Path(directory))
            self.assertEqual(19, len(results))
            self.assertTrue(all(results.values()))

    def test_stop_clear_write_and_delivery_races(self):
        with tempfile.TemporaryDirectory() as directory:
            results = probe('races', Path(directory))
            self.assertEqual(11, len(results))
            self.assertTrue(all(results.values()))

    def test_sse_fallback_rejects_truncation_errors_and_incomplete_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            results = probe('sse_compression', Path(directory))
            self.assertEqual(5, len(results))
            self.assertTrue(all(results.values()))

    def test_clear_discards_chain_but_keeps_files(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('clear_chain', Path(directory)).values()))

    def test_generated_images_and_agent_do_not_execute_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('generated_and_agent', Path(directory)).values()))

    def test_all_three_reply_modes_across_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            results = probe('stream_modes', Path(directory))
            self.assertEqual(15, len(results))
            self.assertTrue(all(results.values()))

    def test_failed_restore_survives_restart_and_retries_without_clearing(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('retry_saved', Path(directory)).values()))
            self.assertTrue(all(probe('retry_restarted', Path(directory)).values()))

    def test_manual_export_and_compression_share_identical_archive_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('export_equivalence', Path(directory)).values()))

    def test_legacy_summary_migration_is_idempotent_and_preserves_archive_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('legacy_saved', Path(directory)).values()))
            self.assertTrue(all(probe('legacy_restarted', Path(directory)).values()))

    def test_pending_and_running_tasks_require_manual_retry_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('pending_saved', Path(directory)).values()))
            self.assertTrue(all(probe('pending_restarted', Path(directory)).values()))

    def test_partial_streams_remain_ordinary_incomplete_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(8, len(probe('partial_streams', Path(directory))))

    def test_stop_clear_and_failure_during_durable_reply_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(4, len(probe('handoff_races', Path(directory))))

    def test_manual_export_reports_a_failed_download_association(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('export_association_failure', Path(directory)).values()))

    def test_provider_stream_end_markers_and_truncation_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(40, len(probe('stream_wire_termination', Path(directory))))


if __name__ == '__main__':
    unittest.main()
