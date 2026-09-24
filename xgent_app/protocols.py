"""纯 Agent 协议解析器。

该模块只负责把模型输出转换为结构化协议块，或移除协议块；
不执行命令、不访问文件、不发送 Telegram 消息。

协议形状：围栏行只写标签，成对标记单独占正文的首尾两行——

    ```run-x
    <<BEGIN_deploy_check_7f3a
    df -h
    <<END_deploy_check_7f3a
    ```

正文在完整结束序列出现前始终按不透明文本处理，不解析内部 Markdown 或协议。

为什么标记不再挂在围栏行上（旧写法是 ```run-x <<AGENT_BEGIN_xxx）：
  - 围栏行只剩标签，info string 的第一个词就是 `run-x`，任何 Markdown
    渲染器都能把它当成一个正常代码块，不会把 `<<AGENT_BEGIN_...` 漏进正文；
  - BEGIN 和 END 现在形状完全对称、都顶格独占一行、只差一个词，模型写结束
    标记时做的是"照抄上一行、把 BEGIN 换成 END"——这是 transformer 最稳的
    那类操作，比在几百个 token 之后回忆一串随机字符可靠得多。
"""

import html
import re
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Tuple

from xgent_app.text_utils import head_lines


class ProtocolParser:
    """解析使用唯一成对标记的 Agent 协议。"""

    # 随机标记允许任意字符，但排除空白和反引号：
    # - 空白：结束标记要独占一行做精确比较，含空白会导致永不闭合
    # - 反引号：<<END_a```b 这类标记与围栏语法混淆，模型也难原样复现
    # 长度下限（6）刻意低于提示词要求的 10：这是有意留的安全冗余。
    # 提示词让模型用 10-32 位，解析器接受 6-64 位，下限 4 位容错带 + 上限放宽到 64——
    # 模型偶尔少给几位时，协议块照样能被识别执行；模型给到 64 位也照常匹配。
    # 不要"为了一致"把这里收紧到 10：那会让 6-9 位的块整个不被识别成协议，
    # 等于静默丢掉一次操作，用户和模型都收不到任何提示。
    _NONCE_PATTERN = r"[^\s`]{6,64}"

    # 围栏行：只有标签，标签后面除了空白什么都不能有。
    # 带参数的标签（edit-x:/a/b.py、stdin-x:会话ID）用 [^\n]+ 而不是 \S+：
    # 路径里可以有空格，尾部空白由调用方 .strip() 掉。
    _OPEN_RE = re.compile(
        r"^[^\n]*?```(?P<tag>"
        r"run-x|shell-x|stdin-x:[^\n]+|shellkill-x:[^\n]+|"
        r"sendfile-x|read-x|edit-x(?::[^\n]+)?|grep-x|"
        r"search-x|fetch-x|"
        r"media-x|file-x(?::[^\n]+)?|ask-x|intel-x|over-x"
        r")\s*$"
    )

    # 开始标记行。允许前后有空白：模型偶尔会跟着围栏缩进一格，为这个丢掉
    # 整次操作不划算。
    _BEGIN_RE = re.compile(r"^\s*<<BEGIN_(?P<nonce>" + _NONCE_PATTERN + r")\s*$")

    _END_PREFIX = "<<END_"

    # 智能匹配相似度下限：低于此值不执行。
    # 1.0 = 精确匹配；0.9-0.99 = 容错匹配，执行但在结果回灌时给 AI 提示；
    # < 0.9 = 视为不匹配，协议块不执行。
    _SMART_MATCH_THRESHOLD = 0.9

    # 折叠协议块时的一行占位图标/标签（按 _build_block 归一化后的 type 索引）。
    # 覆盖全部 tag；未知 type 回落到 ("🔧", type)，见 _placeholder_icon_label。
    _PLACEHOLDER_ICON_LABEL: Dict[str, Tuple[str, str]] = {
        "run": ("🔧", "run"),
        "shell": ("🔧", "shell"),
        "edit": ("📝", "edit"),
        "file": ("📄", "file"),
        "file_base64": ("📄", "file"),
        "read": ("📖", "read"),
        "grep": ("🔍", "grep"),
        "search": ("🔍", "search"),
        "fetch": ("🌐", "fetch"),
        "media": ("🖼️", "media"),
        "stdin": ("⌨️", "stdin"),
        "shellkill": ("⏹️", "shellkill"),
        "sendfile": ("📎", "sendfile"),
        "ask": ("❓", "ask"),
        "intel": ("🧠", "intel"),
        "over": ("✅", "over"),
    }

    @classmethod
    def _collect_marked_body(
        cls,
        lines: List[str],
        start: int,
        marker: str,
        smart_match: bool = False,
        smart_match_threshold: Optional[float] = None,
    ) -> Tuple[List[str], Optional[int], bool, float]:
        """收集正文直到完整的 "marker + ```" 结束序列。

        单独出现的三反引号、协议头或 marker 都属于正文。未找到完整结束
        序列时返回 ([], None, False, 0.0)，确保残缺协议不会进入执行流程。

        ``smart_match=True`` 时，如果精确匹配找不到，会扫描以 ``<<END_``
        开头的行，用 SequenceMatcher 计算 nonce 相似度——
        高于阈值时返回该行作为结束标记，并在返回值里标记智能匹配。
        ``smart_match_threshold`` 不为 None 时覆盖类默认阈值。
        """
        body: List[str] = []
        i = start
        best_match: Optional[Tuple[int, int, float]] = None
        begin_nonce = marker[len(cls._END_PREFIX):]
        threshold = (
            smart_match_threshold
            if smart_match_threshold is not None
            else cls._SMART_MATCH_THRESHOLD
        )
        while i < len(lines):
            if lines[i].strip() == marker:
                closer_i = i + 1
                if closer_i < len(lines) and lines[closer_i].strip() == "```":
                    return body, closer_i, False, 1.0
            # 智能匹配：记录最相似的 <<END_<nonce> 行
            if smart_match:
                stripped = lines[i].strip()
                if stripped.startswith(cls._END_PREFIX):
                    end_nonce = stripped[len(cls._END_PREFIX):]
                    if end_nonce and "`" not in end_nonce:
                        ratio = SequenceMatcher(None, begin_nonce, end_nonce).ratio()
                        if ratio >= threshold:
                            closer_i = i + 1
                            if closer_i < len(lines) and lines[closer_i].strip() == "```":
                                if best_match is None or ratio > best_match[2]:
                                    best_match = (i, closer_i, ratio)
            body.append(lines[i])
            i += 1
        if best_match is not None:
            end_i, closer_i, ratio = best_match
            body = lines[start:end_i]
            return body, closer_i, True, ratio
        return [], None, False, 0.0

    @classmethod
    def _match_open(cls, lines: List[str], index: int) -> Optional[Tuple[str, str, int]]:
        """在 ``index`` 处尝试匹配"围栏行 + 开始标记行"。

        命中返回 ``(标签, nonce, 正文起始行号)``，否则 None。围栏行和开始标记
        行之间允许夹空行——只有围栏而没有开始标记的块不是协议，交回主循环
        当普通文本继续扫描。
        """
        open_match = cls._OPEN_RE.match(lines[index].rstrip("\r"))
        if not open_match:
            return None
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor >= len(lines):
            return None
        begin_match = cls._BEGIN_RE.match(lines[cursor].rstrip("\r"))
        if not begin_match:
            return None
        return open_match.group("tag").strip(), begin_match.group("nonce"), cursor + 1

    @classmethod
    def _build_block(
        cls,
        tag: str,
        raw_body: str,
        block_start: int,
        block_end: int,
    ) -> Dict[str, Any]:
        """把外部 -x 标签映射为现有执行层使用的内部字段。"""
        normalized_tag = re.sub(r"-x(?=:|$)", "", tag, count=1)
        common = {"start_line": block_start, "end_line": block_end}

        if normalized_tag.startswith("file:base64:"):
            return {
                "type": "file_base64",
                "path": normalized_tag[len("file:base64:"):].strip(),
                "body": raw_body,
                **common,
            }

        if normalized_tag.startswith("file:"):
            return {
                "type": "file",
                "path": normalized_tag[5:].strip(),
                "body": raw_body,
                **common,
            }

        if normalized_tag.startswith("stdin:"):
            return {
                "type": "stdin",
                "path": normalized_tag[6:].strip(),
                "body": raw_body,
                **common,
            }

        if normalized_tag.startswith("shellkill:"):
            return {
                "type": "shellkill",
                "path": normalized_tag[10:].strip(),
                "body": raw_body.strip(),
                **common,
            }

        if normalized_tag in {"edit", "grep"} or normalized_tag.startswith("edit:"):
            if normalized_tag.startswith("edit:"):
                return {
                    "type": "edit",
                    "path": normalized_tag[5:].strip(),
                    "body": raw_body.strip("\n"),
                    **common,
                }
            return {
                "type": normalized_tag,
                "path": "",
                "body": raw_body.strip("\n"),
                **common,
            }

        return {
            "type": normalized_tag,
            "path": "",
            "body": raw_body.strip(),
            **common,
        }

    @classmethod
    def extract_protocol_blocks(
        cls,
        ai_response: str,
        smart_match_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """按出现顺序提取完整的 Agent 协议块。

        任何协议只有同时满足有效开始行、nonce 匹配的结束标记和收尾
        三反引号时，才会生成可执行协议块。旧格式不会被识别。
        ``smart_match_threshold`` 不为 None 时覆盖默认智能匹配阈值。
        """
        return [
            block for block in cls._scan_blocks(ai_response, smart_match_threshold)
            if block.get("executable")
        ]

    @classmethod
    def _scan_blocks(
        cls,
        ai_response: str,
        smart_match_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """扫描出所有闭合的协议块，含不可执行的重复 nonce 块。

        重复 nonce 的块不执行，但仍要标出它的行范围：否则
        strip_protocol_blocks 不会剥离它，原始协议标记会直接显示给用户。
        """
        blocks: List[Dict[str, Any]] = []
        lines = ai_response.split("\n")
        i = 0
        seen_nonces = set()

        while i < len(lines):
            opened = cls._match_open(lines, i)
            if opened is None:
                i += 1
                continue

            tag, nonce, body_start = opened
            end_marker = f"{cls._END_PREFIX}{nonce}"
            body_lines, end_i, smart_matched, similarity = cls._collect_marked_body(
                lines,
                body_start,
                end_marker,
                smart_match=True,
                smart_match_threshold=smart_match_threshold,
            )
            if end_i is None:
                # 有效开始行之后的内容都可能属于未闭合正文；停止继续扫描，
                # 避免把正文中的协议示例误当成新的可执行协议。
                # 注意这会丢弃后面所有内容，所以 has_unclosed_block 会把这个
                # 情况报出来，让调用方提示模型重发，而不是静默什么都不做。
                break

            block = cls._build_block(
                tag,
                "\n".join(body_lines),
                i,
                end_i,
            )
            # 重复 nonce 不执行，但标记出来以便从展示文本里剥离。
            block["executable"] = nonce not in seen_nonces
            block["smart_matched"] = smart_matched
            block["similarity"] = similarity
            seen_nonces.add(nonce)
            blocks.append(block)
            i = end_i + 1

        return blocks

    @classmethod
    def has_unclosed_block(cls, ai_response: str) -> bool:
        """是否存在「开始行有效但没有闭合」的协议块。

        这种情况下解析器会停止扫描，后面所有协议都不会执行。以前这是完全
        静默的：用户看不出发生了什么，模型也不知道自己的操作没被执行。
        """
        lines = ai_response.split("\n")
        i = 0
        while i < len(lines):
            opened = cls._match_open(lines, i)
            if opened is None:
                i += 1
                continue
            _tag, nonce, body_start = opened
            _body, end_i, _sm, _sim = cls._collect_marked_body(
                lines, body_start, f"{cls._END_PREFIX}{nonce}"
            )
            if end_i is None:
                return True
            i = end_i + 1
        return False

    @classmethod
    def strip_protocol_blocks(cls, ai_response: str) -> str:
        """从 AI 回复中剔除所有完整 Agent 协议块。"""
        blocks = cls._scan_blocks(ai_response)
        if not blocks:
            return ai_response

        lines = ai_response.split("\n")
        keep = [True] * len(lines)
        for block in blocks:
            start = block.get("start_line")
            end = block.get("end_line")
            if start is None or end is None:
                continue
            for index in range(start, min(end + 1, len(lines))):
                keep[index] = False

        result = [lines[index] for index in range(len(lines)) if keep[index]]
        cleaned: List[str] = []
        previous_blank = False
        for line in result:
            is_blank = line.strip() == ""
            if is_blank and previous_blank:
                continue
            cleaned.append(line)
            previous_blank = is_blank
        return "\n".join(cleaned)

    @classmethod
    def _placeholder_icon_label(cls, block_type: str) -> Tuple[str, str]:
        """按归一化 type 取占位图标与标签；未知 type 回落到 ("🔧", type)。"""
        return cls._PLACEHOLDER_ICON_LABEL.get(block_type, ("🔧", block_type))

    @classmethod
    def _format_block_placeholder(cls, block: Dict[str, Any]) -> str:
        """把一个闭合协议块渲染成一行占位：〔{icon} {label}[ path] · {N} 行已折叠〕。"""
        icon, label = cls._placeholder_icon_label(block.get("type", ""))
        path = (block.get("path") or "").strip()
        head = f"{label} {path}".strip() if path else label
        body = block.get("body") or ""
        line_count = 0 if not body else body.count("\n") + 1
        return f"〔{icon} {head} · {line_count} 行已折叠〕"

    @classmethod
    def redact_protocol_blocks(cls, ai_response: str, hide_unclosed: bool = False) -> str:
        """把 AI 回复里的 Agent 协议块折叠成一行占位——只改「显示」，不改执行/存储。

        与 strip_protocol_blocks 共用 _match_open/_collect_marked_body/_build_block
        的同一套文法，逐块处理：

        - 每个「闭合」协议块（围栏行 → 收尾```）折成一行 _format_block_placeholder。
        - hide_unclosed=True（仅流式中途用）：结尾若有「已写围栏+BEGIN、还没闭合」
          的真协议块，折成一行〔{icon} {label} · 生成中…〕并收尾（丢弃未闭合尾巴的
          原始内容，等定稿再原样显示）。裸围栏（无 BEGIN，_match_open 失败）不算
          协议块，原样保留。
        - hide_unclosed=False（定稿默认）：**不折叠未闭合尾巴**——截断/写错/缺 END
          的块原样完整显示，绝不折成「生成中…」、绝不把整段回复吞成一行。
        - 普通 ```lang 演示块（不含 -x）不被 _OPEN_RE 识别 → 原样保留。
        - 有折叠时收尾合并连续空行（照抄 strip_protocol_blocks）；无折叠 / 空输入
          原样返回。占位行不匹配围栏文法，故 redact(redact(x)) == redact(x)。
        """
        if not ai_response:
            return ai_response

        lines = ai_response.split("\n")
        n = len(lines)
        out: List[str] = []
        changed = False
        i = 0
        while i < n:
            opened = cls._match_open(lines, i)
            if opened is None:
                out.append(lines[i])
                i += 1
                continue
            tag, nonce, body_start = opened
            body_lines, end_i, _smart, _ratio = cls._collect_marked_body(
                lines, body_start, f"{cls._END_PREFIX}{nonce}", smart_match=True
            )
            if end_i is None:
                # 未闭合块：围栏 + BEGIN 都在，但一直没等到 END + 收尾```。
                if hide_unclosed:
                    stub_type = cls._build_block(tag, "", i, i).get("type", "")
                    icon, label = cls._placeholder_icon_label(stub_type)
                    out.append(f"〔{icon} {label} · 生成中…〕")
                    changed = True
                else:
                    # 定稿：未闭合尾巴原样完整显示，绝不折叠。
                    out.extend(lines[i:])
                break
            # 闭合块 → 一行占位。
            block = cls._build_block(tag, "\n".join(body_lines), i, end_i)
            out.append(cls._format_block_placeholder(block))
            changed = True
            i = end_i + 1

        if not changed:
            return ai_response

        # 合并折叠后产生的连续空行（与 strip_protocol_blocks 收尾一致）。
        cleaned: List[str] = []
        previous_blank = False
        for line in out:
            is_blank = line.strip() == ""
            if is_blank and previous_blank:
                continue
            cleaned.append(line)
            previous_blank = is_blank
        return "\n".join(cleaned)

    # ------------------------------------------------------------------
    # 真·可展开折叠：把回复切成 [散文|协议块] 序列，块渲染成可展开元素。
    # 与 strip/redact 共用 _match_open/_collect_marked_body/_build_block 同一文法。
    # scan_folded_segments 是纯结构解析（无渲染依赖），供 CLI TUI 直接建消息模型；
    # render_folded_html 在其上产出最终 Telegram-HTML（散文经注入的 prose_renderer，
    # 协议块→<blockquote expandable>），同一份 HTML 同时喂电报原生折叠与网页折叠。
    # ------------------------------------------------------------------

    @classmethod
    def scan_folded_segments(
        cls,
        ai_response: str,
        hide_unclosed: bool = False,
    ) -> List[Dict[str, Any]]:
        """把回复切成有序的 [散文段 | 协议块] 段序列（纯结构，不渲染）。

        返回每段是 dict：
          - 散文：{"kind": "prose", "text": str}
          - 闭合块：{"kind": "block", "type", "icon", "label", "path",
                     "body"(完整正文), "line_count", "closed": True, "generating": False}
          - 未闭合尾块（仅 hide_unclosed=True）：同上但 closed=False, generating=True,
                     body="" —— 流式中途「生成中…」占位，原始正文从不外泄。

        hide_unclosed=False（定稿）时，未闭合尾巴原样并入 prose（不折叠、防吞），
        与 redact_protocol_blocks 一致：命中首个未闭合块即停止扫描。
        """
        if not ai_response:
            return []
        lines = ai_response.split("\n")
        n = len(lines)
        segments: List[Dict[str, Any]] = []
        prose_buf: List[str] = []

        def flush_prose() -> None:
            if prose_buf:
                segments.append({"kind": "prose", "text": "\n".join(prose_buf)})
                prose_buf.clear()

        i = 0
        while i < n:
            opened = cls._match_open(lines, i)
            if opened is None:
                prose_buf.append(lines[i])
                i += 1
                continue
            tag, nonce, body_start = opened
            body_lines, end_i, _smart, _ratio = cls._collect_marked_body(
                lines, body_start, f"{cls._END_PREFIX}{nonce}", smart_match=True
            )
            if end_i is None:
                if hide_unclosed:
                    flush_prose()
                    stub_type = cls._build_block(tag, "", i, i).get("type", "")
                    icon, label = cls._placeholder_icon_label(stub_type)
                    segments.append({
                        "kind": "block", "type": stub_type, "icon": icon,
                        "label": label, "path": "", "body": "",
                        "line_count": 0, "closed": False, "generating": True,
                    })
                else:
                    prose_buf.extend(lines[i:])
                    flush_prose()
                break
            block = cls._build_block(tag, "\n".join(body_lines), i, end_i)
            flush_prose()
            btype = block.get("type", "")
            icon, label = cls._placeholder_icon_label(btype)
            body = block.get("body") or ""
            line_count = 0 if not body else body.count("\n") + 1
            segments.append({
                "kind": "block", "type": btype, "icon": icon, "label": label,
                "path": (block.get("path") or "").strip(), "body": body,
                "line_count": line_count, "closed": True, "generating": False,
            })
            i = end_i + 1
        flush_prose()
        return segments

    # 单块展开后最多显示的正文行数。① 展开 = 块头 + 前 N 行(+②)；第 N+1 行起
    # 永不渲染。正文 ≤ N 行时无 ②，展开即整块。spec 固定 50。
    _FOLD_MAX_LINES = 50

    @classmethod
    def _render_block_blockquote(
        cls,
        seg: Dict[str, Any],
        max_lines: Optional[int] = None,
    ) -> str:
        """把一个协议块段渲染成 Telegram-HTML 的 <blockquote expandable>。

        闭合块 → <blockquote expandable>：首行块头 "{icon} {label}[ path] · {N} 行"
        （N=正文总行数，收起态电报原生预览即这一行），其后 <pre>{escape(前 max_lines 行)}</pre>；
        正文超过 max_lines 时，末尾追加纯文字标签「已折叠 K 行」(K=总行数−max_lines)，
        标注第 max_lines+1 行起被折且永不渲染。正文 ≤ max_lines：无标签，展开即整块。
        未闭合块（generating）→ 不带 expandable 的「⚡ … · 生成中…」，正文为空，
        绝不外泄在写的原始内容。**只改显示**，落库/执行/镜像永远用完整原文。
        """
        limit = cls._FOLD_MAX_LINES if max_lines is None else max_lines
        icon = seg.get("icon") or "🔧"
        label = seg.get("label") or seg.get("type") or ""
        path = (seg.get("path") or "").strip()
        head = f"{icon} {label} {path}".strip() if path else f"{icon} {label}".strip()

        if seg.get("generating"):
            return f"<blockquote>⚡ {html.escape(head)} · 生成中…</blockquote>"

        body = seg.get("body") or ""
        line_count = seg.get("line_count", 0)
        header_line = html.escape(f"{head} · {line_count} 行")
        preview, dropped = head_lines(body, limit)
        escaped_body = html.escape(preview, quote=False)
        inner = f"{header_line}\n<pre>{escaped_body}</pre>"
        if dropped > 0:
            inner += f"\n{html.escape(f'已折叠 {dropped} 行')}"
        return f"<blockquote expandable>{inner}</blockquote>"

    @classmethod
    def render_folded_html(
        cls,
        ai_response: str,
        hide_unclosed: bool = False,
        prose_renderer: Optional[Callable[[str], str]] = None,
        max_lines: Optional[int] = None,
    ) -> str:
        """把回复渲染成最终 Telegram-HTML：协议块折成 <blockquote expandable>。

        散文段经 ``prose_renderer``（调用方注入 markdown_to_telegram_html，保持与全局
        一致的渲染；协议解析器是独立模块、拿不到 section 命名空间里的渲染器，故用注入）；
        prose_renderer=None 时退化为 html.escape（供无 app 依赖的单元测试）。

        无任何协议块时原样返回输入（上层再按需过 markdown→HTML）。产出的
        <blockquote>/<pre> 不匹配围栏文法且正文已转义，故 f(f(x)) == f(x)。
        **只改显示**：不触碰 DB/执行/镜像的原始文本。
        """
        if not ai_response:
            return ai_response
        segments = cls.scan_folded_segments(ai_response, hide_unclosed=hide_unclosed)
        if not any(s.get("kind") == "block" for s in segments):
            return ai_response
        parts: List[str] = []
        for seg in segments:
            if seg.get("kind") == "prose":
                text = seg.get("text") or ""
                if not text.strip():
                    continue
                if prose_renderer is not None:
                    rendered = prose_renderer(text)
                else:
                    rendered = html.escape(text, quote=False)
                if rendered and rendered.strip():
                    parts.append(rendered)
            else:
                parts.append(cls._render_block_blockquote(seg, max_lines))
        return "\n".join(p for p in parts if p)

