from __future__ import annotations

import os
import unittest

from xgent_app.agent_ask import (
    PendingAsk,
    PendingAskStore,
    SecretStore,
    parse_ask_form,
)
from xgent_app.shell_output import build_run_notice, set_context_redactor


SAMPLE = """Q1|multi|数据库迁移方式？
- 直接覆盖
- 备份后迁移
- 终止
+other
Q2|single|部署到哪个环境？
- 生产
- 测试
Q3|text|还有其他想法？
Q4|secret:DB_ROOT_PASS|请输入数据库密码"""


class ParseAskFormTests(unittest.TestCase):
    def test_parses_all_four_modes(self):
        form, err = parse_ask_form(SAMPLE)
        self.assertIsNone(err)
        kinds = [(q.qtype, q.other, q.var, len(q.options)) for q in form.questions]
        self.assertEqual(kinds, [
            ("multi", True, "", 3),
            ("single", False, "", 2),
            ("text", False, "", 0),
            ("secret", False, "DB_ROOT_PASS", 0),
        ])
        # qid 用位置下标，稳定且短（进 callback_data）
        self.assertEqual([q.qid for q in form.questions], ["0", "1", "2", "3"])

    def test_chinese_type_keywords_accepted(self):
        form, err = parse_ask_form("Q1|多选|随便\n- A\n- B\nQ2|密钥:API_TOKEN|填")
        self.assertIsNone(err)
        self.assertEqual(form.questions[0].qtype, "multi")
        self.assertEqual(form.questions[1].qtype, "secret")
        self.assertEqual(form.questions[1].var, "API_TOKEN")

    def test_rejects_empty_body(self):
        _f, err = parse_ask_form("   ")
        self.assertIsNotNone(err)

    def test_rejects_choice_without_options(self):
        _f, err = parse_ask_form("Q1|single|没有选项")
        self.assertIsNotNone(err)
        self.assertIn("选项", err)

    def test_rejects_bad_secret_var_name(self):
        for bad in ("bad-name", "1STARTS_DIGIT", "has space"):
            _f, err = parse_ask_form(f"Q1|secret:{bad}|x")
            self.assertIsNotNone(err, bad)

    def test_rejects_unknown_type(self):
        _f, err = parse_ask_form("Q1|dropdown|x\n- A")
        self.assertIsNotNone(err)

    def test_stray_line_before_any_question_is_error_not_silent(self):
        _f, err = parse_ask_form("- 孤立选项\nQ1|single|x\n- A")
        self.assertIsNotNone(err)


class KeyboardAndAnswerTests(unittest.TestCase):
    def setUp(self):
        self.form, _ = parse_ask_form(SAMPLE)

    def test_keyboard_reflects_draft_marks(self):
        draft = {"0": {"selected": [0, 2]}, "3": {"filled": True}}
        kb = self.form.build_keyboard("abc123", draft)
        flat = [btn.text for row in kb.inline_keyboard for btn in row]
        # 多选选中项带 ✅，未选带 ▫️
        self.assertTrue(any("✅" in t and "直接覆盖" in t for t in flat))
        self.assertTrue(any("▫️" in t and "备份后迁移" in t for t in flat))
        # 密钥已录入
        self.assertTrue(any("已录入 $DB_ROOT_PASS" in t for t in flat))
        # 末行有提交/取消
        self.assertTrue(any("提交" in t for t in flat))
        self.assertTrue(any("取消" in t for t in flat))

    def test_single_select_callback_shape(self):
        kb = self.form.build_keyboard("aid", {})
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("asks:aid:1:0", cbs)   # Q2 单选第 0 项
        self.assertIn("askm:aid:0:0", cbs)   # Q1 多选第 0 项
        self.assertIn("asko:aid:2", cbs)     # Q3 text 录入
        self.assertIn("askk:aid:3", cbs)     # Q4 secret 录入
        self.assertIn("askd:aid", cbs)       # 提交
        self.assertIn("askx:aid", cbs)       # 取消

    def test_assemble_answer_secret_only_var_name_never_plaintext(self):
        draft = {
            "0": {"selected": [0, 2]},
            "1": {"selected": [1]},
            "2": {"custom": "尽快上线"},
            "3": {"filled": True},
        }
        ans = self.form.assemble_answer(draft)
        self.assertIn("$DB_ROOT_PASS", ans)
        self.assertIn("已就绪", ans)
        self.assertIn("直接覆盖、终止", ans)
        self.assertIn("尽快上线", ans)
        # 明文绝不出现（draft 里本来也没有明文，这是双保险）
        self.assertNotIn("filled", ans)

    def test_unanswered_listing(self):
        draft = {"0": {"selected": [0]}}
        unanswered = [q.qid for q in self.form.unanswered(draft)]
        self.assertEqual(unanswered, ["1", "2", "3"])


class SecretStoreTests(unittest.TestCase):
    def test_set_writes_env_and_calls_register_hook(self):
        reg = []
        store = SecretStore()
        store.register_hook = reg.append
        store.set(7, "TEST_ASK_SECRET_VAR", "sup3r,s3cret")  # 含逗号：整串登记
        self.addCleanup(lambda: os.environ.pop("TEST_ASK_SECRET_VAR", None))
        self.assertEqual(os.environ.get("TEST_ASK_SECRET_VAR"), "sup3r,s3cret")
        self.assertEqual(reg, ["sup3r,s3cret"])
        self.assertTrue(store.has(7, "TEST_ASK_SECRET_VAR"))

    def test_purge_removes_env_and_calls_unregister_hook(self):
        unreg = []
        store = SecretStore()
        store.unregister_hook = unreg.append
        store.set(7, "TEST_ASK_PURGE_VAR", "value123")
        removed = store.purge(7)
        self.assertEqual(removed, ["TEST_ASK_PURGE_VAR"])
        self.assertIsNone(os.environ.get("TEST_ASK_PURGE_VAR"))
        self.assertEqual(unreg, ["value123"])

    def test_purge_leaves_env_var_overwritten_elsewhere(self):
        store = SecretStore()
        store.set(7, "TEST_ASK_OVERWRITE_VAR", "ours")
        os.environ["TEST_ASK_OVERWRITE_VAR"] = "someone-else"  # 别处覆盖
        self.addCleanup(lambda: os.environ.pop("TEST_ASK_OVERWRITE_VAR", None))
        store.purge(7)
        # 不误删别人覆盖的同名变量
        self.assertEqual(os.environ.get("TEST_ASK_OVERWRITE_VAR"), "someone-else")


class RedactionWiringTests(unittest.TestCase):
    """build_run_notice 必须经过脱敏钩子——echo $VAR 的明文不能回灌给模型。"""

    def tearDown(self):
        set_context_redactor(lambda t: t)  # 复位，避免影响其它测试

    def test_run_notice_applies_redactor(self):
        set_context_redactor(lambda t: t.replace("leaked_secret_xyz", "[REDACTED]"))
        notice = build_run_notice({
            "command": "echo $X", "success": True, "return_code": 0,
            "timed_out": False, "stopped": False, "elapsed_seconds": 0.0,
            "output_path": "/tmp/x", "output_bytes": 3,
            "output": "leaked_secret_xyz",
        })
        self.assertNotIn("leaked_secret_xyz", notice)
        self.assertIn("[REDACTED]", notice)


class PendingAskStoreTests(unittest.TestCase):
    def _make(self, ask_id="a1", generation=1):
        form, _ = parse_ask_form(SAMPLE)
        return PendingAsk(
            ask_id=ask_id, form=form, generation=generation,
            turn_history_snapshot=[], agent_iteration=1, chat_id=42,
        )

    def test_pop_is_atomic_prevents_double_submit(self):
        store = PendingAskStore()
        store.put(self._make("a1"))
        self.assertIsNotNone(store.pop("a1"))
        self.assertIsNone(store.pop("a1"))  # 第二次拿不到 → 防重复提交

    def test_purge_generation_only_targets_that_generation(self):
        store = PendingAskStore()
        store.put(self._make("old", generation=1))
        store.put(self._make("new", generation=2))
        removed = store.purge_generation(1)
        self.assertEqual(removed, 1)
        self.assertIsNone(store.get("old"))
        self.assertIsNotNone(store.get("new"))

    def test_snapshot_preserves_multimodal_parts(self):
        # readx 的多模态 part（图片/二进制）必须在快照里原样保留，不退化成文本。
        multimodal = [
            {"role": "user", "content": "看这张图"},
            {"role": "user", "content": [
                {"type": "text", "text": "图见下"},
                {"type": "image", "source": {"data": "BASE64DATA", "media_type": "image/png"}},
            ]},
            {"role": "assistant", "content": "```ask-x\n<<BEGIN_x\nQ1|text|?\n<<END_x\n```"},
        ]
        pending = PendingAsk(
            ask_id="a1", form=self._make().form, generation=1,
            turn_history_snapshot=list(multimodal), agent_iteration=2, chat_id=42,
        )
        snap = pending.turn_history_snapshot
        self.assertEqual(snap[1]["content"][1]["type"], "image")
        self.assertEqual(snap[1]["content"][1]["source"]["data"], "BASE64DATA")


if __name__ == "__main__":
    unittest.main()
