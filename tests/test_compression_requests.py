import tempfile
import unittest
from pathlib import Path
from tests.test_thinking_params import run_in_app


def probe(name, root, module="compression_request_probe"):
    return run_in_app(
        'import asyncio\nfrom tests import ' + module + ' as probe\n'
        f'print(json.dumps(asyncio.run(probe.{name}(bot, {str(root)!r}))))'
    )


class CompressionRequestTests(unittest.TestCase):
    def check(self, name, module="compression_request_probe", count=None):
        with tempfile.TemporaryDirectory() as directory:
            result = probe(name, Path(directory), module)
            self.assertTrue(all(result.values()))
            if count is not None:
                self.assertEqual(count, len(result))

    def test_native_context_and_prompt_isolation_across_all_providers_and_renderers(self):
        self.check('provider_matrix', 'lossless_context_probe', 15)

    def test_all_failures_preserve_context_and_add_one_system_notice(self):
        self.check('failures', 'lossless_context_probe', 14)

    def test_atomic_commit_stop_clear_concurrent_messages_and_delivery(self):
        self.check('races', 'lossless_context_probe', 5)

    def test_retry_after_restart_uses_latest_history_and_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('retry_saved', directory, 'lossless_context_probe').values()))
            self.assertTrue(all(probe('retry_restarted', directory, 'lossless_context_probe').values()))

    def test_sse_fallback_rejects_truncation_errors_and_incomplete_responses(self):
        self.check('sse_compression', count=5)

    def test_clear_discards_chain_but_keeps_files(self):
        self.check('clear_chain')

    def test_generated_images_are_supplied_but_summary_protocols_never_execute(self):
        self.check('generated_and_agent')

    def test_all_three_reply_modes_across_providers(self):
        self.check('stream_modes', count=15)

    def test_manual_export_and_compression_share_identical_archive_contents(self):
        self.check('export_equivalence')

    def test_legacy_summary_migration_is_idempotent_and_preserves_archive_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('legacy_saved', directory).values()))
            self.assertTrue(all(probe('legacy_restarted', directory).values()))

    def test_pending_and_running_tasks_keep_history_and_require_manual_retry_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(probe('pending_saved', directory).values()))
            self.assertTrue(all(probe('pending_restarted', directory).values()))

    def test_manual_export_reports_a_failed_download_association(self):
        self.check('export_association_failure')

    def test_provider_stream_end_markers_and_truncation_never_commit_partial_output(self):
        self.check('stream_wire_termination', count=40)

    def test_unsupported_binary_is_not_degraded_to_a_filename(self):
        self.check('blocked_binary', 'lossless_context_probe')

    def test_live_stream_stop_and_timeout_do_not_show_or_persist_partial_summaries(self):
        self.check('live_stop', 'lossless_context_probe', 2)

    def test_archive_is_sent_before_compression_status_including_retry(self):
        self.check('export_before_compression_status', 'lossless_context_probe', 5)


if __name__ == '__main__':
    unittest.main()
