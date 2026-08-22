"""Telegram 入站文本提取：转义损坏修复的回归测试。

text_markdown/caption_markdown 会对**所有普通文本段**做 escape_markdown
（哪怕消息没有任何实体），用户直接打的 API Key / 带下划线的 URL 会被改写成
带 \_ 的版本喂给 AI 并入库。修复后：纯文本一律用原文字节，只有消息里
真有格式实体（加粗/代码块/链接等）才走 Markdown 重构。

sections 靠共享命名空间加载（需要环境变量、会写数据库），所以按仓库惯例
用子进程探针跑，不污染收集期。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
import json, sys
from datetime import datetime, timezone
sys.path.insert(0, %r)
from telegram import Chat, Message, MessageEntity
from xgent_app.bootstrap import load_sections
ns = {"__file__": "xgent_server.py"}
load_sections(ns)

def build(text, entities=None):
    return Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private"),
        text=text, entities=entities or [],
    )

BS = ns["BotState"]
extract = ns["_initial_message_text"]
has_worthy = ns["_has_markdown_worthy_entities"]
plain = "我的API key是 bvheiwv_vwrs_vbebvw. 请保存"

url_msg = build(
    "看下 https://example.com/a_b/c 我的key是 bvheiwv_vwrs_vbebvw",
    entities=[MessageEntity(type=MessageEntity.URL, offset=3, length=26)],
)
bold_msg = build("注意 这个很重要",
                 entities=[MessageEntity(type=MessageEntity.BOLD, offset=3, length=5)])
bold_key = build("sk_plain_value",
                 entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=13)])

print(json.dumps({
    "plain_exact": extract(build(plain), BS.IDLE) == plain,
    "url_entity_exact": extract(url_msg, BS.IDLE) == url_msg.text,
    "bold_markdown": extract(bold_msg, BS.IDLE) == "注意 *这个很重要*",
    "config_raw": all(extract(bold_key, s) == "sk_plain_value"
                      for s in (BS.SET_SEARCH_KEY, BS.SET_UPDATE_TOKEN, BS.SET_WEB_PASSWORD)),
    "worthy_none": not has_worthy(build("x")),
    "worthy_url_no": not has_worthy(url_msg),
    "worthy_code_yes": has_worthy(build("x", [MessageEntity(
        type=MessageEntity.CODE, offset=0, length=1)])),
    "caption_entity_yes": has_worthy(
        Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private"),
                text=None, caption="c",
                caption_entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=1)]),
        caption=True),
}))
""" % str(ROOT)


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
            self.fail(f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_plain_and_auto_entities_stay_byte_exact(self):
        result = self.run_probe()
        self.assertTrue(result["plain_exact"], "纯文本必须原文字节进上下文")
        self.assertTrue(result["url_entity_exact"],
                        "客户端自动加的 URL 实体不该触发整条消息转义")
        self.assertTrue(result["bold_markdown"], "真格式实体的 Markdown 要保留")
        self.assertTrue(result["config_raw"], "配置态一律原文")
        self.assertTrue(result["worthy_none"], "无实体的消息不该判为需要 Markdown")
        self.assertTrue(result["worthy_url_no"], "自动 URL 实体不算格式实体")
        self.assertTrue(result["worthy_code_yes"])
        self.assertTrue(result["caption_entity_yes"])


if __name__ == "__main__":
    unittest.main()
