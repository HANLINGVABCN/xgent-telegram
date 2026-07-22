"""纯 Agent 协议解析器。

该模块只负责把模型输出转换为结构化协议块，或移除协议块；
不执行命令、不访问文件、不发送 Telegram 消息。
"""

import re
from typing import Any, Dict, List


class ProtocolParser:
    @classmethod
    def _collect_fenced_body(cls, lines: List[str], start: int):
        """收集到下一个独占一行的 ``` 为止，返回 (body_lines, end_index)。
        与旧正则语义一致：允许 fence 行前后有空白。未闭合返回 ([], None)。"""
        body: List[str] = []
        i = start
        while i < len(lines):
            if lines[i].strip() == '```':
                return body, i
            body.append(lines[i])
            i += 1
        return body, None

    @classmethod
    def _collect_heredoc_body(cls, lines: List[str], start: int, marker: str):
        """收集到独占一行、等于 marker（去掉行尾空白后）的行。
        未闭合返回 ([], None)。内容里可含任意 fence 或特殊字符。"""
        body: List[str] = []
        i = start
        while i < len(lines):
            if lines[i].rstrip() == marker:
                return body, i
            body.append(lines[i])
            i += 1
        return body, None

    @classmethod
    def extract_protocol_blocks(cls, ai_response: str) -> List[Dict[str, Any]]:
        """按出现顺序提取 Agent 协议块，支持同一回复里出现多个协议。

        支持的 file-x 写入形式：
          - 普通 file-x:/path + 三反引号
          - heredoc: file-x:/path <<MARKER ... MARKER（内容可含 ```，推荐）
          - file-x:base64:/path + base64 body（二进制安全）
        """
        blocks: List[Dict[str, Any]] = []
        lines = ai_response.split('\n')
        i = 0
        n = len(lines)

        std_tag_re = re.compile(
            r'^```(?P<tag>run-x|shell-x|stdin-x:[^\n]+|shellread-x:[^\n]+|shellkill-x:[^\n]+|trigger-x(?::[^\n]+)?|'
            r'sendfile-x|read-x:[^\n]+|read-x|edit-x|grep-x|media-x|file-x(?::[^\n]*)?)\s*$'
        )

        while i < n:
            m = std_tag_re.match(lines[i])
            if not m:
                i += 1
                continue

            external_tag = m.group('tag').strip()
            # 外部协议统一使用 -x 命名空间；内部仍沿用原类型名，避免影响执行分支。
            tag = re.sub(r'-x(?=:|$)', '', external_tag, count=1)
            block_start = i

            # file:base64:/path  —— 二进制安全写入
            if tag.startswith('file:base64:'):
                path = tag[len('file:base64:'):].strip()
                body_lines, end_i = cls._collect_fenced_body(lines, i + 1)
                if end_i is None:
                    i += 1
                    continue
                blocks.append({
                    'type': 'file_base64',
                    'path': path,
                    'body': '\n'.join(body_lines),
                    'start_line': block_start,
                    'end_line': end_i,
                })
                i = end_i + 1
                continue

            # file:  —— 可能是 heredoc 或普通三反引号
            if tag.startswith('file:'):
                rest = tag[5:]
                hm = re.match(r'^(\S+)\s*<<\s*([A-Za-z0-9_-]+)\s*$', rest)
                if hm:
                    # heredoc 形式：内容到独占一行的 marker 结束
                    path = hm.group(1).strip()
                    marker = hm.group(2)
                    body_lines, end_i = cls._collect_heredoc_body(lines, i + 1, marker)
                    if end_i is None:
                        i += 1
                        continue
                    blocks.append({
                        'type': 'file',
                        'path': path,
                        'body': '\n'.join(body_lines),
                        'start_line': block_start,
                        'end_line': end_i,
                    })
                    # heredoc 结束行后可能还有一个收尾 ```，跳过它避免被当成下一个块的开头
                    ni = end_i + 1
                    if ni < n and lines[ni].strip() == '```':
                        ni += 1
                    i = ni
                    continue
                # 普通三反引号形式（向后兼容）
                path = rest.strip()
                body_lines, end_i = cls._collect_fenced_body(lines, i + 1)
                if end_i is None:
                    i += 1
                    continue
                blocks.append({
                    'type': 'file',
                    'path': path,
                    'body': '\n'.join(body_lines).strip(),
                    'start_line': block_start,
                    'end_line': end_i,
                })
                i = end_i + 1
                continue

            # 其余标准协议（run/shell/stdin:/shellread:/shellkill:/sendfile/read/media）
            body_lines, end_i = cls._collect_fenced_body(lines, i + 1)
            if end_i is None:
                i += 1
                continue
            raw_body = '\n'.join(body_lines)
            if tag.startswith('stdin:'):
                blocks.append({
                    'type': 'stdin',
                    'path': tag[6:].strip(),
                    'body': raw_body,
                    'start_line': block_start,
                    'end_line': end_i,
                })
            elif tag.startswith('shellread:'):
                blocks.append({
                    'type': 'shellread',
                    'path': tag[10:].strip(),
                    'body': raw_body.strip(),
                    'start_line': block_start,
                    'end_line': end_i,
                })
            elif tag.startswith('shellkill:'):
                blocks.append({
                    'type': 'shellkill',
                    'path': tag[10:].strip(),
                    'body': raw_body.strip(),
                    'start_line': block_start,
                    'end_line': end_i,
                })
            elif tag == 'trigger' or tag.startswith('trigger:'):
                blocks.append({
                    'type': 'trigger',
                    'path': tag[8:].strip() if tag.startswith('trigger:') else '',
                    'body': raw_body,
                    'start_line': block_start,
                    'end_line': end_i,
                })
            elif tag.startswith('read:'):
                # read:<path>[:START-END] 或 read:<path>:START:COUNT
                # 路径可能含冒号（Windows 盘符），区间解析交给执行分支
                blocks.append({
                    'type': 'read',
                    'path': tag[5:].strip(),
                    'body': raw_body.strip(),
                    'start_line': block_start,
                    'end_line': end_i,
                })
            elif tag == 'edit':
                blocks.append({
                    'type': 'edit',
                    'path': '',
                    'body': raw_body.strip('\n'),
                    'start_line': block_start,
                    'end_line': end_i,
                })
            elif tag == 'grep':
                blocks.append({
                    'type': 'grep',
                    'path': '',
                    'body': raw_body.strip('\n'),
                    'start_line': block_start,
                    'end_line': end_i,
                })
            else:
                blocks.append({
                    'type': tag,
                    'path': '',
                    'body': raw_body.strip(),
                    'start_line': block_start,
                    'end_line': end_i,
                })
            i = end_i + 1

        return blocks

    @classmethod
    def strip_protocol_blocks(cls, ai_response: str) -> str:
        """从 AI 回复中剔除所有协议块，避免文件内容被当成普通文本发回。
        与 extract_protocol_blocks 共用同一套行扫描逻辑，单一真相源。"""
        blocks = cls.extract_protocol_blocks(ai_response)
        if not blocks:
            return ai_response

        lines = ai_response.split('\n')
        keep = [True] * len(lines)
        for block in blocks:
            start = block.get('start_line')
            end = block.get('end_line')
            if start is None or end is None:
                continue
            for k in range(start, min(end + 1, len(lines))):
                keep[k] = False
            ni = end + 1
            if ni < len(lines) and lines[ni].strip() == '```':
                keep[ni] = False

        result = [lines[k] for k in range(len(lines)) if keep[k]]
        cleaned: List[str] = []
        prev_blank = False
        for ln in result:
            is_blank = ln.strip() == ''
            if is_blank and prev_blank:
                continue
            cleaned.append(ln)
            prev_blank = is_blank
        return '\n'.join(cleaned)

