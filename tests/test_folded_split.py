"""折叠协议块 → Telegram 分段的回归测试。

复现并锁死 neko 事件（2026-09-28）的根因：``split_html_for_telegram`` 曾按「当前
累计 + 下一【行】」判断分段边界，折叠块的开头行很短、判断通过后整块又在 depth>0 里
被吸进来无从再切——于是两个各自不超限、合起来超限的折叠块被并进同一段，末段兜底按整段
硬切成残缺 <blockquote> HTML → Telegram 400 「can't parse entities」→ 退回纯文本，
满屏 ``<blockquote expandable data-raw=…`` 裸标签刷屏。

sections 靠共享命名空间加载（需环境变量、会写库、起常驻线程），按仓库惯例用带环境变量的
子进程探针跑。断言在父进程里做，失败信息更清楚。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import json, sys, html, asyncio
sys.path.insert(0, %r)
from xgent_app.bootstrap import load_sections
from xgent_app.protocols import ProtocolParser
from xgent_app.tg_html_adapter import install_telegram_html_adapter
from telegram.constants import ParseMode
ns = {"__file__": "xgent_server.py"}
load_sections(ns)
split = ns["split_html_for_telegram"]
tg_len = ns["_tg_len"]
prose = ns["markdown_to_telegram_html"]
safe_send = ns["safe_send_message"]
safe_edit = ns["safe_edit_text"]
sanitize = ns["_sanitize_telegram_html"]
plain = ns["plain_text_from_html"]
LIMIT = 4000
TG_HARD = 4096  # Telegram 单条消息硬上限

def block(tag, nlines, width):
    body = "\n".join("%%s-line-%%03d %%s" %% (tag, i, "x" * width) for i in range(nlines))
    nonce = "NONCE" + tag.upper().replace("-", "")
    return "```%%s\n<<BEGIN_%%s\n%%s\n<<END_%%s\n```" %% (tag, nonce, body, nonce)

def block_path(path, nlines, width):
    # 块头 path 从围栏信息串取（```file-x:PATH）；nonce 另起、保持短，故 path 可任意长。
    body = "\n".join("pline-%%03d %%s" %% (i, "x" * width) for i in range(nlines))
    n = "NONCEPATHBLOCK"
    return "```file-x:%%s\n<<BEGIN_%%s\n%%s\n<<END_%%s\n```" %% (path, n, body, n)

def render(resp):
    return ProtocolParser.render_folded_html(resp, prose_renderer=prose, raw_copy=True)

def probe_chunks(resp, limit=LIMIT):
    folded = render(resp)
    chunks = split(folded, limit)
    out = []
    for c in chunks:
        tg = ProtocolParser.to_telegram_html(c)
        out.append({
            "tg_len": tg_len(c),
            "open_bq": tg.count("<blockquote"),
            "close_bq": tg.count("</blockquote>"),
            "open_pre": tg.count("<pre"),
            "close_pre": tg.count("</pre>"),
        })
    return {"n": len(chunks), "total_tg": tg_len(folded), "chunks": out}


# —— 端到端发送路径：还原 finalize_html_response 的实际动作（chunks[0] 走 safe_edit_text，
#    chunks[1:] 走 safe_send_message），并装上出口适配器，断言【真正打到 Telegram 的文本】
#    完整。事故根因之一是 split 已在块边界切好、chunks[1:] 却被 safe_send_message 按【原始
#    长度】重切碎（data-raw 令原始长度远大于出口长度）。只测 split 的输出抓不到它。
class _FakeBot:
    def __init__(self):
        self.sent = []  # 经出口适配器转换后、真正发出的文本
    async def send_message(self, chat_id=None, text=None, parse_mode=None, **kw):
        self.sent.append(text)
        return object()
    async def edit_message_text(self, text=None, chat_id=None, message_id=None, parse_mode=None, **kw):
        self.sent.append(text)
        return object()

class _FakeMsg:
    def __init__(self, bot):
        self.bot = bot
    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        # 真实 Message.edit_text 会走 bot.edit_message_text（被适配器补丁拦截转换）
        return await self.bot.edit_message_text(text=text, chat_id=1, message_id=1, parse_mode=parse_mode)

class _FakeCtx:
    def __init__(self, bot):
        self.bot = bot

def probe_send_path(resp, limit=LIMIT):
    folded = render(resp)
    chunks = split(folded, limit)
    bot = _FakeBot()
    install_telegram_html_adapter(bot)
    ctx = _FakeCtx(bot)
    msg = _FakeMsg(bot)
    async def go():
        await safe_edit(msg, chunks[0], reply_markup=None, parse_mode=ParseMode.HTML)
        for c in chunks[1:]:
            await safe_send(ctx, 1, c, limit=limit, parse_mode=ParseMode.HTML)
    asyncio.run(go())
    out = []
    for t in bot.sent:
        out.append({
            "len": len(t),
            "open_bq": t.count("<blockquote"),
            "close_bq": t.count("</blockquote>"),
            "open_pre": t.count("<pre"),
            "close_pre": t.count("</pre>"),
            "has_data_raw": ("data-raw" in t),
            "leak_tag": ("<blockquote expandable data-raw" in t),
        })
    return {"n_sent": len(bot.sent), "sent": out}


# —— 兜底防线：即便上游真产生了半截标签，sanitize / 纯文本降级都不能再把裸标签放出去。
_BROKEN = '正文\n<blockquote expandable data-raw="```file-x&#10;&lt;&lt;BEGIN&#10;file-x000 yyy'

result = {
    # 主根因：file-x(~3000) + run-x(~1400) 同一条消息，合计 > 4000
    "two_blocks": probe_chunks("先看两个协议块：\n" + block("file-x", 60, 48) + "\n" + block("run-x", 22, 55) + "\n"),
    # 单块超长且转义膨胀（压缩代码首行）
    "minified": probe_chunks("```file-x\n<<BEGIN_NONCEMIN\n" + ("<" * 3000 + "div>x</div>") + "\nline2 short\n<<END_NONCEMIN\n```"),
    # 三块，每块 ~2000，合计 ~6000
    "three_blocks": probe_chunks(block("file-x", 40, 45) + "\n" + block("run-x", 40, 45) + "\n" + block("read-x", 40, 45) + "\n"),
    # 未超限：整条一段返回
    "fits_one": probe_chunks("简短说明\n" + block("run-x", 3, 20) + "\n"),
    # HIGH（neko 未覆盖，工作流对抗性探针挖出）：块头 path/命令无长度上限——正文预算只
    #   保住正文，超长 path 会把整块 tg 形态顶过 4000，重蹈「单块被硬切成残缺 <blockquote>」
    #   覆辙。path 中段省略后，任何单个折叠块必 < 4000 → 单段、配对、不泄漏。
    "long_path": probe_chunks("看长路径块：\n" + block_path("/srv/" + "deep/" * 1200 + "app/x.py", 20, 40) + "\n"),
    "long_path_full_body": probe_chunks(block_path("/srv/" + "seg/" * 260 + "very_long_component_name.py", 60, 48) + "\n"),
    # 直接断言块头被中段省略：超长 path 渲染后必含 … 且整块 tg < 4000
    "head_elided": (lambda f: {"has_ellipsis": ("…" in f), "tg_len": tg_len(f)})(
        render("看：\n" + block_path("/srv/" + "deep/" * 1200 + "app/x.py", 20, 40) + "\n")),
    # 端到端：超长 path 块 + 一个普通块合计超限，强制切成 ≥2 段并走 safe_send + 出口适配器
    "send_long_path": probe_send_path(
        "看长路径块：\n" + block_path("/srv/" + "deep/" * 1200 + "app/x.py", 40, 48) + "\n" + block("run-x", 22, 55) + "\n"),
    # 端到端发送路径（两块合计超限）：chunks[1:] 经 safe_send_message + 出口适配器后必须完整
    "send_two_blocks": probe_send_path("先看两个协议块：\n" + block("file-x", 60, 48) + "\n" + block("run-x", 22, 55) + "\n"),
    # 端到端（三块）
    "send_three_blocks": probe_send_path(block("file-x", 40, 45) + "\n" + block("run-x", 40, 45) + "\n" + block("read-x", 40, 45) + "\n"),
    # 兜底防线：半截标签喂给 sanitize / 纯文本降级
    "defense": {
        "sanitize_raw_bq": ("<blockquote" in sanitize(_BROKEN)),  # 期望 False：裸 '<' 已转义成 &lt;
        "plain_has_bq": ("blockquote" in plain(_BROKEN)),          # 期望 False：半截标签整段删掉
        "plain_has_data_raw": ("data-raw" in plain(_BROKEN)),      # 期望 False
    },
}
print(json.dumps(result))
''' % str(ROOT)


class FoldedSplitTests(unittest.TestCase):
    def _run(self):
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        })
        with tempfile.TemporaryDirectory() as cwd:
            env["XGENT_TRACE_LOG_FILE"] = str(Path(cwd) / "xgent_full_trace.log")
            r = subprocess.run([sys.executable, "-c", PROBE], cwd=cwd, env=env,
                               text=True, encoding="utf-8", capture_output=True, timeout=120)
        if r.returncode != 0:
            self.fail(f"probe failed\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        return json.loads(r.stdout.strip().splitlines()[-1])

    def _assert_all_chunks_intact(self, scenario, data):
        for i, c in enumerate(data["chunks"]):
            self.assertLessEqual(c["tg_len"], 4000, f"{scenario} chunk[{i}] 超过 4000: {c}")
            self.assertEqual(c["open_bq"], c["close_bq"], f"{scenario} chunk[{i}] blockquote 不配对（被硬切）: {c}")
            self.assertEqual(c["open_pre"], c["close_pre"], f"{scenario} chunk[{i}] pre 不配对（被硬切）: {c}")

    def _assert_send_path_intact(self, scenario, data):
        # 真正打到 Telegram 的每一条文本都必须：不超硬上限、blockquote 配对、无残留 data-raw、
        # 无裸 <blockquote expandable data-raw 前缀（后者就是事故里刷屏的形态）。
        self.assertGreaterEqual(data["n_sent"], 2, f"{scenario} 应至少发出 2 条（含首条 edit）")
        for i, t in enumerate(data["sent"]):
            self.assertLessEqual(t["len"], 4096, f"{scenario} sent[{i}] 超过 Telegram 硬上限 4096: {t}")
            self.assertEqual(t["open_bq"], t["close_bq"], f"{scenario} sent[{i}] blockquote 不配对（被 safe_send 重切碎）: {t}")
            self.assertEqual(t["open_pre"], t["close_pre"], f"{scenario} sent[{i}] pre 不配对: {t}")
            self.assertFalse(t["has_data_raw"], f"{scenario} sent[{i}] 泄漏 data-raw（残缺块出口适配器无法剥离）: {t}")
            self.assertFalse(t["leak_tag"], f"{scenario} sent[{i}] 泄漏裸 <blockquote expandable data-raw 标签: {t}")

    def test_no_shattered_html_across_scenarios(self):
        data = self._run()
        # 两块合计超限：必须切成 ≥2 段，且每段都是完整块
        self.assertGreater(data["two_blocks"]["total_tg"], 4000)
        self.assertGreaterEqual(data["two_blocks"]["n"], 2)
        self._assert_all_chunks_intact("two_blocks", data["two_blocks"])
        # 单块超长压缩行：protocols 层已截到安全长度 → 单段且不超限
        self.assertEqual(data["minified"]["n"], 1)
        self._assert_all_chunks_intact("minified", data["minified"])
        # 三块：每段都完整、都不超限
        self._assert_all_chunks_intact("three_blocks", data["three_blocks"])
        # 未超限：原样一段
        self.assertEqual(data["fits_one"]["n"], 1)
        self._assert_all_chunks_intact("fits_one", data["fits_one"])
        # HIGH：超长 path 单块——块头省略后必 < 4000 → 单段且完整（否则会重演硬切残块）
        self.assertEqual(data["long_path"]["n"], 1, f"超长 path 单块未被块头省略收进单段: {data['long_path']}")
        self._assert_all_chunks_intact("long_path", data["long_path"])
        self.assertEqual(data["long_path_full_body"]["n"], 1, data["long_path_full_body"])
        self._assert_all_chunks_intact("long_path_full_body", data["long_path_full_body"])
        # 块头确实被中段省略，且整块 tg < 4000
        self.assertTrue(data["head_elided"]["has_ellipsis"], f"超长 path 未中段省略: {data['head_elided']}")
        self.assertLessEqual(data["head_elided"]["tg_len"], 4000, data["head_elided"])
        # 端到端：超长 path 块 + 普通块合计超限，切成 ≥2 段经 safe_send 后仍完整
        self._assert_send_path_intact("send_long_path", data["send_long_path"])
        # 端到端发送路径：chunks[1:] 经 safe_send_message + 出口适配器后仍完整（锁死事故根因）
        self._assert_send_path_intact("send_two_blocks", data["send_two_blocks"])
        self._assert_send_path_intact("send_three_blocks", data["send_three_blocks"])
        # 兜底防线：半截标签不再能穿过 sanitize / 纯文本降级泄漏出去
        self.assertFalse(data["defense"]["sanitize_raw_bq"], f"sanitize 仍放出裸 <blockquote: {data['defense']}")
        self.assertFalse(data["defense"]["plain_has_bq"], f"纯文本降级仍泄漏 blockquote 标签: {data['defense']}")
        self.assertFalse(data["defense"]["plain_has_data_raw"], f"纯文本降级仍泄漏 data-raw: {data['defense']}")


if __name__ == "__main__":
    unittest.main()
