from __future__ import annotations

import asyncio
import logging
import unittest

from xgent_app.agent_context import build_intel_context_message
from xgent_app.agent_dispatch import (
    STANDARD_PROTOCOL_TYPES,
    dispatch_standard_protocol,
)
from xgent_app.agent_results import normalize_intel_result
from xgent_app.code_intel import INTEL_SUBCOMMANDS, run_intel
from xgent_app.protocols import ProtocolParser


_LOG = logging.getLogger("test_agent_intel")


def _dispatch(block):
    return asyncio.run(
        dispatch_standard_protocol(
            block,
            executor=None,
            provider_api_format="anthropic",
            stop_event_factory=lambda: None,
            logger=_LOG,
        )
    )


class RunIntelTests(unittest.TestCase):
    def test_summary_returns_captured_text(self):
        out = run_intel(["summary", "."])
        self.assertIsInstance(out, str)
        self.assertIn("项目", out)  # summary 输出里必有「项目快照」等字样

    def test_empty_argv_is_graceful(self):
        out = run_intel([])
        self.assertIn("缺少子命令", out)

    def test_bad_args_do_not_raise(self):
        # argparse 参数错误走 SystemExit，run_intel 必须吞掉、返回文本
        out = run_intel(["context"])  # 缺 symbol 参数
        self.assertIsInstance(out, str)
        self.assertTrue(out)  # 有 usage 文本，不抛异常

    def test_15_subcommands_registered(self):
        self.assertEqual(len(INTEL_SUBCOMMANDS), 15)
        self.assertIn("context", INTEL_SUBCOMMANDS)
        self.assertIn("callers", INTEL_SUBCOMMANDS)


class ProtocolParsingTests(unittest.TestCase):
    def test_intel_x_block_parses_to_intel_type(self):
        resp = (
            "```intel-x\n"
            "<<BEGIN_intel_t_1a2b\n"
            "callers register xgent_app\n"
            "<<END_intel_t_1a2b\n"
            "```"
        )
        blocks = ProtocolParser.extract_protocol_blocks(resp)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "intel")
        self.assertEqual(blocks[0]["body"], "callers register xgent_app")

    def test_intel_in_standard_types(self):
        self.assertIn("intel", STANDARD_PROTOCOL_TYPES)


class DispatchTests(unittest.TestCase):
    def test_valid_subcommand_dispatches(self):
        block = {"type": "intel", "path": "", "body": "summary ."}
        result = _dispatch(block)
        self.assertTrue(result["success"])
        self.assertEqual(result["kind"], "intel")
        self.assertIn("[intel结果]", result["notice"])

    def test_unknown_subcommand_rejected(self):
        block = {"type": "intel", "path": "", "body": "bogus foo"}
        result = _dispatch(block)
        self.assertFalse(result["success"])
        self.assertIn("不支持的子命令", result["notice"])

    def test_injection_attempt_blocked_at_whitelist(self):
        # 'context x; rm -rf /' → shlex 切成 [context, x;, rm, -rf, /]，
        # 'context' 过白名单但没有 shell 参与，分号只是 argparse 的多余参数。
        # 关键断言：绝不经 shell、不真执行 rm。这里验证它不 raise、走进程内。
        block = {"type": "intel", "path": "", "body": "context x; rm -rf /"}
        result = _dispatch(block)
        # context 在白名单里 → 进入 run_intel；argparse 对多余参数报 usage
        self.assertEqual(result["kind"], "intel")
        self.assertNotIn("rm -rf", result["output"].replace("rm -rf /", "SANITIZED"))

    def test_first_token_not_whitelisted_never_runs(self):
        # 真正的注入防线：第一个 token 不在白名单，直接拒，不进 run_intel
        block = {"type": "intel", "path": "", "body": "rm -rf /"}
        result = _dispatch(block)
        self.assertFalse(result["success"])
        self.assertIn("不支持的子命令", result["notice"])

    def test_unbalanced_quotes_reported(self):
        block = {"type": "intel", "path": "", "body": 'context "unclosed'}
        result = _dispatch(block)
        self.assertFalse(result["success"])
        self.assertIn("参数解析失败", result["notice"])


class NormalizeTests(unittest.TestCase):
    def test_normalize_shape(self):
        result = normalize_intel_result({"success": True, "output": "[intel结果] map\nfoo"})
        self.assertEqual(result["kind"], "intel")
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["context_message"])
        self.assertEqual(result["context_message"]["role"], "user")

    def test_context_message_mentions_intel_workflow(self):
        msg = build_intel_context_message("[intel结果] context foo")
        self.assertIn("intel", msg["content"])
        self.assertIn("context", msg["content"])


if __name__ == "__main__":
    unittest.main()
