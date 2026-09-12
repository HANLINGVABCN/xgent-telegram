import unittest

from xgent_app.agent_context import (
    build_edit_context_message,
    build_file_context_message,
    build_media_context_message,
    build_read_attachment_context_message,
    build_read_ranged_context_message,
    build_sendfile_context_message,
    build_shell_context_message,
)


class AgentContextTests(unittest.TestCase):
    def test_file_and_sendfile_context_keep_notice_and_protocol_guidance(self):
        sendfile = build_sendfile_context_message("[sendfile结果] 已发送 a.txt")
        file_message = build_file_context_message("[file结果] 已写入 a.txt")

        self.assertEqual("user", sendfile["role"])
        self.assertIn("[sendfile结果] 已发送 a.txt", sendfile["content"])
        self.assertIn("不包含文件本体", sendfile["content"])
        self.assertIn("[file结果] 已写入 a.txt", file_message["content"])
        self.assertIn("使用 read", file_message["content"])

    def test_shell_context_changes_only_by_running_state(self):
        running = build_shell_context_message("[Agent shell 启动会话]", running=True)
        finished = build_shell_context_message("[Agent shell 关闭会话]", running=False)

        self.assertIn("仍在运行的 shell 会话", running["content"])
        self.assertIn("不要无意义轮询", running["content"])
        self.assertIn("会话已经结束", finished["content"])
        self.assertNotIn("不要无意义轮询", finished["content"])

    def test_read_context_preserves_multimodal_payload(self):
        ranged = build_read_ranged_context_message(
            "[read结果] 已读取 demo.py",
            "text/x-python",
            "demo.py",
            "     1\\tprint('ok')",
        )
        attachment = build_read_attachment_context_message(
            "[read结果] 已读取 image.png",
            "image/png",
            "image.png",
            "AQI=",
            attachment_type="image",
        )

        self.assertIsInstance(ranged["content"], str)
        self.assertIn("[文件内容开始]", ranged["content"])
        self.assertEqual("image", attachment["content"][1]["type"])
        self.assertEqual("AQI=", attachment["content"][1]["data"])
        self.assertNotIn("filename", attachment["content"][1])

    def test_media_context_leaves_all_images_to_durable_assembly(self):
        message = build_media_context_message(
            {
                "success": True, "text": "媒体已生成",
                "image_attachment_ids": ["first", "second"],
                "artifacts": [
                    {"path": "/unused/first.png", "size": 10 * 1024 * 1024},
                    {"path": "/unused/second.png", "size": 20 * 1024 * 1024},
                ],
            },
            "媒体结果",
        )
        self.assertIsInstance(message["content"], str)
        self.assertIn("媒体结果", message["content"])
        self.assertIn("全部图片已持久关联", message["content"])
        self.assertIn("Conversation attachments", message["content"])
        self.assertNotIn("媒体过大", message["content"])
        self.assertNotIn("data:", message["content"])

    def test_media_context_does_not_claim_unlinked_images_are_visible(self):
        message = build_media_context_message(
            {"success": True, "file_path": "/unused/generated.png", "mime_type": "image/png"},
            "媒体执行说明",
        )
        self.assertIn("没有已持久关联的图片", message["content"])
        self.assertNotIn("全部图片已持久关联", message["content"])

    def test_context_builders_are_side_effect_free(self):
        message = build_edit_context_message("notice")
        self.assertEqual("user", message["role"])
        self.assertTrue(message["content"].startswith("notice\n"))


if __name__ == "__main__":
    unittest.main()
