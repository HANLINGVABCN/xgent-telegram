"""Authenticated workbench operations over attachment identities, never raw paths."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re

from .attachment_versions import AttachmentVersions, VersionError, attachment_identity, inspect_version
from .attachments import _metadata, _legacy_references, decode_full_text, AttachmentContextError
from .conversations import current_scope, get_conversations, ConversationChanged
from .web_history import build_history_message


@dataclass
class VersionDownload:
    content: bytes
    filename: str
    mime_type: str
    inline: bool = False


def locate_reference(record, identifier, upload_root):
    if record.get('role') != 'user' or record.get('msg_type') not in {'user_file', 'user_photo'}:
        raise VersionError('此记录不是用户上传附件')
    metadata = deepcopy(_metadata(record))
    if metadata.get('attachment_purpose') == 'configuration':
        raise VersionError('配置导入不建立附件版本')
    refs = metadata.get('attachments')
    if refs is None:
        refs = _legacy_references(record, upload_root)
        metadata['attachments'] = refs
    if not isinstance(refs, list):
        raise VersionError('附件关联损坏')
    for ref in refs:
        if not isinstance(ref, dict) or ref.get('storage', 'uploads') != 'uploads':
            continue
        identity, _ = attachment_identity(ref, upload_root)
        if identity == identifier:
            return ref, metadata
    raise VersionError('附件关联已不存在')


class AttachmentVersionAPI:
    def __init__(self, workbench):
        self.workbench = workbench
        self.ns = workbench.ns
        self.upload_root = self.ns['ArtifactManager'].UPLOAD_DIR

    async def target(self, data):
        match = re.fullmatch(r'([1-9]\d{0,18}):([0-9a-f]{64})', str(data.get('id') or ''))
        if not match or int(match[1]) >= 2**63:
            raise VersionError('无效附件关联标识')
        db = await self.workbench.db()
        record = await db.get_display_message(int(match[1]))
        if not record:
            raise VersionError('附件记录已不存在或不属于此对话')
        ref, metadata = locate_reference(record, match[2], self.upload_root)
        session = await db.get_session(current_scope().conversation_id)
        if not session or session['deleting']:
            raise ConversationChanged('会话已删除')
        return db, record, ref, metadata, session

    async def read(self, action, data):
        db, record, ref, metadata, session = await self.target(data)
        store = AttachmentVersions(self.upload_root, ref)
        if action == 'versions':
            result = await asyncio.to_thread(store.describe)
            # Long disk work may overlap a context reset, archive or deletion.
            # Re-authorize before publishing a capability or downloaded bytes.
            _, _, _, _, session = await self.target(data)
            allowed = not session['archived'] and record['id'] >= session['context_start_record_id']
            result.update(id=data['id'], filename=ref.get('name') or store.path.name,
                          conversation_id=session['id'], generation=session['generation'], can_restore=allowed)
            result['restore_reason'] = ('' if allowed else '归档或旧上下文附件只能查看、下载；不能恢复到当前路径')
            return result
        raw, info = await asyncio.to_thread(store.read_version, data.get('version_id'))
        await self.target(data)
        if action == 'version_download':
            return VersionDownload(raw, ref.get('name') or store.path.name, info['mime_type'], data.get('inline') == '1')
        if action != 'version_preview':
            raise VersionError('未知版本操作')
        if info['kind'] != 'text':
            error = VersionError('此版本不支持文本预览，请下载或查看图片')
            error.status = 415
            raise error
        try:
            text, encoding = decode_full_text(raw, info['encoding'])
            offset = int(data.get('offset') or 0)
            if offset < 0 or offset > len(text):
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise VersionError('版本编码或分页位置无效，请重新打开') from exc
        end = min(len(text), offset + 65536)
        return {'version_id': info['id'], 'text': text[offset:end], 'offset': offset, 'next_offset': end,
                'eof': end == len(text), 'size': len(raw), 'characters': len(text), 'encoding': encoding}

    async def restore(self, data):
        if data.get('confirm') is not True or type(data.get('generation')) is not int:
            raise VersionError('请确认目标版本后再恢复')
        # HTTP disconnect/cancel cannot release the OS slot while a thread is
        # still replacing the file. Finish the small transaction, then propagate.
        task = asyncio.create_task(self._restore(data))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    async def _restore(self, data):
        manager = get_conversations()
        ticket, success = None, False
        async with manager.file_maintenance_slot():
            db, record, ref, metadata, session = await self.target(data)
            if session['generation'] != data['generation'] or session['archived'] or record['id'] < session['context_start_record_id']:
                raise VersionError('附件已不属于当前可编辑上下文，请刷新后确认')
            store = AttachmentVersions(self.upload_root, ref)
            try:
                async with db._transaction() as conn:
                    await db._validate_conversation_write(conn)
                    cursor = await conn.execute('SELECT archived,generation,context_start_record_id FROM chat_sessions WHERE id=?', (session['id'],))
                    checked = await cursor.fetchone()
                    await cursor.close()
                    if checked['archived'] or checked['generation'] != data['generation'] or record['id'] < checked['context_start_record_id']:
                        raise VersionError('会话状态已改变，未恢复文件')
                    ticket = await asyncio.to_thread(store.prepare_restore, data.get('version_id'), data.get('fingerprint'))
                    info = inspect_version(ticket.data, ref)
                    for key in ('kind', 'mime_type', 'encoding', 'width', 'height', 'error'):
                        ref.pop(key, None)
                    ref.update(info, size=len(ticket.data), sha256=hashlib.sha256(ticket.data).hexdigest())
                    ref['versions'] = await asyncio.to_thread(ticket.apply)
                    updated = await conn.execute('UPDATE global_messages SET metadata=? WHERE id=? AND session_id=? AND metadata IS ?',
                        (json.dumps(metadata, ensure_ascii=False), record['id'], session['id'], record.get('metadata')))
                    if updated.rowcount != 1:
                        raise VersionError('附件关联在恢复期间改变，已取消本次恢复')
                success = True
            finally:
                if ticket is not None:
                    await asyncio.to_thread(ticket.finish, success)
            message = build_history_message({**record, 'metadata': metadata}, self.ns['ArtifactManager'].ROOT_DIR,
                                            self.ns['AgentExecutor'].WORK_DIR)
            outbox = self.ns['get_web_outbox']() or self.ns.get('_web_external_outbox')
            if outbox is not None:
                try:
                    outbox.put({'type': 'attachment_changed', 'conversation_id': session['id'],
                                'generation': session['generation'], 'record_id': record['id'], 'message': message})
                except Exception:
                    # The restore is committed. A failed display channel must not
                    # masquerade as a failed write and invite a blind retry.
                    import logging
                    logging.getLogger(__name__).warning('附件版本已恢复，实时通知暂失败；刷新可核对', exc_info=True)
            return {'ok': True, 'id': data['id'], 'restored_version': ref['sha256'], 'message': message}
