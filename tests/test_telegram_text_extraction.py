"""Telegram 入站文本提取：转义损坏修复的回归测试（无转义重构版）。

PTB 的 text_markdown/caption_markdown 对所有普通文本段做 escape_markdown；
上一版按"有无格式实体"分流后，富粘贴（代码块复制按钮 -> bold/code 实体）
生成的混合消息里明文部分仍被转义。最终方案：彻底放弃格式重构，Telegram 原文（message.text）
一字不差直传，格式属性丢弃。

sections 靠共享命名空间加载（需要环境变量、会写数据库），按仓库惯例用
子进程探针跑。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
import json, sys
from datetime import datetime, timezone
sys.path.insert(0, {root!r})
from telegram import Chat, Message, MessageEntity
from xgent_app.bootstrap import load_sections
ns = {{"__file__": "xgent_server.py"}}
load_sections(ns)

def build(text, entities=None):
    return Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private"),
        text=text, entities=entities or [],
    )

BS = ns["BotState"]
extract = ns["_initial_message_text"]

plain = "我的API key是 sk_live_bvheiwv_vwrs_vbebvw_9917 请保存"

mixed_text = ("把这段原样重复 测试样本A1: sk_live_bvheiwv_vwrs_vbebvw_9917 "
              "星号 反引号 方括号 链接 https://example.com/a_b/c_key 结束标记Z9")
mixed = build(mixed_text, entities=[
    MessageEntity(type=MessageEntity.ITALIC,
                  offset=mixed_text.index("星号"), length=len("星号")),
    MessageEntity(type=MessageEntity.CODE,
                  offset=mixed_text.index("反引号"), length=len("反引号")),
    MessageEntity(type=MessageEntity.URL,
                  offset=mixed_text.index("https://"),
                  length=len("https://example.com/a_b/c_key")),
])
mixed_out = extract(mixed, BS.IDLE)

url_text = "看下 https://example.com/a_b/c 我的key是 sk_a_b_c"
url_msg = build(url_text, entities=[
    MessageEntity(type=MessageEntity.URL,
                  offset=url_text.index("https://"),
                  length=len("https://example.com/a_b/c")),
])

bold_msg = build("注意 这个很重要", entities=[
    MessageEntity(type=MessageEntity.BOLD, offset=3, length=5)])

bold_key = build("sk_plain_value", entities=[
    MessageEntity(type=MessageEntity.BOLD, offset=0, length=13)])

cjk_text = "前缀_abc_中间强调后缀_xyz_尾部"
cjk = build(cjk_text, entities=[
    MessageEntity(type=MessageEntity.BOLD,
                  offset=cjk_text.index("强调"), length=len("强调"))])

print(json.dumps({{
    "plain_exact": extract(build(plain), BS.IDLE) == plain,
    "mixed_no_backslash": chr(92) not in mixed_out,
    "mixed_is_raw": mixed_out == mixed_text,
    "mixed_key_intact": "sk_live_bvheiwv_vwrs_vbebvw_9917" in mixed_out,
    "url_entity_exact": extract(url_msg, BS.IDLE) == url_msg.text,
    "bold_raw": extract(bold_msg, BS.IDLE) == "注意 这个很重要",
    "config_raw": all(extract(bold_key, s) == "sk_plain_value"
                      for s in (BS.SET_SEARCH_KEY, BS.SET_UPDATE_TOKEN, BS.SET_WEB_PASSWORD)),
    "cjk_raw": extract(cjk, BS.IDLE) == cjk_text,
}}))
""".format(root=str(ROOT))


class TelegramTextExtractionTests(unittest.TestCase):
    def run_probe(self):
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        })
        with tempfile.TemporaryDirectory() as cwd:
            env["XGENT_TRACE_LOG_FILE"] = str(Path(cwd) / "trace.log")
            result = subprocess.run(
                [sys.executable, "-c", PROBE],
                cwd=cwd, env=env, text=True, encoding="utf-8",
                capture_output=True, timeout=120,
            )
        if result.returncode != 0:
            self.fail("probe failed stdout: " + result.stdout + " stderr: " + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_extraction_never_escapes_and_keeps_format_marks(self):
        result = self.run_probe()
        self.assertTrue(result["plain_exact"], "纯文本必须原文字节进上下文")
        self.assertTrue(result["mixed_no_backslash"],
                        "富粘贴的混合消息里不允许出现任何反斜杠转义")
        self.assertTrue(result["mixed_is_raw"], "富粘贴混合消息必须原文直传（格式属性丢弃）")
        self.assertTrue(result["mixed_key_intact"], "混合消息里的 API Key 必须原样")
        self.assertTrue(result["url_entity_exact"], "自动 URL 实体原样透传")
        self.assertTrue(result["bold_raw"], "加粗消息也只传字符，不加标记")
        self.assertTrue(result["config_raw"], "配置态一律原文")
        self.assertTrue(result["cjk_raw"], "中文消息原文字节直传")


if __name__ == "__main__":
    unittest.main()
