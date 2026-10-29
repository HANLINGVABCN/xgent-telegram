"""Endpoint-local form input; all routing rules are owned by message_policy."""
from __future__ import annotations


def local_ask_notice():
    from .message_policy import current_policy
    policy = current_policy()
    return policy is not None and policy.purpose == 'form_input'


async def send_ask_input_notice(message, text, *, form_id=None, field_id=None):
    from .presentation import message_presentation
    with message_presentation({'kind': 'ask_input_notice', 'purpose': 'form_input',
                               'mode': 'folded', 'form_id': form_id, 'field_id': field_id}):
        return await message.reply_text(text)
