"""思考深度参数映射的表驱动测试。

_build_thinking_params 定义在 xgent_app/sections/models.py 里，靠共享命名空间加载，
无法直接 import；沿用 test_runtime_smoke.py 的子进程范式，在子进程里加载完整应用
再断言。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_in_app(body: str) -> dict:
    """在加载了完整应用的子进程里执行 body，把它 print 的 JSON 取回来。"""
    script = (
        "import json\n"
        "import xgent_server as bot\n"
        f"{body}\n"
    )
    env = dict(os.environ)
    env.update({
        "BOT_TOKEN": "123456:test-token",
        "AUTHORIZED_USER_ID": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    with tempfile.TemporaryDirectory() as temp_dir:
        # trace 日志重定向到临时目录，避免污染项目根目录
        env["XGENT_TRACE_LOG_FILE"] = str(Path(temp_dir) / "trace.log")
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
    if proc.returncode != 0:
        raise AssertionError(f"子进程失败:\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class ThinkingParamsTests(unittest.TestCase):
    """覆盖 8 个档位 × 各家 api_format 的映射。"""

    @classmethod
    def setUpClass(cls):
        # 一次子进程跑完所有组合，避免每个用例都付一次应用加载开销。
        cls.result = run_in_app(
            "levels = list(bot.THINKING_LEVEL_ORDER)\n"
            "cases = [\n"
            "    ('openai', 'gpt-4o', 'https://api.openai.com/v1'),\n"
            "    ('claude', 'claude-sonnet-4-5', 'https://api.anthropic.com/v1'),\n"
            "    ('gemini', 'gemini-2.5-pro', 'https://generativelanguage.googleapis.com/v1beta'),\n"
            "    ('vertex', 'gemini-2.5-pro', 'https://aiplatform.googleapis.com/v1'),\n"
            "    ('openai_compatible', 'deepseek-r1', 'https://api.deepseek.com/v1'),\n"
            "    ('openai_compatible', 'anthropic/claude-sonnet-4.5', 'https://openrouter.ai/api/v1'),\n"
            "]\n"
            "out = {}\n"
            "for level in levels:\n"
            "    bot.UserDataManager.set('thinking_level', level)\n"
            "    for fmt, model, url in cases:\n"
            "        key = level + '|' + fmt + '|' + url\n"
            "        out[key] = bot.ModelClient._build_thinking_params(fmt, model, url, 'p')\n"
            "print(json.dumps(out))"
        )

    def get(self, level: str, fmt: str, url: str) -> dict:
        return self.result[f"{level}|{fmt}|{url}"]

    def test_auto_sends_nothing_for_every_format(self):
        """AUTO 是默认档位，必须一个字段都不发——否则不支持思考的老模型直接 400。"""
        for key, params in self.result.items():
            if key.startswith("auto|"):
                self.assertEqual({}, params, key)

    def test_claude_budget_and_max_tokens_ordering(self):
        """Anthropic 要求 max_tokens > budget_tokens，违反会直接 400。"""
        url = "https://api.anthropic.com/v1"
        for level in ("low", "medium", "high", "xhigh", "ultra", "max"):
            params = self.get(level, "claude", url)
            self.assertEqual("enabled", params["thinking"]["type"], level)
            budget = params["thinking"]["budget_tokens"]
            self.assertGreater(budget, 0, level)
            self.assertGreater(params["max_tokens"], budget, level)

    def test_claude_off_sends_nothing(self):
        """Anthropic 没有"显式关闭"，不发字段就是关闭。"""
        self.assertEqual({}, self.get("off", "claude", "https://api.anthropic.com/v1"))

    def test_gemini_off_uses_zero_budget(self):
        """Gemini 是唯一能显式关闭思考的格式。"""
        url = "https://generativelanguage.googleapis.com/v1beta"
        params = self.get("off", "gemini", url)
        self.assertEqual(0, params["generationConfig"]["thinkingConfig"]["thinkingBudget"])

    def test_gemini_max_uses_dynamic_budget(self):
        url = "https://generativelanguage.googleapis.com/v1beta"
        params = self.get("max", "gemini", url)
        self.assertEqual(-1, params["generationConfig"]["thinkingConfig"]["thinkingBudget"])

    def test_gemini_never_includes_thoughts(self):
        """思考内容不展示，includeThoughts 必须保持关闭。"""
        for key, params in self.result.items():
            config = params.get("generationConfig", {}).get("thinkingConfig", {})
            self.assertNotIn("includeThoughts", config, key)

    def test_vertex_matches_gemini_shape(self):
        params = self.get("high", "vertex", "https://aiplatform.googleapis.com/v1")
        self.assertIn("thinkingBudget", params["generationConfig"]["thinkingConfig"])

    def test_openai_uses_reasoning_effort(self):
        params = self.get("high", "openai", "https://api.openai.com/v1")
        self.assertEqual({"reasoning_effort": "high"}, params)

    def test_openai_off_sends_nothing(self):
        self.assertEqual({}, self.get("off", "openai", "https://api.openai.com/v1"))

    def test_openrouter_uses_reasoning_object(self):
        """OpenRouter 吃 reasoning.effort，不是 reasoning_effort。"""
        url = "https://openrouter.ai/api/v1"
        params = self.get("high", "openai_compatible", url)
        self.assertEqual({"reasoning": {"effort": "high"}}, params)

    def test_openrouter_clamps_effort_to_supported_values(self):
        """OpenRouter 只认 low/medium/high，xhigh/max 要折算回 high。"""
        url = "https://openrouter.ai/api/v1"
        for level in ("xhigh", "ultra", "max"):
            params = self.get(level, "openai_compatible", url)
            self.assertIn(params["reasoning"]["effort"], {"low", "medium", "high"}, level)

    def test_openrouter_off_disables_explicitly(self):
        url = "https://openrouter.ai/api/v1"
        self.assertEqual(
            {"reasoning": {"enabled": False}},
            self.get("off", "openai_compatible", url),
        )

    def test_plain_openai_compatible_uses_reasoning_effort(self):
        url = "https://api.deepseek.com/v1"
        params = self.get("medium", "openai_compatible", url)
        self.assertEqual({"reasoning_effort": "medium"}, params)

    def test_levels_increase_budget_monotonically(self):
        """档位越高预算越大，max 是动态档除外。"""
        url = "https://api.anthropic.com/v1"
        budgets = [
            self.get(level, "claude", url)["thinking"]["budget_tokens"]
            for level in ("low", "medium", "high", "xhigh", "ultra")
        ]
        self.assertEqual(budgets, sorted(budgets))
        self.assertEqual(len(budgets), len(set(budgets)))


class ThinkingHelpersTests(unittest.TestCase):
    """归一化、降级判定、参数合并/剥离。"""

    @classmethod
    def setUpClass(cls):
        cls.result = run_in_app(
            "out = {}\n"
            "out['normalize'] = {\n"
            "    'blank': bot.normalize_thinking_level(''),\n"
            "    'none': bot.normalize_thinking_level(None),\n"
            "    'garbage': bot.normalize_thinking_level('nonsense'),\n"
            "    'chinese_off': bot.normalize_thinking_level('关闭'),\n"
            "    'upper': bot.normalize_thinking_level('HIGH'),\n"
            "    'false': bot.normalize_thinking_level('false'),\n"
            "}\n"
            "out['rejection'] = {\n"
            "    'budget': bot.ModelClient._is_thinking_rejection('Unsupported parameter: budget_tokens'),\n"
            "    'effort': bot.ModelClient._is_thinking_rejection(\"unknown field 'reasoning_effort'\"),\n"
            "    'unrelated': bot.ModelClient._is_thinking_rejection('rate limit exceeded'),\n"
            "}\n"
            "body = {'generationConfig': {'temperature': 0.7}}\n"
            "thinking = {'generationConfig': {'thinkingConfig': {'thinkingBudget': 100}}}\n"
            "bot.ModelClient._merge_thinking_params(body, thinking)\n"
            # 深拷贝：下面的 _strip 会原地改 body，直接存引用会让两个快照指向同一份数据
            "import copy\n"
            "out['merged'] = copy.deepcopy(body)\n"
            "bot.ModelClient._strip_thinking_params(body, thinking, 'p', 'm')\n"
            "out['stripped'] = copy.deepcopy(body)\n"
            "out['remembered'] = bot.ModelClient._thinking_reject_key('p', 'm') in bot.ModelClient._thinking_unsupported\n"
            "bot.UserDataManager.set('thinking_level', 'high')\n"
            "out['after_reject'] = bot.ModelClient._build_thinking_params('openai', 'm', 'https://api.openai.com/v1', 'p')\n"
            "out['other_model'] = bot.ModelClient._build_thinking_params('openai', 'other', 'https://api.openai.com/v1', 'p')\n"
            "out['strip_noop'] = bot.ModelClient._strip_thinking_params({'a': 1}, {}, 'p', 'm2')\n"
            "print(json.dumps(out))"
        )

    def test_normalize_falls_back_to_auto(self):
        norm = self.result["normalize"]
        self.assertEqual("auto", norm["blank"])
        self.assertEqual("auto", norm["none"])
        self.assertEqual("auto", norm["garbage"])

    def test_normalize_accepts_aliases(self):
        norm = self.result["normalize"]
        self.assertEqual("off", norm["chinese_off"])
        self.assertEqual("off", norm["false"])
        self.assertEqual("high", norm["upper"])

    def test_rejection_detection(self):
        rejection = self.result["rejection"]
        self.assertTrue(rejection["budget"])
        self.assertTrue(rejection["effort"])
        self.assertFalse(rejection["unrelated"])

    def test_merge_preserves_sibling_keys(self):
        """深合并 generationConfig，不能把 temperature 覆盖掉。"""
        merged = self.result["merged"]
        self.assertEqual(0.7, merged["generationConfig"]["temperature"])
        self.assertEqual(100, merged["generationConfig"]["thinkingConfig"]["thinkingBudget"])

    def test_strip_removes_only_thinking_keys(self):
        stripped = self.result["stripped"]
        self.assertEqual(0.7, stripped["generationConfig"]["temperature"])
        self.assertNotIn("thinkingConfig", stripped["generationConfig"])

    def test_rejected_combo_is_remembered_and_skipped(self):
        """记住不支持的组合后不再重试，但只针对该 model。"""
        self.assertTrue(self.result["remembered"])
        self.assertEqual({}, self.result["after_reject"])
        self.assertEqual({"reasoning_effort": "high"}, self.result["other_model"])

    def test_strip_without_thinking_params_is_noop(self):
        """本来就没加思考参数时不该触发重试。"""
        self.assertFalse(self.result["strip_noop"])


class ThinkingContentLeakTests(unittest.TestCase):
    """思考内容绝不能混进正文——用户选择了"不显示"。"""

    @classmethod
    def setUpClass(cls):
        cls.result = run_in_app(
            "out = {}\n"
            "out['thought_part'] = bot.ModelClient._model_content_to_text({'text': 'secret', 'thought': True})\n"
            "out['normal_part'] = bot.ModelClient._model_content_to_text({'text': 'visible'})\n"
            "out['thought_false'] = bot.ModelClient._model_content_to_text({'text': 'visible', 'thought': False})\n"
            "mixed = {'candidates': [{'content': {'parts': [\n"
            "    {'text': 'thinking...', 'thought': True},\n"
            "    {'text': 'answer'},\n"
            "]}}]}\n"
            "out['gemini_response'] = bot.ModelClient._extract_gemini_text_response(mixed)\n"
            "claude_data = {'content': [\n"
            "    {'type': 'thinking', 'thinking': 'secret'},\n"
            "    {'type': 'redacted_thinking', 'data': 'blob'},\n"
            "    {'type': 'text', 'text': 'answer'},\n"
            "]}\n"
            "out['claude_response'] = bot.ModelClient._extract_claude_text_response(claude_data)\n"
            "sse = {'choices': [{'delta': {'reasoning_content': 'secret', 'content': 'answer'}}]}\n"
            "out['openai_delta'] = bot.ModelClient._extract_openai_compatible_text(sse)\n"
            "reasoning_only = {'choices': [{'delta': {'reasoning_content': 'secret'}}]}\n"
            "out['openai_reasoning_only'] = bot.ModelClient._extract_openai_compatible_text(reasoning_only)\n"
            "print(json.dumps(out))"
        )

    def test_gemini_thought_part_is_dropped(self):
        self.assertEqual("", self.result["thought_part"])
        self.assertEqual("visible", self.result["normal_part"])
        self.assertEqual("visible", self.result["thought_false"])

    def test_gemini_response_keeps_only_answer(self):
        self.assertEqual("answer", self.result["gemini_response"])

    def test_claude_response_skips_thinking_blocks(self):
        self.assertEqual("answer", self.result["claude_response"])

    def test_openai_compatible_ignores_reasoning_content(self):
        self.assertEqual("answer", self.result["openai_delta"])
        self.assertEqual("", self.result["openai_reasoning_only"])


if __name__ == "__main__":
    unittest.main()
