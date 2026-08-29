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

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple


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
        r"run-x|shell-x|stdin-x:[^\n]+|shellread-x:[^\n]+|shellkill-x:[^\n]+|"
        r"trigger-x(?::[^\n]+)?|sendfile-x|read-x:[^\n]+|read-x|edit-x(?::[^\n]+)?|grep-x|"
        r"search-x|fetch-x|"
        r"media-x|file-x(?::[^\n]+)?"
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

        if normalized_tag.startswith("shellread:"):
            return {
                "type": "shellread",
                "path": normalized_tag[10:].strip(),
                "body": raw_body.strip(),
                **common,
            }

        if normalized_tag.startswith("shellkill:"):
            return {
                "type": "shellkill",
                "path": normalized_tag[10:].strip(),
                "body": raw_body.strip(),
                **common,
            }

        if normalized_tag == "trigger" or normalized_tag.startswith("trigger:"):
            return {
                "type": "trigger",
                "path": normalized_tag[8:].strip() if normalized_tag.startswith("trigger:") else "",
                "body": raw_body,
                **common,
            }

        if normalized_tag.startswith("read:"):
            return {
                "type": "read",
                "path": normalized_tag[5:].strip(),
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
