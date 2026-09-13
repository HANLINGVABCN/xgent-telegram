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

    def test_failures_preserve_original_context(self):
        with tempfile.TemporaryDirectory() as directory:
            results = probe('failures', Path(directory))
            self.assertEqual(15, len(results))
            self.assertTrue(all(results.values()))

    def test_stop_clear_write_and_delivery_races(self):
        with tempfile.TemporaryDirectory() as directory:
            results = probe('races', Path(directory))
            self.assertEqual(8, len(results))
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


if __name__ == '__main__':
    unittest.main()
