"""Web form actions over the original pending Agent continuation, never a new agent."""
from __future__ import annotations

from .agent_ask import PENDING_ASKS, SECRET_STORE
from .conversations import current_scope
from .ui_history import without_ui_history
import asyncio

_settling = set()


@without_ui_history
async def sync_ask(namespace, pending, *, status='pending', require_delivery=False):
    """Save only safe form state; the model-history snapshot remains in memory."""
    db = await namespace['BotMemoryDB'].get_instance()
    if pending.form_record_id is not None:
        from .presentation import metadata_dict
        row = await db.get_display_message(pending.form_record_id)
        if row:
            metadata = metadata_dict(row.get('metadata'))
            metadata['ask_form'] = pending.web_snapshot(status)
            await db.backfill_attachment_metadata(pending.form_record_id, row.get('metadata'), metadata)
    # The creator owns the logical/native message mapping (or the CLI screen).
    # A fresh bot can interpret the logical ID as a native Telegram ID instead.
    bot = pending.form_bot or namespace['build_trigger_delivery_bot'](int(pending.chat_id))
    if pending.form_message_id is not None:
        try:
            from .presentation import message_presentation
            from .conversations import replay_conversation
            from contextlib import nullcontext
            hint = {'kind':'agent_result','mode':'folded','ask_id':pending.ask_id,'purpose':'form'}
            route = replay_conversation(pending.conversation_context) if pending.conversation_context else nullcontext()
            with route, message_presentation(hint):
                if status == 'submitted':
                    from .telegram_form_receipts import build
                    from .telegram_presentation import chat_display
                    spec = build(pending)
                    spec['owner_id'] = pending.form_message_id
                    hint.update(audience=['telegram'], model_context=False, record_id=pending.form_record_id)
                    with message_presentation(hint), chat_display({'telegram_native': spec}):
                        await bot.edit_message_text(chat_id=pending.chat_id, message_id=pending.form_message_id,
                            text=spec['fallback'][0], parse_mode='HTML', reply_markup=None)
                else:
                    await bot.edit_message_reply_markup(chat_id=pending.chat_id, message_id=pending.form_message_id,
                        reply_markup=pending.form.build_keyboard(pending.ask_id, pending.draft) if status=='pending' else None)
            if require_delivery and hasattr(bot, '_tg_channel'):
                channel = bot._tg_channel()
                if not await channel.wait_idle(timeout=5):
                    raise RuntimeError('表单更新尚未送达，输入保留。')
        except Exception:
            if require_delivery:
                raise
            import logging
            logging.getLogger(__name__).warning('原表单按钮更新失败，草稿已保留；不重发表单', exc_info=True)
    context = namespace['_SelfTriggerContext'](bot)
    if pending.form_record_id is not None:
        await namespace['publish_saved_reply'](context, pending.form_record_id)


def validated_draft(pending, value):
    if not isinstance(value, dict) or set(value)-{q.qid for q in pending.form.questions}:
        raise ValueError('表单答案格式无效')
    result = {}
    for question in pending.form.questions:
        entry = value.get(question.qid, {})
        if not isinstance(entry, dict) or set(entry)-{'selected','custom'}:
            raise ValueError('表单答案包含无效字段')
        if question.qtype == 'secret':
            if entry:
                raise ValueError('密钥必须使用专属录入字段')
            if pending.draft.get(question.qid, {}).get('filled'):
                result[question.qid] = {'filled': True}
            continue
        selected = entry.get('selected', [])
        if (not isinstance(selected, list) or any(type(i) is not int or not 0<=i<len(question.options) for i in selected)
                or question.qtype=='text' and selected or question.qtype=='single' and len(selected)>1):
            raise ValueError('表单选项无效')
        custom = entry.get('custom', '')
        if not isinstance(custom, str) or len(custom)>20000 or custom and not question.allows_custom:
            raise ValueError('自定义答案无效或过长')
        if question.qtype=='single' and selected and custom.strip():
            raise ValueError('单选题只能选择一项或填写自定义答案')
        result[question.qid] = {'selected': sorted(set(selected)), 'custom': custom}
    return result


async def saved_form(namespace, form_id):
    from .presentation import metadata_dict
    from copy import deepcopy
    scope = current_scope()
    db = await namespace['BotMemoryDB'].get_instance()
    conn = await db._get_conn()
    cursor = await conn.execute("SELECT metadata FROM global_messages WHERE session_id=? AND withdrawn=0 "
        "AND json_extract(metadata,'$.ask_form.id')=? ORDER BY id DESC LIMIT 1", (scope.conversation_id, form_id))
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    if not row:
        return None
    snapshot = deepcopy(metadata_dict(row['metadata']).get('ask_form'))
    if not isinstance(snapshot,dict) or snapshot.get('conversation_id')!=scope.conversation_id:
        return None
    if snapshot.get('generation')!=scope.generation or snapshot.get('status')=='pending':
        # The only continuation lives in PendingAsk. A saved pending form alone
        # cannot resume work after a process restart.
        snapshot['status']='expired'
    return snapshot


async def handle_ask(namespace, method, data, background, *, idempotent=False):
    if idempotent and method=='POST':
        # The HTTP caller may disappear after the database commit. Finish the
        # same locked submission and schedule its continuation exactly once.
        task = asyncio.create_task(_handle_ask(namespace,method,data,background,idempotent=True))
        _settling.add(task)
        def done(value):
            _settling.discard(value)
            if not value.cancelled():
                value.exception()
        task.add_done_callback(done)
        return await asyncio.shield(task)
    return await _handle_ask(namespace,method,data,background,idempotent=idempotent)


async def _handle_ask(namespace, method, data, background, *, idempotent=False):
    pending = PENDING_ASKS.get(str(data.get('id') or ''))
    scope = current_scope()
    if pending is None:
        if idempotent:
            snapshot = await saved_form(namespace,str(data.get('id') or ''))
            if snapshot:
                return snapshot
        raise ValueError('这个表单已处理或失效，请刷新当前对话')
    if pending.conversation_id!=scope.conversation_id or pending.generation!=scope.generation:
        raise ValueError('这个表单已处理或失效，请刷新当前对话')
    if method=='GET':
        return pending.web_snapshot()
    action = data.get('action') or 'save'
    if action not in {'save','submit','cancel'}:
        raise ValueError('不支持的表单操作')
    async with pending.lock:
        if idempotent and pending.status!='pending':
            return pending.web_snapshot()
        if PENDING_ASKS.get(pending.ask_id) is not pending:
            raise ValueError('这个表单已处理，请刷新当前对话')
        if type(data.get('revision')) is not int or data['revision']!=pending.revision:
            raise ValueError('表单已在其他页面更新，请刷新表单后重试')
        if pending.status!='pending':
            raise ValueError('表单已提交或取消，请等待处理结果')
        if action!='cancel':
            draft = validated_draft(pending, data.get('draft'))
            secrets = data.get('secrets') or {}
            secret_questions = {q.qid: q for q in pending.form.questions if q.qtype=='secret'}
            if (not isinstance(secrets,dict) or set(secrets)-secret_questions.keys()
                    or any(not isinstance(value,str) or len(value)>20000 for value in secrets.values())):
                raise ValueError('密钥录入字段无效')
            for qid,value in secrets.items():
                if value:
                    SECRET_STORE.set(pending.conversation_id, secret_questions[qid].var, value)
                    draft[qid] = {'filled': True}
            pending.draft = draft
            pending.revision += 1
            await sync_ask(namespace,pending)
            if action=='submit' and pending.form.unanswered(pending.draft):
                raise ValueError('请填写全部问题后再提交')
        snapshot = pending.web_snapshot()
        if action!='save':
            pending.status = 'submitted' if action=='submit' else 'cancelled'
            pending.revision += 1
            await sync_ask(namespace,pending,status=pending.status)
            snapshot = pending.web_snapshot()
            background(namespace['resume_from_ask'](pending.ask_id) if action=='submit' else namespace['cancel_ask'](pending.ask_id))
    return snapshot
