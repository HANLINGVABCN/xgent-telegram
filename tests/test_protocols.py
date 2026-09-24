import unittest

from xgent_app.protocols import ProtocolParser


NONCE_A = "0123456789AB"
NONCE_B = "FEDCBA987654"


def protocol_block(tag: str, body: str, nonce: str) -> str:
    return (
        f"```{tag}\n<<BEGIN_{nonce}\n"
        f"{body}\n"
        f"<<END_{nonce}\n"
        "```"
    )


class ProtocolParserTests(unittest.TestCase):
    def test_extracts_multiple_protocols_in_order(self):
        response = (
            "先说明\n"
            f"{protocol_block('run-x', 'echo ok', NONCE_A)}\n"
            "中间文字\n"
            f"{protocol_block('read-x', '/tmp/demo.txt:1-3', NONCE_B)}\n"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(["run", "read"], [block["type"] for block in blocks])
        self.assertEqual("echo ok", blocks[0]["body"])
        self.assertEqual("/tmp/demo.txt:1-3", blocks[1]["body"])

    def test_search_and_fetch_blocks_keep_multiline_body(self):
        # 反斜杠不能出现在 f-string 表达式里（Python 3.12 前）。
        search_body = "nginx 502\nmax: 3"
        response = (
            f"{protocol_block('search-x', search_body, NONCE_A)}\n"
            f"{protocol_block('fetch-x', 'https://x.example', NONCE_B)}\n"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(["search", "fetch"], [block["type"] for block in blocks])
        self.assertEqual(search_body, blocks[0]["body"])
        self.assertEqual("https://x.example", blocks[1]["body"])

    def test_nonce_accepts_arbitrary_characters(self):
        for label, nonce in [
            ("字母数字", "abc123XYZ"),
            ("中文", "随机标记甲乙丙"),
            ("emoji", "🎲🎯🎨🎪🎭🎬"),
            ("符号", "a!@#$%^&*()b"),
            ("点与括号", "a.b[c]d+e*f"),
            ("下划线连字符", "old_style-nonce"),
        ]:
            with self.subTest(label):
                blocks = ProtocolParser.extract_protocol_blocks(
                    protocol_block("run-x", "echo ok", nonce)
                )
                self.assertEqual(1, len(blocks), label)
                self.assertEqual("echo ok", blocks[0]["body"])

    def test_nonce_rejects_whitespace_and_backticks(self):
        # 空白会让结束标记无法独占一行精确比较；反引号与围栏语法冲突。
        for label, nonce in [
            ("含空格", "abc def"),
            ("含反引号", "abc```def"),
            ("全反引号", "``````"),
            ("含制表符", "abc\tdef"),
        ]:
            with self.subTest(label):
                self.assertEqual(
                    [],
                    ProtocolParser.extract_protocol_blocks(
                        protocol_block("run-x", "echo ok", nonce)
                    ),
                    label,
                )

    def test_mismatched_nonce_does_not_close_block(self):
        response = (
            "```run-x\n<<BEGIN_中文标记甲乙丙\n"
            "echo ok\n"
            "<<END_中文标记丁戊己\n"          # 不同标记，不能闭合
            "```"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_body_is_opaque_until_matching_end_sequence(self):
        response = protocol_block(
            "run-x",
            "python - <<'PY'\n"
            "```read-x\n<<BEGIN_111111111111\n"
            "/tmp/should-not-run\n"
            "<<END_111111111111\n"
            "```",
            NONCE_A,
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("run", blocks[0]["type"])
        self.assertIn("```read-x", blocks[0]["body"])
        self.assertIn("/tmp/should-not-run", blocks[0]["body"])

    def test_incomplete_outer_protocol_does_not_expose_nested_protocol(self):
        response = (
            f"```run-x\n<<BEGIN_{NONCE_A}\n"
            "outer body\n"
            f"{protocol_block('read-x', '/tmp/should-not-run', NONCE_B)}"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_duplicate_nonce_block_is_stripped_from_visible_text(self):
        """重复 nonce 的块不执行，但也不能把原始协议标记漏给用户看。"""
        response = (
            "前言\n"
            f"{protocol_block('run-x', 'echo one', NONCE_A)}\n"
            f"{protocol_block('run-x', 'echo two', NONCE_A)}\n"
            "结尾"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks), "重复 nonce 的块不应该被执行")
        self.assertEqual("echo one", blocks[0]["body"])

        stripped = ProtocolParser.strip_protocol_blocks(response)
        self.assertNotIn("<<BEGIN_", stripped, "重复块的原始标记泄漏给了用户")
        self.assertNotIn("echo two", stripped)
        self.assertIn("前言", stripped)
        self.assertIn("结尾", stripped)

    def test_has_unclosed_block_detects_missing_end_marker(self):
        """未闭合会让后续协议全部不执行，必须能被检测到并提示。"""
        response = (
            f"```run-x\n<<BEGIN_{NONCE_A}\n"
            "echo ok\n"
            "（这里少了结束标记）"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertTrue(ProtocolParser.has_unclosed_block(response))

    def test_has_unclosed_block_false_for_well_formed(self):
        response = (
            "文字\n"
            f"{protocol_block('run-x', 'echo ok', NONCE_A)}\n"
            f"{protocol_block('read-x', '/tmp/a', NONCE_B)}"
        )
        self.assertFalse(ProtocolParser.has_unclosed_block(response))

    def test_has_unclosed_block_false_for_plain_text(self):
        self.assertFalse(ProtocolParser.has_unclosed_block("普通回复，没有协议。"))

    def test_end_marker_without_closing_fence_remains_body(self):
        end_marker = f"<<END_{NONCE_A}"
        response = (
            f"```file-x:/tmp/demo.md\n<<BEGIN_{NONCE_A}\n"
            "before\n"
            f"{end_marker}\n"
            "not-a-closing-fence\n"
            "after\n"
            f"{end_marker}\n"
            "```\n"
        )
        block = ProtocolParser.extract_protocol_blocks(response)[0]
        self.assertEqual("file", block["type"])
        self.assertEqual("/tmp/demo.md", block["path"])
        self.assertIn(f"{end_marker}\nnot-a-closing-fence", block["body"])

    def test_all_protocol_tags_keep_existing_execution_contract(self):
        cases = [
            ("run-x", "echo ok", "run", "", "echo ok"),
            ("shell-x", "sleep 1", "shell", "", "sleep 1"),
            ("stdin-x:shell_1", "line: echo ok", "stdin", "shell_1", "line: echo ok"),
            ("shellkill-x:shell_1", "done", "shellkill", "shell_1", "done"),
            ("intel-x", "callers foo .", "intel", "", "callers foo ."),
            ("sendfile-x", "/tmp/demo.txt", "sendfile", "", "/tmp/demo.txt"),
            ("read-x", "/tmp/demo.txt:1-2", "read", "", "/tmp/demo.txt:1-2"),
            ("edit-x", "edit body", "edit", "", "edit body"),
            ("grep-x", "grep body", "grep", "", "grep body"),
            ("media-x", "draw image", "media", "", "draw image"),
            ("file-x:/tmp/demo.txt", "file body", "file", "/tmp/demo.txt", "file body"),
            ("file-x:base64:/tmp/demo.bin", "SGVsbG8=", "file_base64", "/tmp/demo.bin", "SGVsbG8="),
            ("over-x", "任务完成楼，主人~", "over", "", "任务完成楼，主人~"),
        ]
        for index, (tag, body, expected_type, expected_path, expected_body) in enumerate(cases):
            nonce = f"{index:012X}"
            response = protocol_block(tag, body, nonce)
            with self.subTest(tag=tag):
                block = ProtocolParser.extract_protocol_blocks(response)[0]
                self.assertEqual(expected_type, block["type"])
                self.assertEqual(expected_path, block["path"])
                self.assertEqual(expected_body, block["body"])

    def test_shellread_tag_no_longer_executes(self):
        # shellread-x 已并入 stdin-x（空 body / 只 wait 即纯读）；旧写法不得再执行。
        response = protocol_block("shellread-x:shell_1", "读取原因", NONCE_A)
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_read_x_fence_line_path_no_longer_executes(self):
        # read-x 路径统一写正文；围栏行写路径的旧写法不再被识别为协议。
        response = protocol_block("read-x:/tmp/demo.txt:1-2", "", NONCE_A)
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_read_x_body_path_still_executes(self):
        # 正文写路径是唯一保留的 read-x 写法，必须照常解析。
        block = ProtocolParser.extract_protocol_blocks(
            protocol_block("read-x", "/tmp/demo.txt:1-2", NONCE_A)
        )[0]
        self.assertEqual("read", block["type"])
        self.assertEqual("", block["path"])
        self.assertEqual("/tmp/demo.txt:1-2", block["body"])

    def test_begin_and_end_nonce_must_match(self):
        response = (
            f"```run-x\n<<BEGIN_{NONCE_A}\n"
            "echo unsafe\n"
            f"<<END_{NONCE_B}\n"
            "```"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_begin_without_matching_end_is_not_executable(self):
        response = f"before\n```run-x\n<<BEGIN_{NONCE_A}\necho ok"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_end_without_final_fence_is_not_executable(self):
        response = (
            f"before\n```run-x\n<<BEGIN_{NONCE_A}\n"
            f"echo ok\n<<END_{NONCE_A}"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_old_single_end_marker_header_is_not_executable(self):
        response = (
            f"```run-x <<<<END_{NONCE_A}\n"
            f"echo unsafe\n<<END_{NONCE_A}\n```"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_old_fenced_protocol_is_not_executable(self):
        response = "before\n```run-x\necho unsafe\n```\nafter"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_old_file_heredoc_marker_is_not_executable(self):
        response = "```file-x:/tmp/demo.txt <<EOF\ncontent\nEOF\n```"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_nonce_with_5_characters_is_rejected(self):
        response = protocol_block("run-x", "echo unsafe", "12345")
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_nonce_with_6_characters_is_accepted(self):
        response = protocol_block("run-x", "echo ok", "A23_-B")
        self.assertEqual("echo ok", ProtocolParser.extract_protocol_blocks(response)[0]["body"])

    def test_nonce_with_32_characters_is_accepted(self):
        nonce = "A" + "1" * 29 + "_-"
        self.assertEqual(32, len(nonce))
        response = protocol_block("run-x", "echo ok", nonce)
        self.assertEqual("echo ok", ProtocolParser.extract_protocol_blocks(response)[0]["body"])

    def test_nonce_with_64_characters_is_accepted(self):
        nonce = "A" + "1" * 61 + "_-"
        self.assertEqual(64, len(nonce))
        response = protocol_block("run-x", "echo ok", nonce)
        self.assertEqual("echo ok", ProtocolParser.extract_protocol_blocks(response)[0]["body"])

    def test_nonce_with_65_characters_is_rejected(self):
        nonce = "A" * 65
        response = protocol_block("run-x", "echo unsafe", nonce)
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_duplicate_nonce_is_rejected_after_first_block(self):
        response = (
            f"{protocol_block('run-x', 'first', NONCE_A)}\n"
            f"{protocol_block('run-x', 'second', NONCE_A)}"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("first", blocks[0]["body"])

    def test_duplicate_block_body_remains_opaque(self):
        duplicate_body = protocol_block("read-x", "/tmp/should-not-run", NONCE_B)
        response = (
            f"{protocol_block('run-x', 'first', NONCE_A)}\n"
            f"{protocol_block('run-x', duplicate_body, NONCE_A)}"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("first", blocks[0]["body"])

    def test_strip_protocols_preserves_user_facing_text(self):
        response = (
            "before\n"
            f"{protocol_block('run-x', 'echo ok', NONCE_A)}\n\n"
            "after"
        )
        self.assertEqual("before\n\nafter", ProtocolParser.strip_protocol_blocks(response))

    def test_strip_only_removes_complete_new_protocols(self):
        old_block = (
            f"```run-x <<<<END_{NONCE_B}\n"
            f"echo old\n<<END_{NONCE_B}\n```"
        )
        response = f"before\n{protocol_block('run-x', 'echo ok', NONCE_A)}\n{old_block}\nafter"
        self.assertEqual(f"before\n{old_block}\nafter", ProtocolParser.strip_protocol_blocks(response))

    def test_windows_line_endings_are_supported(self):
        response = (
            f"```run-x\n<<BEGIN_{NONCE_A}\r\n"
            "echo ok\r\n"
            f"<<END_{NONCE_A}\r\n"
            "```"
        )
        block = ProtocolParser.extract_protocol_blocks(response)[0]
        self.assertEqual("run", block["type"])
        self.assertEqual("echo ok", block["body"])


class FenceLineShapeTests(unittest.TestCase):
    """围栏行只写标签，成对标记各自顶格独占一行。

    这是从"标记挂在围栏行上"（```run-x <<AGENT_BEGIN_xxx）换过来的：那种写法
    让 info string 里混进 <<AGENT_BEGIN_...，任何 Markdown 渲染器都只能把它
    当成一个语言名不认识的代码块，而且 BEGIN 和 END 形状不对称，模型写结束
    标记时得凭记忆重写而不是照抄上一行。
    """

    NONCE = "read_fstab_a60c"

    def block(self, tag: str, body: str, nonce: str = "") -> str:
        nonce = nonce or self.NONCE
        return f"```{tag}\n<<BEGIN_{nonce}\n{body}\n<<END_{nonce}\n```"

    def test_old_marker_on_the_fence_line_is_not_executed(self):
        # 只解析新格式：老写法必须彻底不认，否则两套格式并存时模型会各写各的。
        old = (
            f"```run-x <<AGENT_BEGIN_{self.NONCE}\n"
            f"rm -rf /tmp/whatever\n"
            f"AGENT_END_{self.NONCE}\n```"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(old))
        self.assertFalse(ProtocolParser.has_unclosed_block(old))

    def test_missing_begin_line_is_not_a_protocol(self):
        # 光有围栏和标签不算协议——否则用户贴一段 ```run-x 代码就会被执行。
        self.assertEqual(
            [], ProtocolParser.extract_protocol_blocks("```run-x\necho ok\n```")
        )

    def test_begin_line_may_be_indented(self):
        # 模型偶尔跟着围栏缩进一格，为这个丢掉整次操作不划算。
        response = f"```run-x\n   <<BEGIN_{self.NONCE}\necho ok\n<<END_{self.NONCE}\n```"
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("echo ok", blocks[0]["body"])

    def test_tag_parameters_may_contain_spaces(self):
        # 路径带空格是真事；标签参数一路吃到行尾，不能按空白切。
        blocks = ProtocolParser.extract_protocol_blocks(
            self.block("edit-x:/app/my file.py", "-----OLD-----\na\n-----NEW-----\nb")
        )
        self.assertEqual(1, len(blocks))
        self.assertEqual("/app/my file.py", blocks[0]["path"])

    def test_body_containing_a_full_nested_protocol_stays_opaque(self):
        # file-x 写一份 markdown，正文里带着完整的另一个协议块：
        # 只能解析出外层那一个，内层绝不能被执行。
        inner = "```run-x\n<<BEGIN_inner_nonce_9f\nrm -rf /\n<<END_inner_nonce_9f\n```"
        blocks = ProtocolParser.extract_protocol_blocks(
            self.block("file-x:/tmp/doc.md", inner)
        )
        self.assertEqual(1, len(blocks))
        self.assertEqual("file", blocks[0]["type"])
        self.assertIn("rm -rf /", blocks[0]["body"])

    def test_fence_line_with_trailing_content_is_rejected(self):
        # 标签后面还有别的东西 -> 不是协议围栏行，别猜。
        response = f"```run-x 顺便说一句\n<<BEGIN_{self.NONCE}\necho ok\n<<END_{self.NONCE}\n```"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))


class RedactProtocolBlocksTests(unittest.TestCase):
    def test_single_block_folds_to_one_line(self):
        response = protocol_block("run-x", "df -h\ndu -sh /var", NONCE_A)
        out = ProtocolParser.redact_protocol_blocks(response)
        self.assertEqual(1, len(out.splitlines()))
        self.assertIn("🔧", out)
        self.assertIn("run", out)
        self.assertIn("2 行已折叠", out)
        self.assertNotIn("df -h", out)
        self.assertNotIn("<<BEGIN_", out)
        self.assertNotIn("```", out)

    def test_edit_block_placeholder_includes_path(self):
        response = protocol_block("edit-x:/etc/app/config.py", "old\nnew", NONCE_A)
        out = ProtocolParser.redact_protocol_blocks(response)
        self.assertIn("📝", out)
        self.assertIn("/etc/app/config.py", out)
        self.assertNotIn("old", out)

    def test_prose_preserved_blocks_each_one_line(self):
        response = (
            "先看磁盘：\n"
            f"{protocol_block('run-x', 'df -h', NONCE_A)}\n"
            "再读配置：\n"
            f"{protocol_block('read-x', '/tmp/a.txt', NONCE_B)}\n"
            "完成。"
        )
        out = ProtocolParser.redact_protocol_blocks(response)
        self.assertIn("先看磁盘：", out)
        self.assertIn("再读配置：", out)
        self.assertIn("完成。", out)
        self.assertNotIn("df -h", out)
        self.assertNotIn("/tmp/a.txt", out)
        self.assertEqual(2, out.count("行已折叠"))

    def test_plain_code_fence_untouched(self):
        response = "示例：\n```python\nprint('hi')\n```\n讲完了。"
        self.assertEqual(response, ProtocolParser.redact_protocol_blocks(response))

    def test_hide_unclosed_true_folds_in_progress_tail(self):
        response = (
            "开始执行：\n"
            f"```run-x\n<<BEGIN_{NONCE_A}\nfor i in range(100):\n    print(i)"
        )
        out = ProtocolParser.redact_protocol_blocks(response, hide_unclosed=True)
        self.assertIn("开始执行：", out)
        self.assertIn("生成中", out)
        self.assertIn("🔧", out)
        self.assertNotIn("range(100)", out)
        self.assertNotIn("<<BEGIN_", out)

    def test_hide_unclosed_false_keeps_truncated_tail_raw(self):
        # 防「全文折叠」回归：定稿遇未闭合/截断块，原样保留、不折叠。
        response = (
            "开始执行：\n"
            f"```run-x\n<<BEGIN_{NONCE_A}\nfor i in range(100):\n    print(i)"
        )
        out = ProtocolParser.redact_protocol_blocks(response, hide_unclosed=False)
        self.assertEqual(response, out)
        self.assertNotIn("生成中", out)
        self.assertNotIn("行已折叠", out)

    def test_hide_unclosed_false_folds_closed_but_keeps_truncated_tail(self):
        response = (
            f"{protocol_block('run-x', 'echo done', NONCE_A)}\n"
            "然后继续：\n"
            f"```edit-x:/a.py\n<<BEGIN_{NONCE_B}\nprint('unfinished')"
        )
        out = ProtocolParser.redact_protocol_blocks(response, hide_unclosed=False)
        self.assertIn("行已折叠", out)
        self.assertNotIn("echo done", out)
        self.assertIn("print('unfinished')", out)
        self.assertIn("<<BEGIN_", out)
        self.assertNotIn("生成中", out)

    def test_bare_fence_without_begin_not_folded(self):
        response = "```run-x\ndf -h\n```"
        self.assertEqual(response, ProtocolParser.redact_protocol_blocks(response, hide_unclosed=True))
        self.assertEqual(response, ProtocolParser.redact_protocol_blocks(response, hide_unclosed=False))

    def test_idempotent(self):
        response = (
            "文字\n"
            f"{protocol_block('run-x', 'echo ok', NONCE_A)}\n"
            f"{protocol_block('shell-x', 'ls -la', NONCE_B)}\n"
            "尾巴"
        )
        once = ProtocolParser.redact_protocol_blocks(response)
        self.assertEqual(once, ProtocolParser.redact_protocol_blocks(once))

    def test_empty_and_no_block_input_unchanged(self):
        self.assertEqual("", ProtocolParser.redact_protocol_blocks(""))
        plain = "就是一段普通文字\n没有任何协议块\n\n结束"
        self.assertEqual(plain, ProtocolParser.redact_protocol_blocks(plain))

    def test_consecutive_blanks_collapsed_after_fold(self):
        response = (
            "上文\n\n"
            f"{protocol_block('run-x', 'echo ok', NONCE_A)}\n\n\n"
            "下文"
        )
        out = ProtocolParser.redact_protocol_blocks(response)
        self.assertNotIn("\n\n\n", out)
        self.assertIn("上文", out)
        self.assertIn("下文", out)


if __name__ == "__main__":
    unittest.main()
