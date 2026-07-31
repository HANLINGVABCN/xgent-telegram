from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from xgent_app.agent_search import (
    MAX_FETCH_URLS,
    format_fetch_results,
    format_search_results,
    parse_fetch_body,
    parse_search_body,
    run_fetch,
    run_search,
)


class ParseSearchBodyTests(unittest.TestCase):
    def test_first_line_is_query_and_defaults_apply(self):
        options = parse_search_body("nginx 502 排查")
        self.assertEqual("nginx 502 排查", options["query"])
        self.assertEqual(5, options["max_results"])
        self.assertEqual("basic", options["depth"])
        self.assertEqual([], options["include_domains"])
        self.assertFalse(options["include_raw"])

    def test_options_are_parsed_and_clamped(self):
        options = parse_search_body(
            "python asyncio\nmax: 999\ndepth: advanced\nsite: docs.python.org\n-raw"
        )
        self.assertEqual("python asyncio", options["query"])
        self.assertEqual(20, options["max_results"])
        self.assertEqual("advanced", options["depth"])
        self.assertEqual(["docs.python.org"], options["include_domains"])
        self.assertTrue(options["include_raw"])

    def test_invalid_max_falls_back_to_default(self):
        options = parse_search_body("query\nmax: abc")
        self.assertEqual(5, options["max_results"])

    def test_query_containing_colon_is_not_swallowed_as_option(self):
        options = parse_search_body("错误: connection refused")
        self.assertEqual("错误: connection refused", options["query"])


class ParseFetchBodyTests(unittest.TestCase):
    def test_only_http_urls_are_kept_and_deduplicated(self):
        urls = parse_fetch_body(
            "https://a.example/x\n"
            "not-a-url\n"
            "ftp://b.example/y\n"
            "https://a.example/x\n"
            "# comment\n"
            "http://c.example\n"
        )
        self.assertEqual(["https://a.example/x", "http://c.example"], urls)

    def test_url_count_is_capped(self):
        body = "\n".join(f"https://e{i}.example" for i in range(MAX_FETCH_URLS + 5))
        self.assertEqual(MAX_FETCH_URLS, len(parse_fetch_body(body)))


class FormatTests(unittest.TestCase):
    def test_empty_results_render_guidance(self):
        text = format_search_results("q", [], "")
        self.assertIn("命中 0 条", text)
        self.assertIn("无结果", text)

    def test_results_include_title_url_and_answer(self):
        text = format_search_results(
            "q",
            [{"title": "T", "url": "https://x.example", "content": "C"}],
            "简短回答",
        )
        self.assertIn("【摘要】简短回答", text)
        self.assertIn("T", text)
        self.assertIn("https://x.example", text)
        self.assertIn("C", text)

    def test_fetch_failures_are_reported(self):
        text = format_fetch_results(
            [{"url": "https://ok.example", "raw_content": "body"}],
            [{"url": "https://bad.example", "error": "timeout"}],
        )
        self.assertIn("成功 1 个，失败 1 个", text)
        self.assertIn("https://bad.example", text)
        self.assertIn("timeout", text)


class RunSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_returns_actionable_hint_without_request(self):
        with patch("xgent_app.agent_search._post_json", new=AsyncMock()) as post:
            result = await run_search("query", "")
        self.assertFalse(result["success"])
        self.assertIn("TAVILY_API_KEY", result["output"])
        post.assert_not_awaited()

    async def test_empty_query_is_rejected_before_request(self):
        with patch("xgent_app.agent_search._post_json", new=AsyncMock()) as post:
            result = await run_search("   \n  ", "key")
        self.assertFalse(result["success"])
        self.assertIn("查询词为空", result["output"])
        post.assert_not_awaited()

    async def test_successful_search_sends_options_and_formats_results(self):
        payload = {
            "answer": "ans",
            "results": [{"title": "T", "url": "https://x.example", "content": "C"}],
        }
        with patch(
            "xgent_app.agent_search._post_json", new=AsyncMock(return_value=payload)
        ) as post:
            result = await run_search("q\nmax: 3\nsite: x.example", "key")

        self.assertTrue(result["success"])
        self.assertEqual("q", result["query"])
        self.assertIn("https://x.example", result["output"])
        sent = post.await_args.args[1]
        self.assertEqual(3, sent["max_results"])
        self.assertEqual(["x.example"], sent["include_domains"])

    async def test_invalid_key_maps_to_readable_message(self):
        error = httpx.HTTPStatusError(
            "401",
            request=httpx.Request("POST", "https://api.tavily.com/search"),
            response=httpx.Response(401),
        )
        with patch("xgent_app.agent_search._post_json", new=AsyncMock(side_effect=error)):
            result = await run_search("q", "bad-key")

        self.assertFalse(result["success"])
        self.assertIn("无效或已过期", result["output"])

    async def test_rate_limit_maps_to_readable_message(self):
        error = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("POST", "https://api.tavily.com/search"),
            response=httpx.Response(429),
        )
        with patch("xgent_app.agent_search._post_json", new=AsyncMock(side_effect=error)):
            result = await run_search("q", "key")

        self.assertFalse(result["success"])
        self.assertIn("额度", result["output"])

    async def test_network_failure_is_contained(self):
        with patch(
            "xgent_app.agent_search._post_json",
            new=AsyncMock(side_effect=httpx.ConnectError("dns")),
        ):
            result = await run_search("q", "key")

        self.assertFalse(result["success"])
        self.assertIn("搜索异常", result["output"])

    async def test_malformed_payload_does_not_raise(self):
        with patch(
            "xgent_app.agent_search._post_json",
            new=AsyncMock(return_value={"results": "not-a-list"}),
        ):
            result = await run_search("q", "key")

        self.assertTrue(result["success"])
        self.assertEqual([], result["results"])


class RunFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_returns_hint(self):
        result = await run_fetch("https://x.example", "")
        self.assertFalse(result["success"])
        self.assertIn("TAVILY_API_KEY", result["output"])

    async def test_no_valid_url_is_rejected(self):
        with patch("xgent_app.agent_search._post_json", new=AsyncMock()) as post:
            result = await run_fetch("garbage\nftp://x", "key")
        self.assertFalse(result["success"])
        self.assertIn("没有有效 URL", result["output"])
        post.assert_not_awaited()

    async def test_successful_fetch_returns_pages(self):
        payload = {"results": [{"url": "https://x.example", "raw_content": "hello"}]}
        with patch(
            "xgent_app.agent_search._post_json", new=AsyncMock(return_value=payload)
        ):
            result = await run_fetch("https://x.example", "key")

        self.assertTrue(result["success"])
        self.assertIn("hello", result["output"])

    async def test_all_pages_failing_is_not_success(self):
        payload = {
            "results": [],
            "failed_results": [{"url": "https://x.example", "error": "403"}],
        }
        with patch(
            "xgent_app.agent_search._post_json", new=AsyncMock(return_value=payload)
        ):
            result = await run_fetch("https://x.example", "key")

        self.assertFalse(result["success"])
        self.assertIn("403", result["output"])


if __name__ == "__main__":
    unittest.main()
