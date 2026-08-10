"""纯 Agent 协议解析器。

该模块只负责把模型输出转换为结构化协议块，或移除协议块；
不执行命令、不访问文件、不发送 Telegram 消息。

协议使用 nonce 相同的 AGENT_BEGIN/AGENT_END 成对标记。正文在完整结束
序列出现前始终按不透明文本处理，不解析内部 Markdown 或协议。
"""

import re
from typing import Any, Dict, List


class ProtocolParser:
    """解析使用唯一成对标记的 Agent 协议。"""

    # 随机标记允许任意字符，但排除空白和反引号：
    # - 空白：结束标记要独占一行做精确比较，含空白会导致永不闭合
    # - 反引号：AGENT_END_a```b 这类标记与围栏语法混淆，模型也难原样复现
    # 长度下限（6）刻意低于提示词要求的 10：这是有意留的安全冗余。
    # 提示词让模型用 10-32 位，解析器只认到 6 位，中间 4 位是容错带——
    # 模型偶尔少给几位时，协议块照样能被识别执行。
    # 不要"为了一致"把这里收紧到 10：那会让 6-9 位的块整个不被识别成协议，
    # 等于静默丢掉一次操作，用户和模型都收不到任何提示。
    _NONCE_PATTERN = r"[^\s`]{6,32}"
    _OPEN_RE = re.compile(
        r"^[^\n]*?```(?P<tag>"
        r"run-x|shell-x|stdin-x:[^\n<]+|shellread-x:[^\n<]+|shellkill-x:[^\n<]+|"
        r"trigger-x(?::[^\n<]+)?|sendfile-x|read-x:[^\n<]+|read-x|edit-x(?::[^\n<]+)?|grep-x|"
        r"search-x|fetch-x|"
        r"media-x|file-x(?::[^\n<]*?)?"
        r")\s+<<AGENT_BEGIN_(?P<nonce>" + _NONCE_PATTERN + r")\s*$"
    )

    @classmethod
    def _collect_marked_body(cls, lines: List[str], start: int, marker: str):
        """收集正文直到完整的“marker + ```”结束序列。

        单独出现的三反引号、协议头或 marker 都属于正文。未找到完整结束
        序列时返回 ([], None)，确保残缺协议不会进入执行流程。
        """
        body: List[str] = []
        i = start
        while i < len(lines):
            if lines[i].rstrip() == marker:
                closer_i = i + 1
                if closer_i < len(lines) and lines[closer_i].strip() == "```":
                    return body, closer_i
            body.append(lines[i])
            i += 1
        return [], None

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
    def extract_protocol_blocks(cls, ai_response: str) -> List[Dict[str, Any]]:
        """按出现顺序提取完整的 Agent 协议块。

        任何协议只有同时满足有效开始行、nonce 匹配的结束标记和收尾
        三反引号时，才会生成可执行协议块。旧格式不会被识别。
        """
        return [
            block for block in cls._scan_blocks(ai_response)
            if block.get("executable")
        ]

    @classmethod
    def _scan_blocks(cls, ai_response: str) -> List[Dict[str, Any]]:
        """扫描出所有闭合的协议块，含不可执行的重复 nonce 块。

        重复 nonce 的块不执行，但仍要标出它的行范围：否则
        strip_protocol_blocks 不会剥离它，原始协议标记会直接显示给用户。
        """
        blocks: List[Dict[str, Any]] = []
        lines = ai_response.split("\n")
        i = 0
        seen_nonces = set()

        while i < len(lines):
            match = cls._OPEN_RE.match(lines[i].rstrip("\r"))
            if not match:
                i += 1
                continue

            nonce = match.group("nonce")
            end_marker = f"AGENT_END_{nonce}"
            body_lines, end_i = cls._collect_marked_body(
                lines,
                i + 1,
                end_marker,
            )
            if end_i is None:
                # 有效开始行之后的内容都可能属于未闭合正文；停止继续扫描，
                # 避免把正文中的协议示例误当成新的可执行协议。
                # 注意这会丢弃后面所有内容，所以 has_unclosed_block 会把这个
                # 情况报出来，让调用方提示模型重发，而不是静默什么都不做。
                break

            block = cls._build_block(
                match.group("tag").strip(),
                "\n".join(body_lines),
                i,
                end_i,
            )
            # 重复 nonce 不执行，但标记出来以便从展示文本里剥离。
            block["executable"] = nonce not in seen_nonces
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
            match = cls._OPEN_RE.match(lines[i].rstrip("\r"))
            if not match:
                i += 1
                continue
            end_marker = f"AGENT_END_{match.group('nonce')}"
            _body, end_i = cls._collect_marked_body(lines, i + 1, end_marker)
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
