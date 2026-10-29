"""Separate endpoint-specific form input prompts from shared conversation text."""
from __future__ import annotations

import json
import re

_INPUT_END = '（直接发一条消息即可；发 cancel 取消录入）'
_FILLED = '✅ 已填入，回到表单继续。'
_SECRET = re.compile(r'^🔒 已录入变量 \$[A-Z_][A-Z0-9_]*，内容只进了环境变量，没有发给 AI。回到表单继续。$')


def ask_input_notice(text):
    """Recognize the exact transport prompts emitted by older versions."""
    text = str(text or '').strip()
    return (text.startswith('请填写：') and text.endswith('\n'+_INPUT_END)
            or text == _FILLED or bool(_SECRET.fullmatch(text)))


def local_ask_notice():
    from .presentation import current_message_presentation
    return (current_message_presentation() or {}).get('kind') == 'ask_input_notice'


async def send_ask_input_notice(message, text):
    """Keep Bot/CLI instructions on their input endpoint; Web has native fields."""
    from .presentation import message_presentation
    with message_presentation({'kind': 'ask_input_notice', 'mode': 'folded'}):
        return await message.reply_text(text)


def _text(record):
    if 'content' in record:
        return str(record.get('content') or '')
    try:
        payload = record.get('payload') or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return str(payload.get('content') or '')
    except (TypeError, ValueError):
        return ''


def mark_legacy_ask_inputs(records):
    """Mark old field values only when an input prompt/ack encloses them.

    Mark display copies, not durable rows. A bare word like “测试” elsewhere in
    the conversation stays visible even when a form used the same word.
    """
    pending = {}
    ordered = sorted(records, key=lambda row: (row.get('timestamp') or 0,
                    1 if row.get('ui_message_id') or row.get('_source') else 0,
                    str(row.get('id') or row.get('ui_message_id') or '')))
    for record in ordered:
        cid = record.get('session_id') or record.get('conversation_id') or ''
        text = _text(record)
        if text.startswith('请填写：') and ask_input_notice(text):
            pending[cid] = {'prompt_at': record.get('timestamp') or 0, 'candidate': None}
        elif text == _FILLED:
            field = pending.pop(cid, None)
            candidate = field and field.get('candidate')
            if candidate is not None:
                candidate['_ask_field_input'] = True
        elif ask_input_notice(text):
            pending.pop(cid, None)
        elif cid in pending:
            field = pending[cid]
            if (record.get('msg_type') == 'user_text' and record.get('role') == 'user'
                    and field['candidate'] is None and
                    0 <= (record.get('timestamp') or 0)-field['prompt_at'] <= 1800):
                field['candidate'] = record
            else:
                pending.pop(cid, None)
    return records


async def mark_legacy_ask_inputs_from_database(conn, records):
    """Recognize a field value even when its old prompts lie on another page."""
    targets = {row['id']: row for row in records if row.get('msg_type')=='user_text'
               and row.get('role')=='user' and type(row.get('id')) is int}
    if not targets:
        return records
    placeholders = ','.join('?' for _ in targets)
    cursor = await conn.execute(f'''SELECT m.id,m.timestamp,
        (SELECT u.payload FROM ui_messages u WHERE u.conversation_id=m.session_id
         AND u.withdrawn=0 AND u.timestamp<=m.timestamp ORDER BY u.timestamp DESC LIMIT 1) AS prompt,
        (SELECT u.timestamp FROM ui_messages u WHERE u.conversation_id=m.session_id
         AND u.withdrawn=0 AND u.timestamp<=m.timestamp ORDER BY u.timestamp DESC LIMIT 1) AS prompt_at,
        (SELECT u.payload FROM ui_messages u WHERE u.conversation_id=m.session_id
         AND u.withdrawn=0 AND u.timestamp>=m.timestamp ORDER BY u.timestamp LIMIT 1) AS receipt
        FROM global_messages m WHERE m.id IN ({placeholders})''', list(targets))
    try:
        for match in await cursor.fetchall():
            prompt = _text({'payload': match['prompt']})
            receipt = _text({'payload': match['receipt']})
            if (prompt.startswith('请填写：') and ask_input_notice(prompt) and receipt==_FILLED
                    and match['prompt_at'] is not None and 0<=match['timestamp']-match['prompt_at']<=1800):
                targets[match['id']]['_ask_field_input'] = True
    finally:
        await cursor.close()
    return records
