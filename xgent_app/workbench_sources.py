"""Read-only provenance for workbench resources. Never rewrites conversation content."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .ui_history import hide_audit_record, ui_record


def source_info(owner, key=None, *, note=None):
    return {'conversation_id': owner['id'], 'conversation_name': owner.get('name') or '新对话',
            'archived': bool(owner.get('archived')), 'message_key': key,
            'note': note if note else (None if key else '此记录没有关联的对话消息')}


def output_key(value, root):
    """Normalize a verified output reference across stored Windows/POSIX display paths."""
    text = html.unescape(str(value or '')).replace('\\','/').strip()
    marker = Path(root).name + '/'
    if marker in text:
        text = text.rsplit(marker, 1)[1]
    else:
        return None
    parts = text.split('/')
    if len(parts) != 2 or any(p in ('', '.', '..') for p in parts) or not parts[-1].endswith('.txt'):
        return None
    return '/'.join(parts)


async def owners(conn):
    cursor = await conn.execute('SELECT id,name,archived FROM chat_sessions WHERE deleting=0')
    result = {row['id']: dict(row) for row in await cursor.fetchall()}
    await cursor.close()
    return result


async def reference_anchor(conn, conversation_id, reference):
    """Only an exact persisted identifier is a message anchor; no timestamp guessing."""
    if not reference:
        return None
    token = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(str(reference)) + r'(?![A-Za-z0-9_])')
    cursor = await conn.execute("SELECT * FROM global_messages WHERE session_id=? AND msg_type NOT IN ('agent_cmd','command','button_click','management_input') AND (instr(content,?)>0 OR instr(COALESCE(metadata,''),?)>0) ORDER BY timestamp,id", (conversation_id, reference, reference))
    try:
        async for row in cursor:
            record=dict(row)
            if not hide_audit_record(record) and token.search(record['content'] or ''):
                return 'history:' + str(record['id'])
    finally:
        await cursor.close()
    cursor = await conn.execute('SELECT * FROM ui_messages WHERE conversation_id=? AND instr(payload,?)>0 ORDER BY timestamp,ui_message_id', (conversation_id, reference))
    try:
        async for row in cursor:
            record=dict(row)
            message=ui_record(record,active_generation=record['generation'])
            if not message.get('deleted') and token.search(str(message.get('content') or message.get('text') or '')):
                return 'ui:' + record['ui_message_id']
    finally:
        await cursor.close()
    return None


async def output_sources(conn, root, owner_map):
    """Read persisted output references and task runs; unrelated file paths are ignored."""
    pattern = re.compile(re.escape(Path(root).name) + r'[/\\]([^\s<>"`|]+?\.txt)')
    result = {}
    cursor = await conn.execute("SELECT id,session_id,content,metadata,msg_type,timestamp FROM global_messages WHERE instr(content,?)>0 OR instr(COALESCE(metadata,''),?)>0 ORDER BY timestamp,id", (Path(root).name, Path(root).name))
    try:
        async for row in cursor:
            record=dict(row);owner=owner_map.get(row['session_id'])
            if not owner or hide_audit_record(record) or row['msg_type'] in ('agent_cmd','user_text','command','button_click','management_input'):
                continue
            # Decode JSON before matching so escaped slashes/unicode retain their actual value.
            try: metadata=json.loads(row['metadata'] or '{}')
            except (ValueError,TypeError): metadata={}
            text=html.unescape((row['content'] or '')+'\n'+json.dumps(metadata,ensure_ascii=False)).replace('\\\\','/')
            for found in pattern.finditer(text):
                key=output_key(Path(root).name+'/'+found[1],root)
                if key and key not in result:
                    result[key]={'source':source_info(owner,'history:'+str(row['id']))}
    finally:
        await cursor.close()
    cursor=await conn.execute('SELECT r.run_id,r.output_path,r.status,r.created_at,t.id task_id,t.summary,t.command,t.conversation_id FROM trigger_runs r JOIN trigger_tasks t ON t.id=r.task_id WHERE r.output_path IS NOT NULL ORDER BY r.created_at')
    try:
        async for row in cursor:
            owner=owner_map.get(row['conversation_id']);key=output_key(row['output_path'],root)
            if not owner or not key:continue
            entry=result.setdefault(key,{'source':source_info(owner)})
            if not entry['source']['message_key']:
                anchor=await reference_anchor(conn,row['conversation_id'],row['run_id'])
                if not anchor:anchor=await reference_anchor(conn,row['conversation_id'],row['task_id'])
                entry['source']=source_info(owner,anchor,note=None if anchor else '任务尚未在对话中留下结果消息')
            entry.update(task_id=row['task_id'],task_name=row['summary'] or row['task_id'],status=row['status'],command=row['command'],run_id=row['run_id'])
    finally:
        await cursor.close()
    return result
