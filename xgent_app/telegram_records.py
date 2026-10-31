"""Exact delivered IDs tied to durable records; never infer neighbouring messages."""
from collections import OrderedDict
import inspect
import secrets

INSTANCE = secrets.token_hex(16)
_records = OrderedDict()
_deliveries = OrderedDict()
_withdrawal_notices = set()


def _bot_id(bot):
    try:
        return int(bot.id)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def _namespace():
    from .web_history import _section_ns
    return _section_ns()


def source_bot_id():
    namespace = _namespace() or {}
    identity = _bot_id(namespace.get('_web_real_bot'))
    if identity is not None:
        return identity
    prefix = str(getattr(namespace.get('BotConfig'), 'TOKEN', '')).partition(':')[0]
    return int(prefix) if prefix.isdigit() else None


async def bind_record(db, row, logical_ids):
    from .presentation import metadata_dict
    ids = list(dict.fromkeys(mid for mid in logical_ids if type(mid) is int))
    for mid in ids:
        _records[mid] = (row['id'], row['session_id'])
    while len(_records) > 5000:
        _records.popitem(last=False)
    metadata = metadata_dict(row.get('metadata'))
    metadata['telegram_logical_instance'] = INSTANCE
    metadata['telegram_logical_ids'] = list(dict.fromkeys([*metadata.get('telegram_logical_ids', []), *ids]))
    for mid in ids:
        delivery = _deliveries.get(mid)
        if delivery:
            old = metadata.get('telegram_delivery') or {}
            metadata['telegram_delivery'] = {**delivery, 'ids': list(dict.fromkeys([
                *(old.get('ids', []) if old.get('bot_id') == delivery['bot_id'] else []), *delivery['ids']]))}
    await db.patch_display_metadata(row['id'], {key: metadata[key] for key in
        ('telegram_logical_ids', 'telegram_logical_instance', 'telegram_delivery') if key in metadata})


async def delivery_allowed(op):
    from .fanout import OP_DELETE
    if op.kind == OP_DELETE or 'delete' in op.kind:
        return True
    entry = _records.get(op.logical_id)
    record_id = op.payload.get('telegram_native', {}).get('record_id') or op.payload.get('_record_id') or (entry[0] if entry else None)
    ns = _namespace()
    if not record_id or not ns:
        return True
    db = await ns['BotMemoryDB'].get_instance()
    conn = await db._get_conn()
    cursor = await conn.execute('SELECT withdrawn FROM global_messages WHERE id=?', (record_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None and not row['withdrawn']


async def delivered(bot, op, result):
    if 'delete' in op.kind or op.logical_id is None:
        return
    ids = list(op.payload.get('_native_parts') or ([result] if type(result) is int else []))
    if not ids:
        return
    _deliveries[op.logical_id] = {'bot_id': _bot_id(bot), 'chat_id': op.chat_id, 'ids': ids}
    while len(_deliveries) > 5000:
        _deliveries.popitem(last=False)
    entry = _records.get(op.logical_id)
    record_id = op.payload.get('telegram_native', {}).get('record_id') or (entry[0] if entry else None)
    ns = _namespace()
    if not ns or not record_id:
        return
    db = await ns['BotMemoryDB'].get_instance()
    row = await db.get_display_message(record_id)
    if row:
        await bind_record(db, row, [op.logical_id])
    else:
        # A withdrawal won while an upload was in flight: delete that exact result.
        for mid in ids:
            await bot.delete_message(chat_id=op.chat_id, message_id=mid)


def track_delivery(function):
    async def wrapped(bot, op, *args, **kwargs):
        if not await delivery_allowed(op):
            return None
        try:
            result = await function(bot, op, *args, **kwargs)
        except Exception:
            key = op.payload.get('_withdrawal')
            if key and key not in _withdrawal_notices:
                from .fanout import OpNotDeliverable
                import sys
                error = sys.exc_info()[1]
                if isinstance(error, OpNotDeliverable):
                    _withdrawal_notices.add(key)
                    ns = _namespace() or {}
                    outbox = ns.get('get_web_outbox', lambda: None)()
                    if outbox:
                        outbox.put({'type': 'withdrawal_sync', 'text': '网页已撤回，但部分电报消息因平台限制未能删除。'})
            raise
        try:
            await delivered(bot, op, result)
        except Exception:
            import logging
            logging.getLogger(__name__).warning('Telegram已送达，消息关联保存失败；不重复发送。', exc_info=True)
        return result
    wrapped.__name__ = function.__name__
    wrapped.__doc__ = function.__doc__
    wrapped.__signature__ = inspect.signature(function)
    return wrapped
