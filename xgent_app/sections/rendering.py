# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

async def keep_typing_while_waiting(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                    stop_event: asyncio.Event, interval: float = 4.0,
                                    max_duration: Optional[float] = None,
                                    watch_task: Optional[asyncio.Task] = None):
    """Keep Telegram typing status alive while the model is still working.

    ``max_duration`` 是防泄漏兜底：调用方异常退出而未设置 stop_event 时，
    任务也会在超时后自行结束，不会无限发送 typing 状态。
    """
    started_at = time.monotonic()
    while not stop_event.is_set():
        if watch_task is not None and watch_task.done():
            return
        if max_duration is not None and time.monotonic() - started_at >= max_duration:
            return
        try:
            await context.bot.send_chat_action(
                chat_id=chat_id,
                action=constants.ChatAction.TYPING
            )
        except Exception as e:
            logger.debug(f"typing 状态续期失败: {e}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue

# --- ☆ Rich Messages (Bot API 10.1) ☆ ---
# Telegram Bot API 10.1 (2026-06-11) 原生支持表格、标题、引用块、分割线、嵌套列表等富文本。
# 使用 sendRichMessage / sendRichMessageDraft 直接发送结构化 JSON，
# 彻底解决旧 HTML 模式下表格被包在 <pre> 里、链接嵌套失效、引用块不渲染、--- 显示原文等问题。

# Rich Message 字符上限（API 10.1 提升至 32768）
RICH_MESSAGE_CHAR_LIMIT = 32000

class TelegramRichAPI:
    """直接调用 Telegram Bot API 的 Rich Message 端点（绕过 python-telegram-bot 库版本限制）。"""

    _client: Optional[httpx.AsyncClient] = None

    @classmethod
    def _get_client(cls) -> httpx.AsyncClient:
        if cls._client is None or cls._client.is_closed:
            cls._client = httpx.AsyncClient(timeout=30.0)
        return cls._client

    @classmethod
    def _api_url(cls, method: str) -> str:
        base = BotConfig.API_BASE_URL or "https://api.telegram.org"
        return f"{base}/bot{BotConfig.TOKEN}/{method}"

    @classmethod
    async def send_rich_message(cls, chat_id: int, text: str,
                                 reply_markup: Optional[Dict] = None,
                                 reply_to_message_id: Optional[int] = None) -> Dict:
        """发送完整的 Rich Message。返回 Telegram API 响应 JSON。

        Bot API 10.1 的 rich_message 参数接受 {"markdown": "..."} 格式，
        由 Telegram 服务端自行解析 Markdown 为原生 RichBlock（表格/标题/列表等），
        无需客户端自行构建 block_tree。
        """
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": text or " "},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        client = cls._get_client()
        resp = await client.post(cls._api_url("sendRichMessage"), json=payload)
        result = resp.json()
        if not result.get("ok"):
            raise TelegramError(f"sendRichMessage failed: {result.get('description', result)}")
        return result

    @classmethod
    async def send_rich_message_draft(cls, chat_id: int, text: str,
                                       draft_id: int) -> Dict:
        """发送/更新流式 Rich Message Draft。

        Bot API 10.1: sendRichMessageDraft 在私聊中创建一个 30 秒临时的 Draft 预览。
        相同 draft_id 的后续调用会以动画过渡更新内容。
        完成后必须调用 send_rich_message 发送最终消息以持久化。
        API 返回 True（非 Message 对象），draft_id 由调用方生成。
        """
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": {"markdown": text or " "},
        }
        client = cls._get_client()
        resp = await client.post(cls._api_url("sendRichMessageDraft"), json=payload)
        result = resp.json()
        if not result.get("ok"):
            # 429 限流：Telegram 返回 parameters.retry_after，抛 RetryAfter
            # 让上层 append() 捕获后永久降级到 HTML edit，避免后续 chunk 反复撞 429
            params = result.get("parameters") or {}
            retry_after = params.get("retry_after")
            if retry_after is not None:
                raise RetryAfter(retry_after=retry_after)
            raise TelegramError(f"sendRichMessageDraft failed: {result.get('description', result)}")
        return result

async def rich_finalize_text_response(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                       msg: Any, response: str, limit: int = RICH_MESSAGE_CHAR_LIMIT):
    """使用 Rich Message 发送最终回复。失败时 fallback 到旧 HTML 编辑模式。"""
    # sendRichMessage 直连 Telegram Bot API，绕过 context.bot。网页会话用的是
    # WebBot / MirrorBot（_is_xgent_web_bot），直连既不会往网页 outbox 推帧，又
    # 会对纯网页会话误发到 Telegram。网页端改走 finalize_text_response：经
    # context.bot 发 edit/message 帧，网页看得到回复，MirrorBot 也能正常镜像到
    # TG（表格等仍以 <pre> 等宽块呈现，仅失去原生 RichBlock 排版）。
    if getattr(context.bot, "_is_xgent_web_bot", False):
        await finalize_text_response(context, chat_id, msg, response, min(limit, 4000))
        return
    try:
        await TelegramRichAPI.send_rich_message(
            chat_id=chat_id,
            text=response,
        )
        # 删除原来的占位消息
        try:
            await msg.delete()
        except Exception:
            pass
        logger.info(f"Rich Message 发送成功: chat_id={chat_id}")
    except Exception as e:
        logger.warning(f"Rich Message 最终发送失败，降级为 HTML 编辑模式: {e}")
        await finalize_text_response(context, chat_id, msg, response, min(limit, 4000))


def _parse_markdown_table_row(line: str) -> Optional[List[str]]:

    """把一行 `| a | b |` 解析成单元格列表；不是表格行返回 None。"""
    stripped = line.strip()
    if '|' not in stripped:
        return None
    # 必须以 | 开头或结尾（表格行的典型特征）；也允许单列内含 | 但首尾有 | 的情况
    if not stripped.startswith('|'):
        return None
    inner = stripped
    if inner.startswith('|'):
        inner = inner[1:]
    if inner.endswith('|'):
        inner = inner[:-1]
    cells = [c.strip() for c in inner.split('|')]
    # 至少两列才算表格行（单列 |x| 视作普通文本，避免误伤）
    if len(cells) < 2:
        return None
    return cells


def _is_table_separator_row(cells: Optional[List[str]]) -> bool:
    """判断是否是表格分隔行：单元格全是 --- / :--: / --: 之类。"""
    if not cells:
        return False
    sep_re = re.compile(r'^:?-{1,}:?$')
    return all(sep_re.match(c) and '-' in c for c in cells)


def _build_table_pre_block(rows_cells: List[List[str]]) -> str:
    """把多行单元格渲染成等宽对齐的 <pre> 块。rows_cells 含表头+分隔占位+数据行。"""
    # 跳过分隔行本身（它是表格语法的分隔，不展示）
    display_rows = [r for r in rows_cells if not _is_table_separator_row(r)]
    if not display_rows:
        return ''
    # 表格放进 <pre> 等宽块后，单元格内的行内代码反引号是多余的，去掉只保留内容
    display_rows = [[re.sub(r'`([^`]*)`', r'\1', c) for c in row] for row in display_rows]
    num_cols = max(len(r) for r in display_rows)
    # 补齐每行列数
    for r in display_rows:
        while len(r) < num_cols:
            r.append('')

    # 计算每列最大显示宽度（按字符数，中文按 2 计宽以便对齐）
    def _cell_width(s: str) -> int:
        width = 0
        for ch in s:
            width += 2 if ord(ch) > 0x2E80 else 1  # CJK 及全角符号按 2
        return width

    col_widths = [0] * num_cols
    for r in display_rows:
        for i, cell in enumerate(r):
            col_widths[i] = max(col_widths[i], _cell_width(cell))

    # 拼接对齐后的文本（左对齐，右侧补空格），列间用 "  " 分隔
    lines: List[str] = []
    for row_idx, r in enumerate(display_rows):
        parts = []
        for i, cell in enumerate(r):
            pad = col_widths[i] - _cell_width(cell)
            parts.append(cell + ' ' * max(0, pad))
        lines.append('  '.join(parts).rstrip())
        # 在表头下方插入分隔线（ASCII 表格观感）
        if row_idx == 0:
            sep_parts = []
            for i in range(num_cols):
                sep_parts.append('-' * col_widths[i])
            lines.append('  '.join(sep_parts))

    return f"<pre>{html.escape(chr(10).join(lines))}</pre>"


def _extract_markdown_tables(text: str) -> Tuple[str, List[str]]:
    """提取文本中的完整 Markdown 表格，替换为占位符。

    只提取「完整」表格（含表头+分隔行+至少一数据行）。
    流式输出中尚未出现分隔行的半成品不会被识别，避免乱码。
    返回 (替换后的文本, 表格HTML列表)。
    """
    tables: List[str] = []
    if '|' not in text:
        return text, tables

    lines = text.split('\n')
    out_lines: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        row_cells = _parse_markdown_table_row(lines[i])
        # 判断是否是一个表格的起点：本行是表格行，且下一行是分隔行
        if row_cells and i + 1 < n and _is_table_separator_row(_parse_markdown_table_row(lines[i + 1])):
            # 收集连续的表格行（含表头、分隔、数据）
            block: List[List[str]] = [row_cells]
            j = i + 1
            while j < n:
                next_cells = _parse_markdown_table_row(lines[j])
                if next_cells is None:
                    break
                block.append(next_cells)
                j += 1
            pre_html = _build_table_pre_block(block)
            idx = len(tables)
            tables.append(pre_html)
            out_lines.append(f'\x02TBL{idx}\x02')
            i = j
            continue
        out_lines.append(lines[i])
        i += 1

    return '\n'.join(out_lines), tables


def _inline_markdown_to_html(text: str) -> str:
    """将行内 Markdown 转换为 Telegram HTML（非代码文本部分）。"""
    if not text:
        return ""

    # 先提取 Markdown 表格为 <pre> 占位符（表格内容整体等宽对齐，不再参与行内转换）
    text, table_blocks = _extract_markdown_tables(text)

    # 再提取行内代码（保护其内容不被后续处理影响）
    inline_codes: List[str] = []

    def _save_inline(m: re.Match) -> str:
        idx = len(inline_codes)
        inline_codes.append(f'<code>{html.escape(m.group(1))}</code>')
        return f'\x01IC{idx}\x01'

    text = re.sub(r'`([^`]+)`', _save_inline, text)

    # 提取链接 [text](url)，在 html.escape 之前保护 URL 不被双重转义
    # 正则支持 URL 中的一层嵌套括号（如 Wikipedia 链接）
    link_blocks: List[str] = []

    def _save_link(m: re.Match) -> str:
        idx = len(link_blocks)
        link_text = html.escape(m.group(1), quote=False)
        # URL 必须转义：href 属性里的 & 要写成 &amp;，否则 Telegram 报
        # "can't parse entities"；URL 里的引号会直接闭合属性。
        link_url = html.escape(m.group(2), quote=True)
        link_blocks.append(f'<a href="{link_url}">{link_text}</a>')
        return f'\x01LK{idx}\x01'

    text = re.sub(r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _save_link, text)

    # HTML 转义剩余文本（行内代码、表格、链接已被提取为占位符，不受影响）
    text = html.escape(text, quote=False)

    # 逐行处理块级元素：标题、引用、列表
    lines = text.split('\n')
    processed: List[str] = []
    blockquote_buffer: List[str] = []

    for line in lines:
        stripped = line.strip()

        # 引用块（> 在 HTML 转义后变为 &gt;）
        if stripped.startswith('&gt; '):
            blockquote_buffer.append(stripped[5:])
            continue
        else:
            if blockquote_buffer:
                processed.append(f'<blockquote>{"<br>".join(blockquote_buffer)}</blockquote>')
                blockquote_buffer = []

        # 标题：# ## ### 等 → 粗体
        m = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if m:
            processed.append(f'<b>{m.group(2)}</b>')
            continue

        # 无序列表：- 或 * 开头 → 替换为 •
        if re.match(r'^[\-\*]\s+', stripped):
            processed.append(re.sub(r'^[\-\*]\s+', '\u2022 ', stripped))
            continue

        # 有序列表：保持原样
        if re.match(r'^\d+\.\s+', stripped):
            processed.append(stripped)
            continue

        processed.append(line)

    if blockquote_buffer:
        processed.append(f'<blockquote>{"<br>".join(blockquote_buffer)}</blockquote>')

    text = '\n'.join(processed)

    # 粗斜体：***text***（必须在粗体和斜体之前处理）
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text, flags=re.DOTALL)

    # 粗体：**text** 或 __text__（支持跨行，但不跨空行）
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text, flags=re.DOTALL)

    # 删除线：~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 斜体：*text*（在粗体之后处理，避免 ** 冲突）
    # 用 [^\W_] 代替 \w（排除下划线），防止颜文字 (*_*) 中的 * 被误匹配
    text = re.sub(r'(?<!\*)\*(?=[^\W_])(.+?)(?<=[^\W_])\*(?!\*)', r'<i>\1</i>', text)

    # 斜体（下划线）：_text_（避免匹配 snake_case 和颜文字 (>_<) 中的 _）
    # 用 [^\W_] 代替 \w（排除下划线），防止颜文字中的 _ 被误匹配
    text = re.sub(r'(?<![^\W_])_(?=[^\W_])(.+?)(?<=[^\W_])_(?![^\W_])', r'<i>\1</i>', text)

    # 还原行内代码占位符
    for i, code_html in enumerate(inline_codes):
        text = text.replace(f'\x01IC{i}\x01', code_html)

    # 还原链接占位符
    for i, link_html in enumerate(link_blocks):
        text = text.replace(f'\x01LK{i}\x01', link_html)

    # 还原表格 <pre> 占位符（表格 HTML 已构建好，直接放回；不在表格内做行内转换）
    for i, pre_html in enumerate(table_blocks):
        # 转义后的文本里占位符字符 \x02 不受 html.escape 影响，仍可匹配
        text = text.replace(f'\x02TBL{i}\x02', pre_html)

    return text


def _try_parse_text_table_in_codeblock(code: str) -> Optional[str]:
    """尝试将代码块中的空格对齐纯文本表格转换为 <pre> 对齐表格。

    AI 有时会把表格数据包在 ``` 代码块中，用空格对齐列、用 ---- 做分隔行，
    而非使用标准 Markdown | 语法。此函数检测这种模式并转换为等宽 <pre> 块。
    返回转换后的 HTML，或 None 表示不是文本表格。
    """
    lines = code.strip().split('\n')
    if len(lines) < 3:  # 至少需要：表头、分隔行、一行数据
        return None

    # 检测分隔行（第二行应该主要由 - 和空格组成）
    sep_line = lines[1].strip()
    # 分隔行的模式：连续的 --- 段用空格隔开，或者 |---|---| 格式
    dashes = sep_line.replace(' ', '')
    if not dashes or len(dashes) < 3:
        return None
    dash_ratio = sum(1 for c in dashes if c == '-') / len(dashes)
    if dash_ratio < 0.8:  # 至少 80% 的非空白字符是 -
        return None

    # 用分隔行的 ---- 段来确定列的位置
    # 找出每个 ---- 段的起止位置
    col_spans: List[Tuple[int, int]] = []
    in_dash = False
    start = 0
    for ci, ch in enumerate(lines[1]):
        if ch == '-':
            if not in_dash:
                start = ci
                in_dash = True
        else:
            if in_dash:
                col_spans.append((start, ci))
                in_dash = False
    if in_dash:
        col_spans.append((start, len(lines[1])))

    if len(col_spans) < 2:  # 至少需要 2 列才算表格
        return None

    # 用列位置来分割每行
    def split_by_spans(line: str) -> List[str]:
        cells = []
        for si, (cs, ce) in enumerate(col_spans):
            # 最后一列延伸到行尾
            end = ce if si < len(col_spans) - 1 else max(ce, len(line))
            cell = line[cs:end].strip() if cs < len(line) else ''
            cells.append(cell)
        return cells

    # 提取所有行（跳过分隔行）
    table_rows: List[List[str]] = []
    header = split_by_spans(lines[0])
    table_rows.append(header)
    for li in range(2, len(lines)):  # 跳过分隔行(index 1)
        line = lines[li]
        if not line.strip():
            continue
        # 跳过额外的分隔行
        stripped_nospace = line.strip().replace(' ', '')
        if stripped_nospace and all(c == '-' for c in stripped_nospace):
            continue
        table_rows.append(split_by_spans(line))

    if len(table_rows) < 2:  # 至少需要表头 + 1行数据
        return None

    # 用 _build_table_pre_block 的逻辑来构建对齐的 <pre> 表格
    return _build_table_pre_block(table_rows)


def markdown_to_telegram_html(text: str) -> str:
    """将 Markdown 转换为 Telegram 兼容的 HTML。

    支持：代码块、行内代码、粗体、斜体、删除线、链接、标题、引用、列表。
    自动处理不完整的 Markdown（用于流式输出中尚未闭合的标记）。
    """
    if not text:
        return ""

    # 流式输出中可能有未闭合的代码块，临时补全
    fence_count = len(re.findall(r'```', text))
    if fence_count % 2 == 1:
        text = text + '\n```'

    # 用正则分割代码块和非代码文本
    segments = re.split(r'(```\w*\n?.*?```)', text, flags=re.DOTALL)

    result: List[str] = []
    for seg in segments:
        if not seg:
            continue
        if seg.startswith('```'):
            # 代码块
            m = re.match(r'```(\w*)\n?(.*?)```', seg, re.DOTALL)
            if m:
                lang = m.group(1) or ''
                code = m.group(2)
                # 无语言标注（或 text/plain）的代码块：检查是否是伪装的文本表格
                if not lang or lang.lower() in ('text', 'plain'):
                    table_html = _try_parse_text_table_in_codeblock(code)
                    if table_html:
                        result.append(table_html)
                        continue
                escaped = html.escape(code)
                if lang and lang.lower() not in ('text', 'plain'):
                    result.append(f'<pre><code class="language-{lang}">{escaped}</code></pre>')
                else:
                    result.append(f'<pre>{escaped}</pre>')
            else:
                result.append(html.escape(seg))
        else:
            result.append(_inline_markdown_to_html(seg))

    return ''.join(result)


def split_text_for_telegram(text: str, limit: int = 4000) -> List[str]:
    """Split long plain-text replies into Telegram-safe chunks."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    soft_limit = max(1, limit // 2)

    while len(remaining) > limit:
        split_at = remaining.rfind('\n', 0, limit)
        if split_at < soft_limit:
            split_at = remaining.rfind(' ', 0, limit)
        if split_at < soft_limit:
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit

        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks

def plain_text_from_html(text: str) -> str:
    cleaned = re.sub(r'</(p|div|br|pre|blockquote|li|h[1-6])\s*>', '\n', str(text), flags=re.I)
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    return html.unescape(cleaned)

def _sanitize_telegram_html(text: str) -> str:
    """尝试修复无效的 Telegram HTML，而非直接降级到纯文本。

    Telegram 只支持有限的 HTML 标签（b, i, u, s, a, code, pre, blockquote）。
    此函数移除所有不支持的标签，修复常见的解析错误，尽可能保留有效格式。
    """
    # Telegram 支持的标签
    ALLOWED_TAGS = {'b', 'i', 'u', 's', 'a', 'code', 'pre', 'blockquote', 'tg-spoiler', 'tg-emoji'}

    def _replace_tag(m: re.Match) -> str:
        full = m.group(0)
        tag_match = re.match(r'</?([a-zA-Z][a-zA-Z0-9-]*)(?:\s|>|/)', full)
        if not tag_match:
            return html.escape(full)
        tag_name = tag_match.group(1).lower()
        if tag_name in ALLOWED_TAGS:
            return full  # 保留合法标签
        return html.escape(full)  # 转义非法标签

    result = re.sub(r'<[^>]+>', _replace_tag, text)

    # 修复未闭合的标签：统计开闭标签，补全缺失的闭合标签
    open_tags: List[str] = []
    for m in re.finditer(r'<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>',  result):
        is_close = m.group(1) == '/'
        tag = m.group(2).lower()
        if tag not in ALLOWED_TAGS:
            continue
        if tag in ('pre', 'code'):  # pre/code 自闭合错误是最常见的 parse entities 原因
            pass
        if is_close:
            if open_tags and open_tags[-1] == tag:
                open_tags.pop()
        else:
            open_tags.append(tag)
    # 按 LIFO 顺序补全未闭合标签
    for tag in reversed(open_tags):
        result += f'</{tag}>'

    return result

async def safe_send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: Any,
                            limit: int = 3900, **kwargs: Any) -> List[Any]:
    """Send text without letting Telegram's per-message limit break the handler."""
    raw_text = str(text if text is not None else "")
    if not raw_text:
        raw_text = " "
    parse_mode = kwargs.get('parse_mode')
    chunks = split_text_for_telegram(raw_text, limit)
    sent: List[Any] = []

    for index, chunk in enumerate(chunks):
        send_kwargs = dict(kwargs)
        if index > 0:
            send_kwargs.pop('reply_markup', None)
        try:
            sent.append(await context.bot.send_message(chat_id=chat_id, text=chunk, **send_kwargs))
        except RetryAfter as e:
            await asyncio.sleep(_retry_after_seconds(e) + 0.1)
            sent.append(await context.bot.send_message(chat_id=chat_id, text=chunk, **send_kwargs))
        except BadRequest as e:
            message = str(e).lower()
            if parse_mode and "can't parse entities" in message:
                # 先尝试清理 HTML 保留格式，而非直接降级到纯文本
                logger.warning(f"HTML 解析失败，尝试清理后重发: {e}")
                sanitized = _sanitize_telegram_html(chunk)
                try:
                    sent.append(await context.bot.send_message(
                        chat_id=chat_id, text=sanitized, **send_kwargs
                    ))
                    continue
                except BadRequest:
                    pass  # 清理后仍失败，降级到纯文本
                fallback_kwargs = dict(send_kwargs)
                fallback_kwargs.pop('parse_mode', None)
                fallback_text = plain_text_from_html(chunk)
                for fallback_chunk in split_text_for_telegram(fallback_text, limit):
                    sent.append(await context.bot.send_message(
                        chat_id=chat_id,
                        text=fallback_chunk,
                        **fallback_kwargs
                    ))
                continue
            if parse_mode and "message is too long" in message:
                fallback_kwargs = dict(send_kwargs)
                fallback_kwargs.pop('parse_mode', None)
                fallback_text = plain_text_from_html(chunk)
                for fallback_chunk in split_text_for_telegram(fallback_text, limit):
                    sent.append(await context.bot.send_message(
                        chat_id=chat_id,
                        text=fallback_chunk,
                        **fallback_kwargs
                    ))
                continue
            if "message is too long" in message:
                fallback_kwargs = dict(send_kwargs)
                fallback_kwargs.pop('parse_mode', None)
                for fallback_chunk in split_text_for_telegram(plain_text_from_html(chunk), 3000):
                    sent.append(await context.bot.send_message(
                        chat_id=chat_id,
                        text=fallback_chunk,
                        **fallback_kwargs
                    ))
                continue
            raise

    return sent

async def finalize_text_response(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg: Any,
                                 response: str, limit: int = 4000):
    html_response = markdown_to_telegram_html(response)
    chunks = split_text_for_telegram(html_response, limit)
    logger.info(
        f"Sending final Telegram response: chat_id={chat_id}, "
        f"text_len={len(response)}, html_len={len(html_response)}, chunks={len(chunks)}"
    )
    await safe_edit_text(msg, chunks[0], reply_markup=None, parse_mode=constants.ParseMode.HTML)
    for extra_chunk in chunks[1:]:
        await safe_send_message(context, chat_id, extra_chunk, parse_mode=constants.ParseMode.HTML)

def _retry_after_seconds(exc: RetryAfter) -> float:
    retry_after = getattr(exc, 'retry_after', 1.0)
    if hasattr(retry_after, 'total_seconds'):
        return float(retry_after.total_seconds())
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return 1.0

async def safe_edit_text(msg: Any, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None,
                         parse_mode: Optional[str] = None,
                         retry_on_retry_after: bool = True) -> bool:
    """Edit a Telegram message while tolerating no-op edits, flood-wait pacing, and HTML parse failures.

    ``retry_on_retry_after=False`` 时，第一次撞 RetryAfter 不 sleep + 重试，
    而是直接抛 RetryAfter 给调用方处理。流式 append 用这个模式，配合冷却期
    跳过限流期间的 edit 调用，避免 sleep 阻塞流式 loop。
    """
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except RetryAfter as e:
        if not retry_on_retry_after:
            raise  # 流式模式：直接抛给调用方设冷却期，不内部 sleep
        await asyncio.sleep(_retry_after_seconds(e) + 0.1)
        try:
            await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except BadRequest as retry_bad_request:
            msg_lower = str(retry_bad_request).lower()
            if "message is not modified" in msg_lower:
                return True
            if parse_mode and "can't parse entities" in msg_lower:
                logger.warning(f"HTML 解析失败，尝试清理后重发: {retry_bad_request}")
                # 先尝试清理 HTML 保留格式
                try:
                    await msg.edit_text(_sanitize_telegram_html(text), reply_markup=reply_markup, parse_mode=parse_mode)
                    return True
                except BadRequest:
                    pass  # 清理后仍失败，降级到纯文本
                try:
                    await msg.edit_text(plain_text_from_html(text), reply_markup=reply_markup)
                    return True
                except BadRequest as fallback_err:
                    if "message is not modified" in str(fallback_err).lower():
                        return True
                    raise
            raise
    except BadRequest as e:
        msg_lower = str(e).lower()
        if "message is not modified" in msg_lower:
            return True
        if parse_mode and "can't parse entities" in msg_lower:
            logger.warning(f"HTML 解析失败，尝试清理后重发: {e}")
            # 先尝试清理 HTML 保留格式
            try:
                await msg.edit_text(_sanitize_telegram_html(text), reply_markup=reply_markup, parse_mode=parse_mode)
                return True
            except BadRequest:
                pass  # 清理后仍失败，降级到纯文本
            try:
                await msg.edit_text(plain_text_from_html(text), reply_markup=reply_markup)
                return True
            except BadRequest as fallback_err:
                if "message is not modified" in str(fallback_err).lower():
                    return True
                raise
        raise

def _drain_task_result(task: asyncio.Task):
    """Consume a finished task's result so background cancellation never logs noisy warnings."""
    if not task.done():
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()

async def cancel_task_quietly(task: Optional[asyncio.Task], timeout: float = 1.0):
    """Request cancellation without letting a stubborn network call freeze the stop button."""
    if task is None:
        return
    if task.done():
        _drain_task_result(task)
        return

    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        _drain_task_result(task)
    else:
        task.add_done_callback(_drain_task_result)

class TelegramStreamRenderer:
    """Render upstream streaming chunks to Telegram using Rich Message drafts (Bot API 10.1).

    优先使用 sendRichMessageDraft 推送流式内容（不受 edit_message 频率限制），
    失败时自动降级为旧的 edit_message_text HTML 模式。
    """

    FLUSH_INTERVAL_SECONDS = 0.35
    MIN_CHARS_PER_FLUSH = 12

    def __init__(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg: Any,
                 reply_markup: InlineKeyboardMarkup, limit: int = RICH_MESSAGE_CHAR_LIMIT,
                 stop_event: Optional[asyncio.Event] = None):
        self.context = context
        self.chat_id = chat_id
        self.current_msg = msg
        self.reply_markup = reply_markup
        self.limit = limit
        self.stop_event = stop_event
        self.queue: asyncio.Queue = asyncio.Queue()
        self.response_parts: List[str] = []
        self.current_text = ""
        self.pending_text = ""
        self._task: Optional[asyncio.Task] = None
        self.live_edit_enabled = True
        # Rich Message Draft 模式
        # draft_id 由 bot 自行生成（API 要求非零整数），相同 draft_id 的调用会动画过渡更新
        self._draft_id: int = int(time.time() * 1000) % 0x7FFFFFFF + 1
        self._rich_draft_enabled = True  # 尝试使用 Rich Draft，失败则降级
        # HTML edit 冷却期：撞 429 RetryAfter 后，retry_after 秒内跳过 edit 调用，
        # 避免每个 chunk 都撞 429 浪费请求；冷却期过后再试，限流可能已解除。
        # 不永久关闭 live_edit_enabled，让 UI 在冷却期后能恢复更新。
        self._html_edit_cooldown_until: float = 0.0
        # sendRichMessage(Draft) 直连 Telegram Bot API，绕过 context.bot；网页会话
        # 走 WebBot/MirrorBot，直连既不往网页 outbox 推帧，又会把 draft 发到 TG。
        # 网页端禁用 Rich Draft，流式刷新与收尾都走 HTML edit（经 context.bot，
        # 网页收 edit 帧，MirrorBot 也正常镜像到 TG）。
        self._is_web_bot = bool(getattr(context.bot, "_is_xgent_web_bot", False))
        if self._is_web_bot:
            self._rich_draft_enabled = False

    def start(self):
        self._task = asyncio.create_task(self._render_loop())

    async def append(self, text: str):
        if not text:
            return
        self.response_parts.append(text)
        await self.queue.put(text)

    async def finish(self) -> str:
        await self.queue.put(None)
        if self._task:
            await self._task
        await self.remove_controls()
        return ''.join(self.response_parts).strip()

    async def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def stop_and_keep_partial(self) -> str:
        """Stop live rendering, flush pending text, and keep already generated content visible."""
        if self.pending_text:
            await self._flush_pending()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        partial = ''.join(self.response_parts).strip()
        visible_text = self.current_text.strip() or partial
        if visible_text:
            # 用 Rich Message 固化已生成内容（网页会话绕过：见 __init__ 注释）
            if not self._is_web_bot:
                try:
                    await TelegramRichAPI.send_rich_message(chat_id=self.chat_id, text=visible_text + "\n\n⏹️ 已停止，保留以上已生成内容。")
                    try:
                        await self.current_msg.delete()
                    except Exception:
                        pass
                    return partial
                except Exception:
                    pass
            # 网页会话或 Rich 固化失败：降级为 HTML 编辑（经 context.bot）
            html_visible = markdown_to_telegram_html(visible_text)
            stopped_text = html_visible + "\n\n\u23f9\ufe0f 已停止，保留以上已生成内容。"
        else:
            stopped_text = "\u23f9\ufe0f 已停止，还没有生成可保留的内容。"
        if self.live_edit_enabled:
            try:
                await safe_edit_text(self.current_msg, stopped_text, reply_markup=None,
                                     parse_mode=constants.ParseMode.HTML)
            except Exception as e:
                logger.debug(f"停止时保留流式内容失败: {e}")
        return partial

    async def remove_controls(self):
        """流式完成后：用 Rich Message 发送最终内容，删除旧占位消息。"""
        if not self.current_text.strip():
            return
        if self.live_edit_enabled and not self._is_web_bot:
            # 优先尝试 Rich Message 固化（网页会话绕过：见 __init__ 注释）
            try:
                await TelegramRichAPI.send_rich_message(chat_id=self.chat_id, text=self.current_text)
                try:
                    await self.current_msg.delete()
                except Exception:
                    pass
                return
            except Exception as e:
                logger.debug(f"Rich Message 固化失败，降级为 HTML: {e}")
        # 网页会话或 Rich 固化失败：降级为 HTML 编辑（经 context.bot，网页收 edit 帧）
        if self.live_edit_enabled:
            try:
                html_text = markdown_to_telegram_html(self.current_text)
                await safe_edit_text(self.current_msg, html_text, reply_markup=None,
                                     parse_mode=constants.ParseMode.HTML)
            except Exception as e:
                logger.debug(f"移除流式停止按钮失败: {e}")

    async def _render_loop(self):
        while True:
            if self.stop_event and self.stop_event.is_set():
                break
            try:
                chunk = await asyncio.wait_for(self.queue.get(), timeout=self.FLUSH_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                if self.pending_text:
                    await self._flush_pending()
                continue

            if chunk is None:
                if self.pending_text:
                    await self._flush_pending()
                break

            self.pending_text += chunk
            if len(self.pending_text) >= self.MIN_CHARS_PER_FLUSH or '\n' in chunk:
                await self._flush_pending()

    async def _flush_pending(self):
        if not self.pending_text:
            return
        text = self.pending_text
        self.pending_text = ""

        if len(self.current_text) + len(text) > self.limit:
            if self.live_edit_enabled and self.current_text.strip():
                try:
                    html_text = markdown_to_telegram_html(self.current_text)
                    await safe_edit_text(self.current_msg, html_text, reply_markup=None,
                                         parse_mode=constants.ParseMode.HTML)
                except Exception as e:
                    self.live_edit_enabled = False
                    logger.warning(f"流式消息刷新失败，改为结束后一次性发送: {e}")
            self.current_text = text
            if self.live_edit_enabled:
                try:
                    new_text = text if text.strip() else "…"
                    html_text = markdown_to_telegram_html(new_text)
                    self.current_msg = await self.context.bot.send_message(
                        chat_id=self.chat_id,
                        text=html_text,
                        reply_markup=self.reply_markup,
                        parse_mode=constants.ParseMode.HTML
                    )
                except Exception as e:
                    self.live_edit_enabled = False
                    logger.warning(f"流式消息分段发送失败，改为结束后一次性发送: {e}")
            return

        self.current_text += text
        if not self.current_text.strip():
            return
        if not self.live_edit_enabled:
            return

        # 优先使用 Rich Message Draft 推送
        if self._rich_draft_enabled:
            try:
                # 文本较短时在开头添加 <tg-thinking> 思考块（API 10.1 Draft 专属）
                # 给用户"AI 正在思考"的视觉反馈，文本足够长后自动移除
                draft_text = self.current_text
                if len(draft_text.strip()) < 80:
                    draft_text = "<tg-thinking>正在思考…</tg-thinking>\n\n" + draft_text
                await TelegramRichAPI.send_rich_message_draft(
                    chat_id=self.chat_id,
                    text=draft_text,
                    draft_id=self._draft_id,
                )
                return
            except RetryAfter as e:
                # Telegram 429 限流：永久降级到 HTML edit，避免后续 chunk 反复撞 429。
                # sleep retry_after+0.1 后继续走 HTML edit 路径（不 return），让本
                # 次 chunk 也能立刻可见。HTML edit 内部的 safe_edit_text 还会再
                # 兜底一次 RetryAfter。
                wait = _retry_after_seconds(e) + 0.1
                logger.warning(f"Rich Draft 被 Telegram 限流 (retry_after={wait:.1f}s)，永久降级为 HTML edit")
                self._rich_draft_enabled = False
                # sleep 期间监听 stop_event，set 了立即返回（不继续走 HTML edit）。
                # 这样流式 loop 不会被 sleep 阻塞，能立刻回到主循环响应停止。
                _stop_event = get_or_create_stop_event()
                sleep_task = asyncio.create_task(asyncio.sleep(wait))
                stop_task = asyncio.create_task(_stop_event.wait())
                done, _pending = await asyncio.wait({sleep_task, stop_task},
                                                     return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done and _stop_event.is_set():
                    sleep_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sleep_task
                    return
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
            except Exception as e:
                logger.info(f"Rich Draft 推送失败，降级为 HTML edit: {e}")
                self._rich_draft_enabled = False

        # 降级为旧的 edit_message_text HTML 模式
        # 冷却期检查：撞 429 后 retry_after 秒内跳过 edit，避免每个 chunk 都撞 429
        if time.monotonic() < self._html_edit_cooldown_until:
            return  # 冷却期内，跳过这次 edit，下个 chunk 再试

        # 用 asyncio.wait 把 edit 和 stop_event.wait() 并发等待，stop_event set
        # 立即取消 edit_task 返回。safe_edit_text 用 retry_on_retry_after=False
        # 模式，第一次撞 RetryAfter 直接抛（不内部 sleep），让这里能立即设冷却期。
        try:
            html_text = markdown_to_telegram_html(self.current_text)
            _stop_event = get_or_create_stop_event()
            edit_task = asyncio.create_task(safe_edit_text(
                self.current_msg, html_text, reply_markup=self.reply_markup,
                parse_mode=constants.ParseMode.HTML,
                retry_on_retry_after=False))
            stop_task = asyncio.create_task(_stop_event.wait())
            done, _pending = await asyncio.wait({edit_task, stop_task},
                                                 return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done and _stop_event.is_set():
                # 用户停止：取消 edit，让流式 loop 回到主循环响应停止
                edit_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await edit_task
                return
            # edit 完成，取消 stop_task
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            # 重新 await edit_task 拿到异常（如果有）
            await edit_task
        except RetryAfter as e:
            # HTML edit 撞 429：设冷却期，不永久关闭 live_edit_enabled
            # 冷却期过后下个 chunk 会再试，限流可能已解除
            wait = _retry_after_seconds(e) + 0.1
            self._html_edit_cooldown_until = time.monotonic() + wait
            logger.warning(f"HTML edit 被 Telegram 限流 (retry_after={wait:.1f}s)，进入冷却期，期间跳过 UI 更新")
        except Exception as e:
            self.live_edit_enabled = False
            logger.warning(f"流式消息刷新失败，改为结束后一次性发送: {e}")

def _stream_chunk_idle_timeout_seconds() -> float:
    """流式请求两个分片之间的最长静默时间（秒）。

    用户把 AI 回复超时设为“不限”（0）时，底层 HTTP 读超时也是 None，提供商
    建连后不发任何分片就会永久阻塞在 asyncio.wait 上，而这段代码持有全局
    对话锁，结果是整个 bot 对所有消息只回“仍在处理”。

    流式与非流式的区别在于：已经产出的增量值得保留，所以限制的是单次等待
    而不是整轮总时长——持续出字的长回复不会被误伤。
    """
    configured = normalize_stream_timeout(UserDataManager.get('stream_timeout', 0))
    if configured <= 0:
        return STREAM_CHUNK_IDLE_TIMEOUT_SECONDS
    return configured + 30.0


async def send_streaming_response(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   prov_name: str, prov_data: Dict, model: str,
                                   system_prompt: str, history: List[Dict],
                                   extra_media_artifacts: Optional[List[Dict[str, Any]]] = None,
                                   stopped_partial_sink: Optional[List[str]] = None,
                                   token_text_sink: Optional[List[str]] = None) -> Optional[str]:
    """流式回复：上游边生成，Telegram 边按字符刷新显示。

    stopped_partial_sink：可选出参。用户中途手动停止时，把已经生成的部分文本
    append 进去（可能为空串），供调用方记录“当前回复已被用户手动停止”。
    token_text_sink：可选出参。token 用量文本 append 进去，供调用方在正文落库
    之后再落库，避免 token 记录 timestamp 早于正文导致刷新后顺序反序。"""
    global _stop_generation_event
    chat_id = update.effective_chat.id
    TELEGRAM_MSG_LIMIT = RICH_MESSAGE_CHAR_LIMIT

    msg = None
    renderer = None
    typing_stop = None
    typing_task = None
    stopped_by_user = False
    stop_notice_rendered = False
    raw_response_parts: List[str] = []
    native_media_detected = False
    media_detection_tail = ""
    usage_sink: List[Dict[str, int]] = []
    generation_started_at = time.monotonic()
    trace_id = make_trace_id("stream")

    stop_event = get_or_create_stop_event()
    logger.info(f"[停止诊断] 流式开始: stop_event id={id(stop_event)}")

    try:
        stop_kb = build_stop_keyboard()
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="流式输出中...",
            reply_markup=stop_kb
        )
        renderer = TelegramStreamRenderer(context, chat_id, msg, stop_kb, TELEGRAM_MSG_LIMIT, stop_event)
        renderer.start()
        typing_stop = asyncio.Event()
        typing_task = asyncio.create_task(
            keep_typing_while_waiting(
                context, chat_id, typing_stop,
                max_duration=TYPING_MAX_DURATION_SECONDS
            )
        )

        stream_iter = ModelClient.think_and_reply_stream(
            prov_name, get_next_api_key(prov_name, prov_data['api_key']), prov_data['base_url'],
            model, system_prompt, history,
            api_format=prov_data.get('api_format', 'openai'),
            usage_sink=usage_sink,
            trace_id=trace_id
        ).__aiter__()
        stop_task = asyncio.create_task(stop_event.wait())
        stream_timed_out = False
        try:
            while True:
                next_chunk_task = asyncio.create_task(stream_iter.__anext__())
                # 这个 wait 必须有超时：提供商建连后不发分片时 next_chunk_task 和
                # stop_task 都不会完成，没有 timeout 就永久阻塞，而此处持有全局
                # 对话锁，整个 bot 会对所有消息只回“仍在处理”。
                done, _pending = await asyncio.wait(
                    {next_chunk_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=_stream_chunk_idle_timeout_seconds()
                )
                # 诊断日志：判断 asyncio.wait 是否响应了 stop_task 完成
                _stop_set = stop_event.is_set()
                _stop_in_done = stop_task in done
                if _stop_set and not _stop_in_done and not done:
                    logger.warning(f"[停止诊断] BUG 信号: stop_event 已 set 但 asyncio.wait 超时未返回 stop_task, done 为空")
                elif _stop_set and not _stop_in_done:
                    logger.warning(f"[停止诊断] 异常: stop_event 已 set 但 stop_task 不在 done 里, done={[type(t).__name__ for t in done]}")
                else:
                    logger.info(f"[停止诊断] wait 返回: stop_set={_stop_set}, stop_in_done={_stop_in_done}, done_count={len(done)}")

                if not done:
                    stream_timed_out = True
                    await cancel_task_quietly(next_chunk_task, timeout=1.0)
                    break

                if stop_task in done and stop_event.is_set():
                    stopped_by_user = True
                    if renderer and not stop_notice_rendered:
                        await renderer.stop_and_keep_partial()
                        stop_notice_rendered = True
                    await cancel_task_quietly(next_chunk_task, timeout=1.0)
                    break

                try:
                    chunk = next_chunk_task.result()
                except StopAsyncIteration:
                    break

                raw_response_parts.append(chunk)
                if native_media_detected:
                    continue

                detection_window = (media_detection_tail + chunk).lower()
                if contains_inline_generated_media(detection_window):
                    native_media_detected = True
                    if renderer:
                        await renderer.cancel()
                        renderer.live_edit_enabled = False
                    try:
                        await safe_edit_text(msg, "🖼️ 检测到模型返回了媒体内容，正在保存文件...", reply_markup=None)
                    except Exception:
                        pass
                    continue

                await renderer.append(chunk)
                media_detection_tail = (media_detection_tail + chunk)[-64:]
        finally:
            await cancel_task_quietly(stop_task, timeout=0.2)
            aclose = getattr(stream_iter, 'aclose', None)
            if aclose is not None:
                close_task = asyncio.ensure_future(aclose())
                done, _pending = await asyncio.wait({close_task}, timeout=1.0)
                if close_task in done:
                    _drain_task_result(close_task)
                else:
                    await cancel_task_quietly(close_task, timeout=0.2)

        if stopped_by_user:
            if renderer and not stop_notice_rendered:
                await renderer.stop_and_keep_partial()
            write_model_trace("model_stopped", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": True,
                "partial_response": ''.join(raw_response_parts).strip(),
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            if stopped_partial_sink is not None:
                stopped_partial_sink.append(''.join(raw_response_parts).strip())
            return None

        if stream_timed_out:
            waited = int(_stream_chunk_idle_timeout_seconds())
            partial = ''.join(raw_response_parts).strip()
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": True,
                "error": "stream chunk idle timeout",
                "partial_response": partial,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            timeout_notice = (
                f"\n\n⏱️ 已超过 {waited} 秒没有收到新内容，本次流式回复被中断。"
                "上面是已经收到的部分；可以直接重发这条消息重试。"
            )
            if renderer and not native_media_detected:
                try:
                    await renderer.append(timeout_notice)
                except Exception as e:
                    logger.warning(f"流式超时提示追加失败: {e}")
                partial = (await renderer.finish()) or partial
            elif msg:
                try:
                    await safe_edit_text(msg, (partial + timeout_notice).strip(), reply_markup=None)
                except Exception as e:
                    logger.warning(f"流式超时提示发送失败: {e}")
            await send_token_usage_message(
                context, chat_id,
                usage_sink[0] if usage_sink else None,
                time.monotonic() - generation_started_at,
                token_text_sink=token_text_sink
            )
            return partial or timeout_notice.strip()

        if not native_media_detected and renderer and has_media_artifacts(extra_media_artifacts):
            notice_text = build_media_autosave_notice_text(
                extra_media_artifacts or [],
                ''.join(renderer.response_parts)
            )
            if notice_text:
                await renderer.append(f"\n\n{notice_text}")

        full_response = ''.join(raw_response_parts).strip() if native_media_detected else (await renderer.finish() if renderer else "")
        if stop_event.is_set():
            if renderer:
                await renderer.stop_and_keep_partial()
            write_model_trace("model_stopped", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": True,
                "partial_response": full_response,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            if stopped_partial_sink is not None:
                stopped_partial_sink.append(full_response)
            return None

        if full_response:
            media_artifacts: List[Dict[str, Any]] = []
            if native_media_detected:
                full_response, media_artifacts = extract_inline_generated_media(full_response)
            full_response = append_external_media_notices_to_response(full_response, extra_media_artifacts)
            write_model_trace("model_response", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": True,
                "response": full_response,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            if media_artifacts:
                try:
                    await send_generated_media_artifacts(context, chat_id, media_artifacts, caption=full_response)
                    if msg:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
                    await send_token_usage_message(
                        context, chat_id,
                        usage_sink[0] if usage_sink else None,
                        time.monotonic() - generation_started_at,
                        token_text_sink=token_text_sink
                    )
                    return full_response
                except Exception as e:
                    logger.warning(f"发送模型原生媒体失败: {e}")
            if renderer and not renderer.live_edit_enabled and msg:
                try:
                    await rich_finalize_text_response(context, chat_id, msg, full_response, TELEGRAM_MSG_LIMIT)
                except Exception as e:
                    logger.warning(f"流式降级后的最终消息发送失败: {e}")
            await send_token_usage_message(
                context, chat_id,
                usage_sink[0] if usage_sink else None,
                time.monotonic() - generation_started_at,
                token_text_sink=token_text_sink
            )
            return full_response

        empty_text = "模型未返回有效内容。"
        try:
            await safe_edit_text(msg, empty_text, reply_markup=None)
        except Exception as e:
            logger.warning(f"空回复提示发送失败: {e}")
            await context.bot.send_message(chat_id=chat_id, text=empty_text)
        return empty_text

    except Exception as e:
        logger.error(f"流式响应错误: {e}")
        error_text = format_provider_exception(e)
        partial = ''.join(raw_response_parts).strip()
        write_model_trace("model_error", {
            "trace_id": trace_id,
            "provider": prov_name,
            "provider_format": prov_data.get('api_format', 'openai'),
            "model": model,
            "stream": True,
            "error": error_text,
            "partial_response": partial,
            "usage": usage_sink[0] if usage_sink else None,
            "elapsed_seconds": time.monotonic() - generation_started_at,
        })
        if renderer:
            await renderer.cancel()
        # 异常退出兜底：保证占位消息"流式输出中..."被替换。
        # 优先发"已生成内容 + 错误提示"，让用户拿到 AI 已吐出的部分；
        # 任何路径失败都退到 context.bot.send_message 强制发一条新消息，
        # 绝不让 UI 卡在"流式输出中..."。
        fallback_text = (partial + "\n\n" + error_text).strip() if partial else error_text
        target_msg = renderer.current_msg if renderer else msg
        try:
            if target_msg:
                try:
                    await rich_finalize_text_response(context, chat_id, target_msg, fallback_text, TELEGRAM_MSG_LIMIT)
                except Exception as e1:
                    logger.warning(f"Rich 兜底发送失败，降级为 HTML edit: {e1}")
                    await safe_edit_text(target_msg, fallback_text[:4000], reply_markup=None,
                                         parse_mode=constants.ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=chat_id, text=fallback_text[:4000])
        except Exception as edit_err:
            logger.warning(f"兜底 edit 失败，最后退到 send_message: {edit_err}")
            try:
                await context.bot.send_message(chat_id=chat_id, text=fallback_text[:4000])
            except Exception as last_err:
                logger.error(f"连 send_message 都失败了，UI 可能卡住: {last_err}")
        return error_text
    finally:
        if typing_stop:
            typing_stop.set()
        if typing_task:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass


async def send_background_streaming_response(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                              prov_name: str, prov_data: Dict, model: str,
                                              system_prompt: str, history: List[Dict],
                                              extra_media_artifacts: Optional[List[Dict[str, Any]]] = None,
                                              stopped_partial_sink: Optional[List[str]] = None,
                                              token_text_sink: Optional[List[str]] = None) -> Optional[str]:
    """后台流式：底层用流式 API 拿 token，但不实时推送到 Telegram。

    只累积到字符串里，每隔几秒把占位消息更新为"已生成 N 字"（走 safe_edit_text
    兜底限流）。流式结束（或被停止/超时/异常）后，一次性发送完整回复。

    优点：完全避开 Rich Draft 实时推送的 429 限流问题；仍享受流式 API 的特性
    （更早开始返回、可中途停止、某些模型只支持流式）。
    签名与 send_streaming_response 完全一致，便于 messages.py 互换。
    """
    chat_id = update.effective_chat.id
    TELEGRAM_MSG_LIMIT = RICH_MESSAGE_CHAR_LIMIT

    msg = None
    typing_stop = None
    typing_task = None
    stopped_by_user = False
    stream_timed_out = False
    raw_response_parts: List[str] = []
    native_media_detected = False
    usage_sink: List[Dict[str, int]] = []
    generation_started_at = time.monotonic()
    trace_id = make_trace_id("bgstream")

    stop_event = get_or_create_stop_event()
    logger.info(f"[停止诊断] 后台流式开始: stop_event id={id(stop_event)}")

    # 进度更新周期（秒）。不实时推 = 不撞 draft 限流；周期性 edit 走 safe_edit_text
    # 自带 RetryAfter 兜底，即使被限流也只会暂停几秒，不会崩。
    progress_interval = 3.0
    last_progress_at = generation_started_at

    try:
        stop_kb = build_stop_keyboard()
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="后台流式输出中...",
            reply_markup=stop_kb
        )
        typing_stop = asyncio.Event()
        typing_task = asyncio.create_task(
            keep_typing_while_waiting(
                context, chat_id, typing_stop,
                max_duration=TYPING_MAX_DURATION_SECONDS
            )
        )

        stream_iter = ModelClient.think_and_reply_stream(
            prov_name, get_next_api_key(prov_name, prov_data['api_key']), prov_data['base_url'],
            model, system_prompt, history,
            api_format=prov_data.get('api_format', 'openai'),
            usage_sink=usage_sink,
            trace_id=trace_id
        ).__aiter__()
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            while True:
                next_chunk_task = asyncio.create_task(stream_iter.__anext__())
                # 复用流式的 idle 超时：两个分片间最长静默时间。
                done, _pending = await asyncio.wait(
                    {next_chunk_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=_stream_chunk_idle_timeout_seconds()
                )

                if not done:
                    stream_timed_out = True
                    await cancel_task_quietly(next_chunk_task, timeout=1.0)
                    break

                if stop_task in done and stop_event.is_set():
                    stopped_by_user = True
                    await cancel_task_quietly(next_chunk_task, timeout=1.0)
                    break

                try:
                    chunk = next_chunk_task.result()
                except StopAsyncIteration:
                    break

                raw_response_parts.append(chunk)

                # 周期性进度更新。safe_edit_text 内部对 RetryAfter 会 sleep+重试，
                # 即使被限流也只会让进度暂停几秒，不影响主流程。
                now = time.monotonic()
                if now - last_progress_at >= progress_interval:
                    last_progress_at = now
                    partial_len = len(''.join(raw_response_parts))
                    try:
                        await safe_edit_text(
                            msg,
                            f"后台流式输出中... 已生成 {partial_len} 字",
                            reply_markup=stop_kb
                        )
                    except Exception as e:
                        logger.debug(f"进度更新失败（可忽略）: {e}")
        finally:
            await cancel_task_quietly(stop_task, timeout=0.2)
            aclose = getattr(stream_iter, 'aclose', None)
            if aclose is not None:
                close_task = asyncio.ensure_future(aclose())
                done, _pending = await asyncio.wait({close_task}, timeout=1.0)
                if close_task in done:
                    _drain_task_result(close_task)
                else:
                    await cancel_task_quietly(close_task, timeout=0.2)

        partial = ''.join(raw_response_parts).strip()

        if stopped_by_user:
            stop_text = (partial + "\n\n⏹️ 已停止，保留以上已生成内容。").strip() if partial else "⏹️ 已停止，还没有生成可保留的内容。"
            write_model_trace("model_stopped", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": True,
                "partial_response": partial,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            try:
                await rich_finalize_text_response(context, chat_id, msg, stop_text, TELEGRAM_MSG_LIMIT)
            except Exception as e:
                logger.warning(f"后台流式停止消息发送失败: {e}")
                try:
                    await safe_edit_text(msg, stop_text[:4000], reply_markup=None,
                                         parse_mode=constants.ParseMode.HTML)
                except Exception as e2:
                    logger.warning(f"safe_edit_text 也失败: {e2}")
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=stop_text[:4000])
                    except Exception:
                        pass
            if stopped_partial_sink is not None:
                stopped_partial_sink.append(partial)
            return None

        if stream_timed_out:
            waited = int(_stream_chunk_idle_timeout_seconds())
            timeout_notice = (
                f"\n\n⏱️ 已超过 {waited} 秒没有收到新内容，本次流式回复被中断。"
                "上面是已经收到的部分；可以直接重发这条消息重试。"
            )
            timeout_text = (partial + timeout_notice).strip() if partial else timeout_notice.strip()
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": True,
                "error": "stream chunk idle timeout",
                "partial_response": partial,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            try:
                await rich_finalize_text_response(context, chat_id, msg, timeout_text, TELEGRAM_MSG_LIMIT)
            except Exception as e:
                logger.warning(f"后台流式超时消息发送失败: {e}")
                try:
                    await safe_edit_text(msg, timeout_text[:4000], reply_markup=None,
                                         parse_mode=constants.ParseMode.HTML)
                except Exception as e2:
                    logger.warning(f"safe_edit_text 也失败: {e2}")
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=timeout_text[:4000])
                    except Exception:
                        pass
            await send_token_usage_message(
                context, chat_id,
                usage_sink[0] if usage_sink else None,
                time.monotonic() - generation_started_at,
                token_text_sink=token_text_sink
            )
            return partial or timeout_notice.strip()

        # 正常完成
        if partial:
            media_artifacts: List[Dict[str, Any]] = []
            if contains_inline_generated_media(partial.lower()):
                native_media_detected = True
                partial, media_artifacts = extract_inline_generated_media(partial)
            partial = append_external_media_notices_to_response(partial, extra_media_artifacts)
            write_model_trace("model_response", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": True,
                "response": partial,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            if media_artifacts:
                try:
                    await send_generated_media_artifacts(context, chat_id, media_artifacts, caption=partial)
                    if msg:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
                    await send_token_usage_message(
                        context, chat_id,
                        usage_sink[0] if usage_sink else None,
                        time.monotonic() - generation_started_at,
                        token_text_sink=token_text_sink
                    )
                    return partial
                except Exception as e:
                    logger.warning(f"发送模型原生媒体失败: {e}")
            try:
                await rich_finalize_text_response(context, chat_id, msg, partial, TELEGRAM_MSG_LIMIT)
            except Exception as e:
                logger.warning(f"后台流式最终消息发送失败: {e}")
                try:
                    await safe_edit_text(msg, partial[:4000], reply_markup=None,
                                         parse_mode=constants.ParseMode.HTML)
                except Exception as e2:
                    logger.warning(f"safe_edit_text 也失败: {e2}")
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=partial[:4000])
                    except Exception:
                        pass
            await send_token_usage_message(
                context, chat_id,
                usage_sink[0] if usage_sink else None,
                time.monotonic() - generation_started_at,
                token_text_sink=token_text_sink
            )
            return partial

        empty_text = "模型未返回有效内容。"
        try:
            await safe_edit_text(msg, empty_text, reply_markup=None)
        except Exception as e:
            logger.warning(f"空回复提示发送失败: {e}")
            await context.bot.send_message(chat_id=chat_id, text=empty_text)
        return empty_text

    except Exception as e:
        logger.error(f"后台流式响应错误: {e}")
        error_text = format_provider_exception(e)
        partial = ''.join(raw_response_parts).strip()
        write_model_trace("model_error", {
            "trace_id": trace_id,
            "provider": prov_name,
            "provider_format": prov_data.get('api_format', 'openai'),
            "model": model,
            "stream": True,
            "error": error_text,
            "partial_response": partial,
            "usage": usage_sink[0] if usage_sink else None,
            "elapsed_seconds": time.monotonic() - generation_started_at,
        })
        fallback_text = (partial + "\n\n" + error_text).strip() if partial else error_text
        try:
            if msg:
                try:
                    await rich_finalize_text_response(context, chat_id, msg, fallback_text, TELEGRAM_MSG_LIMIT)
                except Exception as e1:
                    logger.warning(f"Rich 兜底发送失败，降级为 HTML edit: {e1}")
                    await safe_edit_text(msg, fallback_text[:4000], reply_markup=None,
                                         parse_mode=constants.ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=chat_id, text=fallback_text[:4000])
        except Exception as edit_err:
            logger.warning(f"兜底 edit 失败，最后退到 send_message: {edit_err}")
            try:
                await context.bot.send_message(chat_id=chat_id, text=fallback_text[:4000])
            except Exception as last_err:
                logger.error(f"连 send_message 都失败了，UI 可能卡住: {last_err}")
        return error_text
    finally:
        if typing_stop:
            typing_stop.set()
        if typing_task:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass


def _nonstream_hard_timeout_seconds() -> float:
    """非流式请求的兜底上限（秒）。

    用户把 AI 回复超时设为“不限”（0）时，底层 HTTP 也不会超时，一旦连接
    静默挂起就再也不会返回，界面永远停在“非流式输出中...”。非流式没有
    增量输出可以保留，所以这里给一个足够宽松的硬上限兜底。

    用户设了具体秒数时，在其基础上留余量，让底层 HTTP 超时先触发，
    保留原有的错误信息。
    """
    configured = normalize_stream_timeout(UserDataManager.get('stream_timeout', 0))
    if configured <= 0:
        return NONSTREAM_FALLBACK_TIMEOUT_SECONDS
    return configured + 30.0


async def send_non_streaming_response(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       prov_name: str, prov_data: Dict, model: str,
                                       system_prompt: str, history: List[Dict],
                                       extra_media_artifacts: Optional[List[Dict[str, Any]]] = None,
                                       stopped_partial_sink: Optional[List[str]] = None,
                                       token_text_sink: Optional[List[str]] = None) -> Optional[str]:
    """非流式回复：等待完整回复后一次性发送。

    stopped_partial_sink：可选出参，语义同流式版本。
    token_text_sink：可选出参，语义同流式版本。"""
    global _stop_generation_event
    chat_id = update.effective_chat.id
    TELEGRAM_MSG_LIMIT = RICH_MESSAGE_CHAR_LIMIT

    msg = None
    typing_stop = None
    typing_task = None
    stop_event = get_or_create_stop_event()
    usage_sink: List[Dict[str, int]] = []
    generation_started_at = time.monotonic()
    trace_id = make_trace_id("nonstream")
    logger.info(f"[停止诊断] 非流式开始: stop_event id={id(stop_event)}")

    try:
        stop_kb = build_stop_keyboard()
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="非流式输出中...",
            reply_markup=stop_kb
        )
        typing_stop = asyncio.Event()
        typing_task = asyncio.create_task(
            keep_typing_while_waiting(
                context, chat_id, typing_stop,
                max_duration=TYPING_MAX_DURATION_SECONDS
            )
        )

        response_task = asyncio.create_task(ModelClient.think_and_reply(
            prov_name, get_next_api_key(prov_name, prov_data['api_key']), prov_data['base_url'],
            model, system_prompt, history,
            api_format=prov_data.get('api_format', 'openai'),
            usage_sink=usage_sink,
            trace_id=trace_id
        ))
        stop_task = asyncio.create_task(stop_event.wait())
        # 兜底硬上限必须加在这个 wait 上：模型无响应时 response_task 和
        # stop_task 都不会完成，没有 timeout 就会永久阻塞在这里，界面
        # 永远停在“非流式输出中...”。
        done, pending = await asyncio.wait(
            {response_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=_nonstream_hard_timeout_seconds(),
        )
        # 诊断日志：判断 asyncio.wait 是否响应了 stop_task 完成
        _stop_set = stop_event.is_set()
        _stop_in_done = stop_task in done
        if _stop_set and not _stop_in_done and not done:
            logger.warning(f"[停止诊断] 非流式 BUG 信号: stop_event 已 set 但 asyncio.wait 超时未返回 stop_task, done 为空")
        elif _stop_set and not _stop_in_done:
            logger.warning(f"[停止诊断] 非流式 异常: stop_event 已 set 但 stop_task 不在 done 里, done={[type(t).__name__ for t in done]}")
        else:
            logger.info(f"[停止诊断] 非流式 wait 返回: stop_set={_stop_set}, stop_in_done={_stop_in_done}, done_count={len(done)}")
        if not done:
            await cancel_task_quietly(response_task, timeout=1.0)
            await cancel_task_quietly(stop_task, timeout=0.2)
            waited = int(time.monotonic() - generation_started_at)
            timeout_text = (
                f"⏱️ 等待模型回复超过 {waited} 秒仍无响应，已中断本次请求。\n"
                "可以直接重发这条消息；如果经常发生，可在"
                "「更多设置 → 超时」调整 AI 回复超时，或换一个提供商试试。"
            )
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": False,
                "error": "non-stream hard timeout",
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            try:
                await safe_edit_text(msg, timeout_text, reply_markup=None)
            except Exception:
                pass
            return timeout_text
        if stop_task in done and stop_event.is_set():
            try:
                await safe_edit_text(msg, "⏹️ 已停止。非流式请求已取消，未产生可保留的增量内容。", reply_markup=None)
            except Exception:
                pass
            await cancel_task_quietly(response_task, timeout=1.0)
            write_model_trace("model_stopped", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": False,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            if stopped_partial_sink is not None:
                stopped_partial_sink.append("")
            return None

        await cancel_task_quietly(stop_task, timeout=0.2)
        response, error = await response_task
        logger.info(
            f"Non-stream result ready: provider={prov_name}, model={model}, "
            f"api_format={prov_data.get('api_format', 'openai')}, "
            f"response_len={len(response or '')}, error={bool(error)}"
        )

        # 检查用户是否在等待期间停止了
        if stop_event.is_set():
            try:
                await safe_edit_text(msg, "⏹️ 已停止。", reply_markup=None)
            except Exception:
                pass
            if stopped_partial_sink is not None:
                stopped_partial_sink.append(response or "")
            return None

        if error:
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": False,
                "error": error,
                "response": response,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            try:
                await rich_finalize_text_response(context, chat_id, msg, error, TELEGRAM_MSG_LIMIT)
            except Exception:
                pass
            return error

        if response:
            media_artifacts: List[Dict[str, Any]] = []
            if contains_inline_generated_media(response):
                response, media_artifacts = extract_inline_generated_media(response)
            response = append_external_media_notices_to_response(response, extra_media_artifacts)
            write_model_trace("model_response", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": prov_data.get('api_format', 'openai'),
                "model": model,
                "stream": False,
                "response": response,
                "usage": usage_sink[0] if usage_sink else None,
                "elapsed_seconds": time.monotonic() - generation_started_at,
            })
            if media_artifacts:
                try:
                    await send_generated_media_artifacts(context, chat_id, media_artifacts, caption=response)
                    if msg:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
                    await send_token_usage_message(
                        context, chat_id,
                        usage_sink[0] if usage_sink else None,
                        time.monotonic() - generation_started_at,
                        token_text_sink=token_text_sink
                    )
                    return response
                except Exception as e:
                    logger.warning(f"发送模型原生媒体失败: {e}")
            try:
                await rich_finalize_text_response(context, chat_id, msg, response, TELEGRAM_MSG_LIMIT)
            except Exception as e:
                logger.warning(f"非流式最终消息更新失败: {e}")
            await send_token_usage_message(
                context, chat_id,
                usage_sink[0] if usage_sink else None,
                time.monotonic() - generation_started_at,
                token_text_sink=token_text_sink
            )
            return response

        empty_text = "模型未返回有效内容。"
        try:
            await safe_edit_text(msg, empty_text, reply_markup=None)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=empty_text)
        return empty_text

    except Exception as e:
        logger.error(f"非流式响应错误: {e}")
        error_text = format_provider_exception(e)
        write_model_trace("model_error", {
            "trace_id": trace_id,
            "provider": prov_name,
            "provider_format": prov_data.get('api_format', 'openai'),
            "model": model,
            "stream": False,
            "error": error_text,
            "usage": usage_sink[0] if usage_sink else None,
            "elapsed_seconds": time.monotonic() - generation_started_at,
        })
        try:
            if msg:
                await rich_finalize_text_response(context, chat_id, msg, error_text, TELEGRAM_MSG_LIMIT)
            else:
                await context.bot.send_message(chat_id=chat_id, text=error_text)
        except Exception:
            pass
        return error_text
    finally:
        if typing_stop:
            typing_stop.set()
        if typing_task:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

# --- ☆ 命令处理 ☆ ---
