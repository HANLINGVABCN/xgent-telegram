"""End-to-end media-x file input checks with all five provider formats."""

import tempfile
import unittest
from pathlib import Path

from tests.test_thinking_params import run_in_app


def probe(name, root):
    return run_in_app(
        "import asyncio\n"
        "from tests import media_input_request_probe as probe\n"
        f"print(json.dumps(asyncio.run(probe.{name}(bot, {str(root)!r}))))"
    )


class MediaInputRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_all_provider_requests_are_complete_ordered_and_isolated(self):
        result = probe("check_outbound", self.root)
        self.assertEqual(6, len(result))
        self.assertTrue(all(result.values()))

    def test_failures_limits_and_unknown_native_token_costs(self):
        result = probe("check_failures_and_limits", self.root)
        self.assertEqual(6, len(result))
        self.assertTrue(all(result.values()))

    def test_agent_all_renderers_keep_outputs_not_explicit_inputs(self):
        result = probe("check_agent_flow", self.root)
        self.assertEqual(16, len(result))
        self.assertTrue(all(result.values()))

    def test_loading_stop_clear_and_delivery_races(self):
        result = probe("check_file_loading_races", self.root)
        self.assertEqual(6, len(result))
        self.assertTrue(all(result.values()))

    def test_stop_and_clear_immediately_before_provider_retry(self):
        result = probe("check_request_boundary_races", self.root)
        self.assertEqual(10, len(result))
        self.assertTrue(all(result.values()))

    def test_large_original_bytes_in_sdk_and_http_requests(self):
        result = probe("check_large_media_request", self.root)
        self.assertEqual(2, len(result))
        self.assertTrue(all(result.values()))


if __name__ == "__main__":
    unittest.main()
