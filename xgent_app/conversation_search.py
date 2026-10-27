"""Read-only, literal global search over saved conversations and visible messages.

No model context, attachment bytes, credentials or transient UI menus are indexed.
Use a separate SQLite reader so a cancelled/slow search never holds the bot writer.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

from .ui_history import hide_audit_record


class SearchError(ValueError):
    pass


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag in {'br', 'p', 'div', 'pre', 'blockquote', 'li', 'tr'}:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {'p', 'div', 'pre', 'blockquote', 'li', 'tr'}:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


_DATA_URI = re.compile(r'data:[\w.+/-]+;base64,[A-Za-z0-9+/=]+', re.IGNORECASE)


def visible_text(kind, role, content, raw_metadata):
    """Search displayed text/filenames, never JSON metadata or encoded payloads."""
    try:
        meta = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    except (ValueError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    if (hide_audit_record({'msg_type': kind, 'content': content, 'metadata': meta})
            or meta.get('attachment_purpose') == 'configuration'):
        return ''
    text = str(content or '')
    if kind in {'user_file', 'user_photo'} and text.startswith(('[提供商配置文件]', '[黑名单文件]', '[记忆文件]')):
        return ''
    display = meta.get('display')
    html = False
    if isinstance(display, dict) and isinstance(display.get('content'), str):
        text = display['content']
        html = display.get('parse_mode') == 'HTML'
    presentation = meta.get('web_file_message')
    if role == 'user' and isinstance(presentation, dict) and isinstance(presentation.get('text'), str):
        text, html = presentation['text'], False
    if html or role != 'user' and re.search(r'</?(?:b|i|pre|code|blockquote|a)(?:\s|>)', text, re.IGNORECASE):
        parser = _PlainText()
        parser.feed(text)
        text = ''.join(parser.parts)
    names = []
    for key in ('attachments', 'server_uploads', 'display_media'):
        refs = meta.get(key)
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, dict):
                    names.extend(str(ref.get(field) or '') for field in ('name', 'filename', 'caption', 'path'))
    return ' '.join(_DATA_URI.sub('[文件数据]', text + ' ' + ' '.join(names)).split())


def _excerpt(text, query):
    folded = text.casefold()
    at = folded.find(query.casefold())
    # casefold can expand characters (Straße -> strasse); keep original offsets.
    if len(folded) != len(text):
        folded_end, position, original_start = at + len(query.casefold()), 0, None
        for index, char in enumerate(text):
            next_position = position + len(char.casefold())
            if original_start is None and next_position > at:
                original_start = index
            if next_position >= folded_end:
                at, end = original_start, index + 1
                break
            position = next_position
    else:
        end = at + len(query.casefold())
    start, stop = max(0, at - 55), min(len(text), max(end + 100, at + 150))
    return ('…' if start else '') + text[start:stop] + ('…' if stop < len(text) else ''), text[at:end]


def _options(data):
    query = str(data.get('q') or '').strip()
    if len(query) > 200:
        raise SearchError('搜索关键词最多 200 个字符，请缩短后重试。')
    archive, kind = str(data.get('archive') or 'all'), str(data.get('kind') or 'all')
    if archive not in {'all', 'active', 'archived'} or kind not in {'all', 'title', 'user', 'assistant', 'tool'}:
        raise SearchError('搜索筛选条件无效。')
    try:
        limit = min(50, max(1, int(data.get('limit') or 30)))
    except (TypeError, ValueError, OverflowError):
        raise SearchError('搜索分页大小无效。') from None
    fingerprint = hashlib.sha256(json.dumps([query, archive, kind], ensure_ascii=False).encode()).hexdigest()
    boundary, snapshot = None, None
    if data.get('cursor'):
        try:
            if len(str(data['cursor'])) > 4096:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(str(data['cursor'])))
            version, signature, snapshot, boundary = value
            if (version != 1 or signature != fingerprint or type(snapshot) is not int or snapshot < 0
                    or not isinstance(boundary, list) or len(boundary) != 3
                    or boundary[0] not in (0, 1) or type(boundary[1]) not in (int, float)
                    or not math.isfinite(boundary[1])
                    or not isinstance(boundary[2], str)):
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError):
            raise SearchError('搜索游标无效或关键词已改变，请重新搜索。') from None
    return query, archive, kind, limit, fingerprint, snapshot, boundary


def _read(path, options, cancelled):
    query, archive, kind, limit, fingerprint, snapshot, boundary = options
    deadline = time.monotonic() + 20
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    try:
        conn.row_factory = sqlite3.Row
        conn.set_progress_handler(lambda: int(cancelled.is_set() or time.monotonic() > deadline), 2000)
        conn.create_function('search_text', 4, visible_text, deterministic=True)
        conn.create_function('casefold', 1, lambda text: str(text or '').casefold(), deterministic=True)
        conn.execute('BEGIN')
        message_columns = {row[1] for row in conn.execute('PRAGMA table_info(global_messages)')}
        active_messages = ' AND m.withdrawn=0' if 'withdrawn' in message_columns else ''
        if snapshot is None:
            snapshot = conn.execute('SELECT COALESCE(MAX(id),0) FROM global_messages').fetchone()[0]
        scope = 's.deleting=0'
        if archive != 'all':
            scope += ' AND s.archived=' + ('1' if archive == 'archived' else '0')
        candidates, params = [], []
        if kind in {'all', 'title'}:
            candidates.append(f"""SELECT 0 rank,s.last_active timestamp,s.id result_key,s.id conversation_id,
                s.name conversation_name,s.archived,'title' kind,NULL message_key,'title' role,s.name text
                FROM chat_sessions s WHERE {scope}""")
        if kind != 'title':
            role = ''
            if kind == 'tool':
                role = " AND m.msg_type='agent_result'"
            elif kind in {'user', 'assistant'}:
                role = " AND m.role=? AND m.msg_type!='agent_result'"
                params.append(kind)
            candidates.append(f"""SELECT 1 rank,m.timestamp,printf('%020d',m.id) result_key,s.id conversation_id,
                s.name conversation_name,s.archived,'message' kind,'history:'||m.id message_key,
                CASE WHEN m.msg_type='agent_result' THEN 'tool' ELSE m.role END role,
                search_text(m.msg_type,m.role,m.content,m.metadata) text
                FROM global_messages m JOIN chat_sessions s ON m.session_id=s.id
                WHERE {scope}{active_messages} AND m.id<={snapshot} {role}
                AND m.msg_type IN ('user_text','ai_reply','user_file','user_photo','media_reply','agent_result','runtime_error')""")
        where = 'instr(casefold(text),?)>0'
        params.append(query.casefold())
        if boundary is not None:
            rank, timestamp, key = boundary
            where += ' AND (rank>? OR (rank=? AND (timestamp<? OR (timestamp=? AND result_key<?))))'
            params.extend([rank, rank, timestamp, timestamp, key])
        sql = 'WITH candidates AS (' + ' UNION ALL '.join(candidates) + ') SELECT * FROM candidates WHERE ' + where
        rows = conn.execute(sql + ' ORDER BY rank,timestamp DESC,result_key DESC LIMIT ?', [*params, limit + 1]).fetchall()
        items = []
        for row in rows[:limit]:
            snippet, match = _excerpt(row['text'], query)
            items.append({key: row[key] for key in ('conversation_id', 'conversation_name', 'archived', 'kind', 'message_key', 'role', 'timestamp')}
                         | {'snippet': snippet, 'match': match})
        cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            cursor = base64.urlsafe_b64encode(json.dumps([1, fingerprint, snapshot,
                [last['rank'], last['timestamp'], last['result_key']]], separators=(',', ':')).encode()).decode()
        return {'items': items, 'next_cursor': cursor, 'query': query}
    except sqlite3.OperationalError as exc:
        if 'interrupt' in str(exc):
            raise SearchError('搜索耗时较长，请缩短关键词或筛选消息类型后重试。') from None
        raise SearchError('暂时无法读取搜索数据，请稍后重试。') from exc
    finally:
        conn.close()


async def global_search(conn, data):
    options = _options(data)
    if not options[0]:
        return {'items': [], 'next_cursor': None, 'query': ''}
    cursor = await conn.execute('PRAGMA database_list')
    databases = await cursor.fetchall()
    await cursor.close()
    path = next(row[2] for row in databases if row[1] == 'main')
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(_read, path, options, cancelled)
    finally:
        cancelled.set()
