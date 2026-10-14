"""A live Web run's opt-in Telegram delivery, not a backlog replay policy.

Owned by the event loop and disposed with the execution slot. Only the current
unfinished reply is materialized; immutable/muted queued Ops are never modified.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict

_reply = ContextVar('web_reply_delivery', default=None)


class WebRunDelivery:
    def __init__(self, scope):
        self.scope = scope
        self.subscribed = scope.telegram_delivery is not False
        self.active = True
        self.reply = None

    def close(self):
        self.active = False
        if self.reply is not None:
            self.reply['fragments'].clear()
        self.reply = None

    def offer(self, bridge, op):
        """Called only for newly produced live operations, before queue admission."""
        from .fanout import CREATING_KINDS, OP_DELETE, OP_EDIT, OP_EDIT_MARKUP
        if not self.active:
            return
        if self.subscribed:
            # Freeze the NEW operation's decision so disconnect/restart delivery
            # does not need this ephemeral subscription, or the current selection.
            op.conversation_context = {**(op.conversation_context or {}), 'telegram_delivery': True}
            bridge._enqueue_telegram(op)
            return
        reply = _reply.get()
        if reply is None or reply is not self.reply or not reply['active']:
            return
        fragments = reply['fragments']
        if op.kind in CREATING_KINDS:
            # Keep one current value per logical ID, not the streaming edit log.
            fragments[op.logical_id] = (bridge, op)
        elif op.kind == OP_DELETE:
            fragments.pop(op.logical_id, None)
        elif op.logical_id in fragments:
            _, created = fragments[op.logical_id]
            if op.kind == OP_EDIT:
                created.payload.update({key: deepcopy(op.payload[key]) for key in
                                        ('text', 'parse_mode', 'reply_markup') if key in op.payload})
                created.transient = dict(op.transient)
                created.presentation_context = dict(op.presentation_context)
            elif op.kind == OP_EDIT_MARKUP:
                created.payload['reply_markup'] = deepcopy(op.payload.get('reply_markup'))
                created.transient['reply_markup'] = op.transient.get('reply_markup')

    def subscribe(self):
        if not self.active or self.subscribed:
            return
        from .fanout import Op, UPLOAD_KINDS
        self.subscribed = True
        fragments = self.reply['fragments'] if self.reply is not None else {}
        # No await between this snapshot and queue admission. Later edits cannot
        # overtake creation; ChannelWorker establishes the native ID first.
        for bridge, saved in fragments.values():
            transient = dict(saved.transient)
            if saved.kind in UPLOAD_KINDS and saved.payload.get('path'):
                transient = {}  # Reopen the file, not an already closed upload handle.
            op = Op(saved.kind, logical_id=saved.logical_id, chat_id=saved.chat_id,
                    payload={**deepcopy(saved.payload),
                             '_conversation_context': {**asdict(self.scope), 'telegram_delivery': True},
                             '_telegram_presentation': dict(saved.presentation_context)},
                    transient=transient, durable=saved.durable)
            bridge._enqueue_telegram(op)
        fragments.clear()


def current_run_delivery():
    from . import conversations
    scope = conversations.current_scope(required=False)
    manager = conversations._manager
    delivery = getattr(manager, '_reply_delivery', None)
    if (scope is not None and delivery is not None and delivery.active
            and manager._run_id == scope.run_id == delivery.scope.run_id
            and (scope.conversation_id, scope.generation, scope.origin, scope.purpose) ==
                (delivery.scope.conversation_id, delivery.scope.generation, 'web', 'chat')):
        return delivery
    return None


@contextmanager
def live_reply_delivery():
    delivery = current_run_delivery()
    if delivery is None:
        yield
        return
    reply = {'active': True, 'fragments': OrderedDict()}
    previous, delivery.reply = delivery.reply, reply
    token = _reply.set(reply)
    try:
        yield
    finally:
        reply['active'] = False
        reply['fragments'].clear()
        if delivery.reply is reply:
            delivery.reply = previous
        _reply.reset(token)
