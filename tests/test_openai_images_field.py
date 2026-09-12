"""message.images 字段提取的回归测试。

画图网关（new-api 系、vertex 中转等）把生成图放在
choices[0].message.images[].image_url.url 里，正文 content 留空。
此前 _extract_openai_compatible_text 只读 content，会把包含整张图
base64 的响应当错误文本吐给用户，媒体生成必失败。

沿用 test_thinking_params.py 的子进程范式：sections 靠共享命名空间
加载，无法直接 import，只能在加载完整应用的子进程里断言。
"""

from __future__ import annotations

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


# 用户实测失败的那类响应：content=None，图片在顶层 images 数组里。
GATEWAY_PAYLOAD = {
    "id": "kiydar3hLe2oq8YPi5rE2Ak",
    "object": "chat.completion",
    "created": 1788685458,
    "model": "gemini-3.1-flash-lite-image",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I am reviewing the generated image.",
            "tool_calls": None,
            "images": [{
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,QUJDREVG"},
            }],
        },
        "finish_reason": "stop",
    }],
}


class MessageImagesExtractionTests(unittest.TestCase):
    """非流式 / SSE 提取函数必须把 message.images 里的 data URL 拼进文本。"""

    @classmethod
    def setUpClass(cls):
        cls.result = run_in_app(
            "import asyncio\n"
            "payload = json.loads(" + repr(json.dumps(GATEWAY_PAYLOAD)) + ")\n"
            "out = {}\n"
            # 1. 用户实测场景：content=None + images
            "out['plain'] = bot.ModelClient._extract_openai_compatible_text(payload)\n"
            # 2. SSE 形状的非流式响应（网关偶尔直接吐 data: 行）
            "sse_text = 'data: ' + json.dumps({'choices': [{'delta': {'content': None, "
            "'images': [{'image_url': {'url': 'data:image/png;base64,WFhY'}}]}}]}) + '\\n\\ndata: [DONE]\\n'\n"
            "out['sse'] = bot.ModelClient._extract_openai_compatible_sse_text(sse_text)\n"
            # 3. content 有文字时图片要追加在文字后面，而不是丢掉
            "mixed = {'choices': [{'message': {'content': '画好了', "
            "'images': [{'image_url': {'url': 'data:image/png;base64,ZWZl'}}]}}]}\n"
            "out['mixed'] = bot.ModelClient._extract_openai_compatible_text(mixed)\n"
            # 4. b64_json 裸数据（OpenAI Images API 惯例，无 mime 时按 png）
            "b64 = {'choices': [{'message': {'content': None, "
            "'images': [{'b64_json': 'QUJD', 'type': 'image_url'}]}}]}\n"
            "out['b64_json'] = bot.ModelClient._extract_openai_compatible_text(b64)\n"
            # 5. 网关同时把 data URL 塞进 content 和 images：不能重复两份
            "dup_url = 'data:image/png;base64,RERVUA=='\n"
            "dup = {'choices': [{'message': {'content': dup_url, "
            "'images': [{'image_url': {'url': dup_url}}]}}]}\n"
            "dup_text = bot.ModelClient._extract_openai_compatible_text(dup)\n"
            "out['dup_count'] = dup_text.count(dup_url)\n"
            # 6. 没有 images、content 也为空：行为保持原样（空串），不能崩
            "empty = {'choices': [{'message': {'content': None}}]}\n"
            "out['empty'] = bot.ModelClient._extract_openai_compatible_text(empty)\n"
            # 7. 跨 mime 的同图：content 里 png、images 里 jpeg，base64 一样。
            #    按 data URL 比较漏过去重，必须按 base64 内容比，只留一份。
            "cross_b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='\n"
            "cross = {'choices': [{'message': {'content': 'data:image/png;base64,' + cross_b64, "
            "'images': [{'image_url': {'url': 'data:image/jpeg;base64,' + cross_b64}}]}}]}\n"
            "cross_text = bot.ModelClient._extract_openai_compatible_text(cross)\n"
            "out['cross_data_url_count'] = cross_text.count('data:')\n"
            "print(json.dumps(out))"
        )

    def test_gateway_payload_extracts_data_url(self):
        """content=None 时必须从 images 里取出 data URL。"""
        self.assertIn("data:image/jpeg;base64,QUJDREVG", self.result["plain"])

    def test_gateway_payload_does_not_leak_reasoning(self):
        """reasoning_content 是思考字段，不能混进正文。"""
        self.assertNotIn("reasoning", self.result["plain"])
        self.assertNotIn("reviewing the generated image", self.result["plain"])

    def test_gateway_payload_not_raw_json(self):
        """不能再把整份 JSON 当错误文本返回。"""
        self.assertNotIn("chat.completion", self.result["plain"])

    def test_sse_delta_images_extracted(self):
        """SSE 流里的 delta.images 同样要提取。"""
        self.assertIn("data:image/png;base64,WFhY", self.result["sse"])

    def test_content_and_images_coexist(self):
        """有正文时正文在前、图片在后，两者都保留。"""
        mixed = self.result["mixed"]
        self.assertIn("画好了", mixed)
        self.assertIn("data:image/png;base64,ZWZl", mixed)
        self.assertLess(mixed.index("画好了"), mixed.index("data:image/png;base64,ZWZl"))

    def test_b64_json_defaults_to_png(self):
        """b64_json 无 mime 时按 OpenAI Images API 惯例视为 png。"""
        self.assertIn("data:image/png;base64,QUJD", self.result["b64_json"])

    def test_duplicate_url_not_duplicated(self):
        """同一张图不能存两份。"""
        self.assertEqual(1, self.result["dup_count"])

    def test_cross_mime_duplicate_deduped(self):
        """同一张图以不同 mime（png/jpeg）出现时也只留一份。"""
        self.assertEqual(1, self.result["cross_data_url_count"])

    def test_empty_response_stays_empty(self):
        """老行为不能被破坏：空响应返回空串。"""
        self.assertEqual("", self.result["empty"])


class SdkThinkAndReplyImagesTests(unittest.TestCase):
    """OpenAI SDK 路径（api_format='openai'）的 message.images 兜底。"""

    @classmethod
    def setUpClass(cls):
        cls.result = run_in_app(
            "import asyncio\n"
            "from unittest.mock import Mock\n"
            "\n"
            "class FakeMessage:\n"
            "    content = None\n"
            "    @staticmethod\n"
            "    def model_dump():\n"
            "        return {'role': 'assistant', 'content': None,\n"
            "                'images': [{'type': 'image_url',\n"
            "                            'image_url': {'url': 'data:image/png;base64,U0RL'}}]}\n"
            "\n"
            "class FakeCompletions:\n"
            "    async def create(self, **kwargs):\n"
            "        choice = Mock()\n"
            "        choice.message = FakeMessage()\n"
            "        choice.finish_reason = 'stop'\n"
            "        completion = Mock()\n"
            "        completion.choices = [choice]\n"
            "        completion.usage = None\n"
            "        return completion\n"
            "\n"
            "fake_client = Mock()\n"
            "fake_client.chat.completions = FakeCompletions()\n"
            "bot.PortalManager.get_portal = staticmethod(lambda *a, **k: fake_client)\n"
            "\n"
            "async def main():\n"
            "    return await bot.ModelClient.think_and_reply(\n"
            "        'p', 'key', 'https://example.invalid/v1', 'img-model', '',\n"
            "        [{'role': 'user', 'content': '画一只猫'}], api_format='openai')\n"
            "\n"
            "text, error = asyncio.run(main())\n"
            "print(json.dumps({'text': text, 'error': error}))"
        )

    def test_sdk_path_returns_data_url(self):
        """SDK 路径 content 为空时也要从 images 兜底出图。"""
        self.assertIsNone(self.result["error"])
        self.assertIn("data:image/png;base64,U0RL", self.result["text"] or "")


class MediaGenerationFlowTests(unittest.TestCase):
    """generate_media_with_provider 全链路：提取出的 data URL 要落成文件。"""

    @classmethod
    def setUpClass(cls):
        cls.result = run_in_app(
            "import asyncio\n"
            "import base64\n"
            "import os\n"
            "\n"
            "# 图片本体：1x1 png 的 base64\n"
            "png_b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='\n"
            "gateway_payload_text = 'data:image/png;base64,' + png_b64\n"
            "\n"
            "async def fake_think_and_reply(*a, **k):\n"
            "    return gateway_payload_text, None\n"
            "\n"
            "bot.ModelClient.think_and_reply = staticmethod(fake_think_and_reply)\n"
            "\n"
            "# 产物目录重定向到临时目录，别污染项目根\n"
            "import tempfile\n"
            "tmp = tempfile.mkdtemp(prefix='xgent_media_test_')\n"
            "bot.ArtifactManager.ROOT_DIR = tmp\n"
            "bot.ArtifactManager.GENERATED_MEDIA_DIR = os.path.join(tmp, 'generated_media')\n"
            "bot.ArtifactManager.UPLOAD_DIR = os.path.join(tmp, 'uploads')\n"
            "\n"
            "async def main():\n"
            "    return await bot.generate_media_with_provider(\n"
            "        'cpa-test', {'api_key': 'k', 'base_url': 'https://example.invalid/v1',\n"
            "                     'api_format': 'openai_compatible'},\n"
            "        'gemini-3.1-flash-image', '一只路由器猫娘', kind='图片')\n"
            "\n"
            "result = asyncio.run(main())\n"
            "artifact = (result.get('artifacts') or [{}])[0]\n"
            "saved_ok = False\n"
            "if artifact.get('path'):\n"
            "    with open(artifact['path'], 'rb') as f:\n"
            "        saved_ok = f.read() == base64.b64decode(png_b64)\n"
            "print(json.dumps({'success': result.get('success'),\n"
            "                  'error': result.get('error'),\n"
            "                  'mime': artifact.get('mime_type'),\n"
            "                  'saved_ok': saved_ok,\n"
            "                  'has_path': bool(artifact.get('path'))}))"
        )

    def test_media_generation_succeeds_from_images_field(self):
        """网关 images 字段的图要走完提取→落盘→success 全流程。"""
        self.assertTrue(self.result["success"], self.result.get("error"))
        self.assertTrue(self.result["has_path"])
        self.assertEqual("image/png", self.result["mime"])
        self.assertTrue(self.result["saved_ok"])


class InlineMediaSaveBoundaryDedupTests(unittest.TestCase):
    """落盘总关口 extract_inline_generated_media 必须按图像内容去重。

    网关（vertex906-gemini 画图系）会把同一张图在一条 response 里重复返回两份
    PNG：像素完全相同，但 C2PA 来源水印每次编码字节不同，base64/整字节/md5
    都合并不了。这里断言落盘这道关口按 IDAT 图像数据指纹去重，只存一份。
    """

    @classmethod
    def setUpClass(cls):
        cls.result = run_in_app(
            "import os, tempfile, io, base64\n"
            "from PIL import Image, PngImagePlugin\n"
            "import xgent_server as bot\n"
            "\n"
            # 造两张像素相同、C2PA 水印元数据字节不同的 PNG（复刻网关真实行为）\n"
            "def make_png(c2pa_meta):\n"
            "    out = io.BytesIO()\n"
            "    info = PngImagePlugin.PngInfo()\n"
            "    info.add_text('C2PA', c2pa_meta.decode())\n"
            "    Image.new('RGB', (2, 2), 'red').save(out, format='PNG', pnginfo=info)\n"
            "    return out.getvalue()\n"
            "\n"
            "a_b64 = base64.b64encode(make_png(b'C2PA-v1-ts-194816')).decode()\n"
            "b_b64 = base64.b64encode(make_png(b'C2PA-v2-ts-194817')).decode()\n"
            "# 两份字节不同、md5 不同，但 IDAT 图像数据一致\n"
            "text = 'data:image/png;base64,' + a_b64 + '\\n' + 'data:image/png;base64,' + b_b64\n"
            "\n"
            "tmp = tempfile.mkdtemp(prefix='xgent_boundary_')\n"
            "bot.ArtifactManager.ROOT_DIR = tmp\n"
            "bot.ArtifactManager.GENERATED_MEDIA_DIR = os.path.join(tmp, 'gm')\n"
            "bot.ArtifactManager.UPLOAD_DIR = os.path.join(tmp, 'up')\n"
            "\n"
            "processed, artifacts = bot.extract_inline_generated_media(text, append_notices=False)\n"
            "print(json.dumps({'artifact_count': len(artifacts),\n"
            "                  'data_url_left': 'data:' in processed}))\n"
        )

    def test_watermarked_duplicate_saves_once(self):
        """同图不同水印（字节不同、IDAT 相同）落盘只存一份文件。"""
        self.assertEqual(1, self.result["artifact_count"])

    def test_data_urls_stripped_from_text(self):
        """存盘后正文里不能再残留裸 data URL。"""
        self.assertFalse(self.result["data_url_left"])


class PerArtifactCaptionTests(unittest.TestCase):
    """多张图时每条消息 caption 只挂自己的存盘路径，不挂全部路径。"""

    @classmethod
    def setUpClass(cls):
        cls.result = run_in_app(
            "import os, tempfile, asyncio\n"
            "from unittest.mock import AsyncMock, patch\n"
            "import xgent_server as bot\n"
            "\n"
            "tmp = tempfile.mkdtemp(prefix='xgent_cap_')\n"
            "bot.ArtifactManager.ROOT_DIR = os.path.join(tmp, 'xs')\n"
            "bot.ArtifactManager.GENERATED_MEDIA_DIR = os.path.join(tmp, 'xs', 'gm')\n"
            "bot.ArtifactManager.UPLOAD_DIR = os.path.join(tmp, 'xs', 'up')\n"
            "\n"
            "# 两个真实落盘的 artifact（两张不同的图）\n"
            "png_b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='\n"
            "a1 = bot._save_inline_generated_media('image/png', png_b64)\n"
            "a2 = bot._save_inline_generated_media('image/png', png_b64)\n"
            "# 故意给两个不同路径\n"
            "artifacts = [a1, a2]\n"
            "\n"
            "captured = []\n"
            "async def fake_send_photo(chat_id, photo, caption=None, **k):\n"
            "    captured.append(caption)\n"
            "context = type('C', (), {'bot': type('B', (), {'send_photo': staticmethod(fake_send_photo)})()})()\n"
            "\n"
            "# caption 含正文 + 两个路径说明\n"
            "d1 = bot.to_display_path(a1['path'])\n"
            "d2 = bot.to_display_path(a2['path'])\n"
            "caption = ('这里是根据您的描述生成的图片：\\n\\n'\n"
            "           '【系统自动生成：本图片已自动存入 ' + d1 + '，需要时请read以返回上下文，无识图能力时请勿read以免报错】\\n'\n"
            "           '【系统自动生成：本图片已自动存入 ' + d2 + '，需要时请read以返回上下文，无识图能力时请勿read以免报错】')\n"
            "\n"
            "async def main():\n"
            "    await bot.send_generated_media_artifacts(context, 1, artifacts, caption=caption)\n"
            "    return captured\n"
            "caps = asyncio.run(main())\n"
            "print(json.dumps({'count': len(caps),\n"
            "                  'cap0_has_a1': d1 in (caps[0] or ''),\n"
            "                  'cap0_has_a2': d2 in (caps[0] or ''),\n"
            "                  'cap1_has_a1': d1 in (caps[1] or '') if len(caps) > 1 else None,\n"
            "                  'cap1_has_a2': d2 in (caps[1] or '') if len(caps) > 1 else None,\n"
            "                  'cap0_has_body': '这里是根据' in (caps[0] or ''),\n"
            "                  'cap1_has_body': '这里是根据' in (caps[1] or '') if len(caps) > 1 else None}))\n"
        )

    def test_each_caption_only_own_path(self):
        """每张图 caption 只含自己的路径，不挂另一张的路径。"""
        self.assertEqual(2, self.result["count"])
        self.assertTrue(self.result["cap0_has_a1"])   # 第一张带自己路径
        self.assertFalse(self.result["cap0_has_a2"])   # 第一张不带第二张路径
        self.assertFalse(self.result["cap1_has_a1"])  # 第二张不带第一张路径
        self.assertTrue(self.result["cap1_has_a2"])   # 第二张带自己路径

    def test_body_text_only_on_first(self):
        """正文只在第一张图上，后续图不重复刷正文。"""
        self.assertTrue(self.result["cap0_has_body"])
        self.assertFalse(self.result["cap1_has_body"])


if __name__ == "__main__":
    unittest.main()
