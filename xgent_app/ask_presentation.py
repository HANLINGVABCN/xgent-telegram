"""Endpoint-local form input; all routing rules are owned by message_policy."""
from __future__ import annotations

from collections import deque

_consumed = set()
_consumed_order = deque()


def input_identity(message):
    return (getattr(message, 'chat_id', None), getattr(message, 'message_id', None))


def consumed_input(message):
    return input_identity(message) in _consumed


def mark_input_consumed(message):
    key = input_identity(message)
    if None in key or key in _consumed:
        return
    _consumed.add(key)
    _consumed_order.append(key)
    while len(_consumed_order) > 2000:
        _consumed.discard(_consumed_order.popleft())


async def cleanup_input(bot, chat_id, message_ids):
    """Delete only known field-input IDs; retry through the ordinary channel."""
    from .presentation import message_presentation
    for mid in dict.fromkeys(message_ids):
        if not isinstance(mid, int) or mid <= 0:
            continue
        try:
            with message_presentation({'kind': 'ask_input_notice', 'purpose': 'form_input'}):
                await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception as exc:
            from telegram.error import BadRequest
            if isinstance(exc, BadRequest):
                continue
            from .fanout import Op, OP_DELETE, get_channel_registry
            with message_presentation({'kind': 'ask_input_notice', 'purpose': 'form_input'}):
                channel = get_channel_registry().get('telegram')
                if channel is not None:
                    channel.offer(Op(OP_DELETE, chat_id=chat_id, payload={'native_id': mid}))


def local_ask_notice():
    from .message_policy import current_policy
    policy = current_policy()
    return policy is not None and policy.purpose == 'form_input'


async def send_ask_input_notice(message, text, *, form_id=None, field_id=None):
    from .presentation import message_presentation
    with message_presentation({'kind': 'ask_input_notice', 'purpose': 'form_input',
                               'mode': 'folded', 'form_id': form_id, 'field_id': field_id}):
        return await message.reply_text(text)
