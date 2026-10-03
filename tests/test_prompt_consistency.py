"""Keep shipped model instructions consistent with the actual execution paths."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

from xgent_app.compression import DEFAULT_COMPRESSION_PROMPT
from xgent_app.protocols import ProtocolParser

ROOT = Path(__file__).resolve().parents[1]


def text(path):
    return (ROOT / path).read_text(encoding="utf-8")


class PromptConsistencyTests(unittest.TestCase):
    def test_every_documented_executable_example_uses_the_current_parser(self):
        types = set()
        for path in ("prompts/agent_addon.txt", "skill-public/bot-system.md",
                     "skill-public/trigger-x-protocol.md"):
            with self.subTest(path=path):
                source = text(path)
                examples = [line for line in source.splitlines()
                            if line.startswith("```") and ProtocolParser._OPEN_RE.match(line)]
                blocks = ProtocolParser.extract_protocol_blocks(source)
                self.assertTrue(examples)
                self.assertEqual(len(examples), len(blocks), "Unclosed, duplicate or obsolete protocol example")
                types.update(block["type"] for block in blocks)
                self.assertFalse(ProtocolParser.has_unclosed_block(source))
        self.assertTrue({"run", "shell", "stdin", "shellkill", "read", "edit", "grep", "intel",
                         "file", "file_base64", "sendfile", "media", "search", "fetch", "ask", "over"} <= types)

    def test_trigger_is_documented_as_cli_not_as_a_legacy_protocol(self):
        source = text("skill-public/bot-system.md")
        for command in ("trigger add --cmd", "trigger show", "trigger kill <id>", "trigger kill-all"):
            self.assertIn(command, source)
        for obsolete in ("trigger:show", "trigger:kill:", "#@summary", "file:/path <<EOF"):
            self.assertNotIn(obsolete, source)
        # Legacy examples are data, not executable protocols. Do not teach them.
        for legacy in ("```trigger-x\n<<BEGIN_probe_123456\necho test\n<<END_probe_123456\n```",
                       "```file:/tmp/example\n<<BEGIN_probe_123456\nx\n<<END_probe_123456\n```",
                       "```file-x:/tmp/example <<EOF\nx\nEOF\n```"):
            self.assertEqual([], ProtocolParser.extract_protocol_blocks(legacy))

    def test_trigger_concurrency_description_is_consistent_in_prompt_and_skill_summary(self):
        for path in ("prompts/agent_addon.txt", "skill-public/trigger-x-protocol.md",
                     "skill-public/bot-system.md"):
            with self.subTest(path=path):
                source = text(path)
                self.assertNotIn("有界并发", source)
                self.assertIn("不同任务之间没有全局并发上限", source)
                self.assertIn("同一任务", source)
        self.assertNotIn('2026-01-01', text("prompts/agent_addon.txt"))

    def test_retention_is_conditional_and_does_not_claim_to_load_unread_files(self):
        source = text("prompts/global_addon.txt")
        for clause in ("工具结果留存", "关闭只影响之后", "不删除已经留存", "实际回传", "历史", "最新版本"):
            self.assertIn(clause, source)
        self.assertIn("不等于把所有文件或命令日志全文", source)
        loader = text("skill-public/skill-loader.md")
        self.assertIn("readx_persist_context", loader)
        self.assertIn("When enabled", loader)
        self.assertIn("When disabled", loader)
        self.assertIn("unread rest of the file", loader)
        self.assertNotIn("does not make its complete text persistent", loader)
        creator = text("skill-public/skill-creator.md")
        self.assertIn("未读取的正文不会自动占用", creator)
        self.assertIn("开启工具结果留存", creator)
        self.assertIn("真实绝对路径", creator)

    def test_summary_documentation_matches_scanning_multiple_nonleading_blocks(self):
        source = text("xgent_app/sections/services.py")
        module = ast.parse(source)
        function = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                        and n.name == "extract_skill_summary_blocks")
        env = {"re": re, "SKILL_SUMMARY_BLOCK_TAG": "!"}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<skill-summary-parser>", "exec"), env)
        summary = env["extract_skill_summary_blocks"](
            "introduction\n```!\nFIRST\n```\nbody\n```!\nSECOND\n```\n"
            "````text\n```!\nEXAMPLE-ONLY\n```\n````\n")
        self.assertEqual("FIRST SECOND", summary)
        creator = text("skill-public/skill-creator.md")
        self.assertIn("全部有效且闭合", creator)
        self.assertNotIn("只有文件最开头", creator)

    def test_system_guide_covers_file_priority_current_directories_and_compression(self):
        source = text("skill-public/bot-system.md")
        for clause in ("文件优先", "非空内容", "回退", "不必重启", "skill-public/", "skill-private/",
                       "有效模型消息条数", "model_context", "readx_persist_context", "compression.txt",
                       "原生附件", "事务提交后替换原历史", "只增加系统失败提示", "当前压缩指令"):
            self.assertIn(clause, source)
        self.assertNotIn("通常优先从 `UserDataManager` 读取", source)
        self.assertNotIn("文件本体通常不会完整写入长期记忆", source)
        self.assertEqual(DEFAULT_COMPRESSION_PROMPT, text("prompts/compression.txt").strip())

    def test_agent_off_separates_disabled_tools_from_existing_model_capabilities(self):
        source = text("prompts/agent_disabled_addon.txt")
        self.assertIn("不要输出", source)
        for tag in ("run-x", "read-x", "media-x", "ask-x", "over-x"):
            self.assertIn(tag, source)
        self.assertIn("当前对话模型本身支持原生图片输出", source)
        self.assertIn("这不代表可以调用 media-x", source)
        self.assertIn("不得假装模型具备未提供的能力", source)
        self.assertIn("已经提供的文本、图片", source)

    def test_channel_and_memory_guidance_does_not_override_agent_permissions(self):
        global_prompt = text("prompts/global_addon.txt")
        for channel in ("Telegram", "Web", "CLI/TUI"):
            self.assertIn(channel, global_prompt)
        self.assertNotIn("运行环境：Telegram 私人机器人", global_prompt)
        memory = text("skill-public/memory.md")
        for clause in ("file-x", "BEGIN/END", "Agent 关闭时不得", "压缩请求不额外注入"):
            self.assertIn(clause, memory)


if __name__ == "__main__":
    unittest.main()
