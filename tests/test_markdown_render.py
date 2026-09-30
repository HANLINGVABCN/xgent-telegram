"""markdown_to_telegram_html 三端上游渲染器的行为回归测试。

锁死 2026-09-30 对 rendering.py 的一批 markdown 显示增强（A 组）：无前导竖线 GFM
表格识别与列对齐、嵌套/任务/有序列表、多级标题分档、水平线、含 title 链接与图片、
反斜杠转义与 XSS、双反引号行内代码与自动链接、多级引用折叠、代码块语言名
（c++/c#/带 title）。出口只允许 Telegram 白名单标签，故断言均落在
b/i/u/s/a/code/pre/blockquote 之内。

sections 靠共享命名空间加载（需环境变量、会写库、起常驻线程），按仓库惯例用带环境
变量的子进程探针跑，断言在父进程里做。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES = {
    "table_no_lead_pipe": "a | b\n--- | ---\n1 | 2",
    "table_lead_pipe": "| a | b |\n|---|---|\n| 1 | 2 |",
    "table_align": "| Name | Mid | Val |\n|:-----|:---:|----:|\n| ab | xy | 100 |\n| longer | z | 7 |",
    "table_cell_fmt": "| A | B | C |\n|---|---|---|\n| **x** | `y` | [z](u) |",
    "cpp_block": "```c++\nint x=0;\n```",
    "csharp_block": "```c#\nvar x=0;\n```",
    "title_block": "```py title=x.py\nprint(1)\n```",
    "headings": "# H1\n## H2\n### H3\n#### H4",
    "hr_dash": "above\n---\nbelow",
    "hr_star": "a\n***\nb",
    "task_list": "- [ ] todo\n- [x] done",
    "nested_list": "- a\n  - b\n    - c",
    "plus_bullet": "+ item",
    "ordered_list": "1. a\n2. b",
    "link_title": '[t](http://x.com "hi")',
    "link_paren_url": "[t](https://en.wikipedia.org/wiki/Foo_(bar))",
    "image": "![alt](http://img/1.png)",
    "backslash": r"a \* b \_ c \# d",
    "backslash_lt": r"a \< b",
    "injection": r"\<script\>",
    "double_backtick": "``a`b``",
    "autolink": "see <https://example.com/x> end",
    "nested_quote": "> real quote\n>> deep\n>nospace",
    "literal_entity_gt": "&gt; not a quote",
    "no_false_italic": "a_b_c and 3*4",
}

PROBE = r'''
import json, sys
sys.path.insert(0, %r)
from xgent_app.bootstrap import load_sections
ns = {"__file__": "xgent_server.py"}
load_sections(ns)
M = ns["markdown_to_telegram_html"]
cases = json.loads(sys.stdin.read())
print(json.dumps({k: M(v) for k, v in cases.items()}))
''' % str(ROOT)

class MarkdownRenderTests(unittest.TestCase):
    _cache = None

    def _render_all(self):
        if MarkdownRenderTests._cache is not None:
            return MarkdownRenderTests._cache
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        })
        with tempfile.TemporaryDirectory() as cwd:
            r = subprocess.run(
                [sys.executable, "-c", PROBE], cwd=cwd, env=env,
                input=json.dumps(CASES), text=True, encoding="utf-8",
                capture_output=True, timeout=120,
            )
        if r.returncode != 0:
            self.fail(f"probe failed\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        MarkdownRenderTests._cache = json.loads(r.stdout.strip().splitlines()[-1])
        return MarkdownRenderTests._cache

    def r(self, key):
        return self._render_all()[key]

    # A#1 无前导竖线 GFM 表格：识别为表格并进 <pre>，且与带前导竖线的等价写法结果一致
    def test_table_no_lead_pipe_recognized(self):
        out = self.r("table_no_lead_pipe")
        self.assertTrue(out.startswith("<pre>") and out.endswith("</pre>"), out)
        self.assertIn("a", out)
        self.assertIn("b", out)
        self.assertEqual(self.r("table_no_lead_pipe"), self.r("table_lead_pipe"))

    # A#9 列对齐：左（贴行首）/中（两侧留白）/右（前导留白）
    def test_table_column_alignment(self):
        out = self.r("table_align")
        lines = out.replace("<pre>", "").replace("</pre>", "").split("\n")
        self.assertTrue(any(l.startswith("longer") for l in lines), out)  # 左对齐
        self.assertIn(" z ", out)   # 居中
        self.assertIn("  7", out)   # 右对齐

    # A#9 单元格内行内格式剥成纯文字：**x**→x、`y`→y、[z](u)→z
    def test_table_cell_plain_text(self):
        out = self.r("table_cell_fmt")
        self.assertNotIn("**", out)
        self.assertNotIn("`", out)
        self.assertNotIn("[z]", out)
        self.assertIn("x", out)
        self.assertIn("z", out)

    # A#3 语言名含 +/# 不漏进正文、language class 正确
    def test_codeblock_language_cpp_csharp(self):
        cpp = self.r("cpp_block")
        self.assertIn('class="language-c++"', cpp)
        self.assertIn("int x=0;", cpp)
        self.assertNotIn("+int", cpp)
        self.assertIn('class="language-c#"', self.r("csharp_block"))

    # A#3 ```py title=x.py：language 取首 token=py，title 不泄漏进正文
    def test_codeblock_title_stripped(self):
        out = self.r("title_block")
        self.assertIn('class="language-py"', out)
        self.assertNotIn("title=", out)
        self.assertIn("print(1)", out)

    # A#4 六级标题分档：1/2/3/4 产出可区分的 b/u/i 组合
    def test_headings_tiered(self):
        out = self.r("headings")
        self.assertIn("<b><u>H1</u></b>", out)
        self.assertIn("<b>H2</b>", out)
        self.assertIn("<b><i>H3</i></b>", out)
        self.assertIn("<i>H4</i>", out)

    # A#5 水平线 --- / *** 渲染为可见分隔线
    def test_horizontal_rule(self):
        self.assertIn("─", self.r("hr_dash"))
        self.assertIn("above", self.r("hr_dash"))
        self.assertIn("─", self.r("hr_star"))

    # A#6 任务列表复选框：[ ]→☐、[x]→☑，不留裸方括号
    def test_task_list(self):
        out = self.r("task_list")
        self.assertIn("☐ todo", out)
        self.assertIn("☑ done", out)
        self.assertNotIn("[ ]", out)
        self.assertNotIn("[x]", out)

    # A#2 嵌套列表：保留层级缩进（nbsp）并按级换项目符号
    def test_nested_list(self):
        out = self.r("nested_list")
        self.assertIn("•", out)
        self.assertIn("◦", out)
        self.assertIn("▪", out)
        self.assertIn(" ", out)

    # A#2 `+` 也识别为无序列表标记
    def test_plus_bullet(self):
        out = self.r("plus_bullet")
        self.assertIn("•", out)
        self.assertNotIn("+ item", out)

    # 有序列表保留编号
    def test_ordered_list(self):
        out = self.r("ordered_list")
        self.assertIn("1. a", out)
        self.assertIn("2. b", out)

    # A#7 链接 title 从 href 剥离
    def test_link_title_stripped(self):
        out = self.r("link_title")
        self.assertIn('href="http://x.com"', out)
        self.assertNotIn("hi", out)

    # A#7 含括号 URL（Wikipedia 式）不被破坏
    def test_link_paren_url(self):
        self.assertIn('href="https://en.wikipedia.org/wiki/Foo_(bar)"', self.r("link_paren_url"))

    # A#7 图片降级为链接、去掉裸 `!`
    def test_image_no_bang(self):
        out = self.r("image")
        self.assertFalse(out.startswith("!"), out)
        self.assertIn("alt", out)
        self.assertIn('href="http://img/1.png"', out)

    # A#8 反斜杠转义：\* \_ \# 输出字面字符、不外泄反斜杠、不误触发格式
    def test_backslash_escape(self):
        out = self.r("backslash")
        self.assertIn("a * b", out)
        self.assertNotIn("\\", out)
        self.assertNotIn("<i>", out)
        self.assertNotIn("<b>", out)

    # A#8 \< 转义为安全实体
    def test_backslash_lt_safe(self):
        out = self.r("backslash_lt")
        self.assertIn("&lt;", out)
        self.assertNotIn("\\", out)

    # A#8 注入防护：\<script\> 全部转义、无裸标签
    def test_injection_escaped(self):
        out = self.r("injection")
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)

    # A#11 双反引号行内代码：内部单反引号得以保留
    def test_double_backtick_inline_code(self):
        out = self.r("double_backtick")
        self.assertTrue(out.startswith("<code>"), out)
        self.assertIn("a`b", out)

    # A#11 <url> 自动链接：转为 <a> 而非被转义成字面
    def test_autolink(self):
        self.assertIn('href="https://example.com/x"', self.r("autolink"))

    # A#10 多级引用折叠为单层 blockquote、无空格/裸行也识别
    def test_nested_quote_folded(self):
        out = self.r("nested_quote")
        self.assertEqual(out.count("<blockquote"), 1)
        self.assertIn("real quote", out)
        self.assertIn("deep", out)
        self.assertIn("nospace", out)

    # A#10 字面实体 &gt; 不被误当引用
    def test_literal_entity_not_quote(self):
        out = self.r("literal_entity_gt")
        self.assertNotIn("<blockquote", out)

    # 回归：snake_case / 单个 * 不误触发斜体、粗体
    def test_no_false_emphasis(self):
        out = self.r("no_false_italic")
        self.assertIn("a_b_c", out)
        self.assertNotIn("<i>", out)
        self.assertNotIn("<b>", out)


if __name__ == "__main__":
    unittest.main()
