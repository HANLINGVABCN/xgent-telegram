"""Versioned message intent shared by live transport, history and model context.

This module decides where a message belongs; it never rewrites its body, executes
callbacks or grants delivery to a conversation. Conversation/run routing remains
authoritative. Old text recognition runs only during the startup migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import json

POLICY_VERSION = 1
CHANNELS = ('web', 'telegram', 'cli')
PURPOSES = frozenset({'chat', 'media', 'tool_result', 'status', 'generation', 'form',
                      'form_input', 'model_context', 'usage', 'menu', 'audit'})
SPEAKERS = frozenset({'user', 'assistant', 'system', 'tool'})
_DEFAULTS = {
    'user_text': ('chat', 'user', True), 'user_file': ('chat', 'user', True),
    'user_photo': ('chat', 'user', True), 'user_sticker': ('chat', 'user', True),
    'ai_reply': ('chat', 'assistant', True), 'media_reply': ('media', 'assistant', True),
    'agent_result': ('tool_result', 'tool', True), 'runtime_error': ('status', 'system', True),
    'system_op': ('status', 'system', True), 'agent_status': ('status', 'system', False),
    'presentation_note': ('status', 'assistant', False),
    'token_usage': ('usage', 'system', False), 'ui_message': ('menu', 'system', False),
    'command': ('menu', 'user', False), 'management_input': ('audit', 'system', False),
    'button_click': ('audit', 'system', False), 'agent_cmd': ('audit', 'system', True),
    'ask_input_notice': ('form_input', 'system', False),
}


def object_dict(value: Any) -> dict:
    try:
        value = json.loads(value) if isinstance(value, str) else value
        return dict(value) if isinstance(value, Mapping) else {}
    except (TypeError, ValueError):
        return {}


@dataclass(frozen=True)
class MessagePolicy:
    purpose: str
    speaker: str
    audience: tuple[str, ...]
    model_context: bool
    origin: str = 'legacy'
    form_id: str | None = None
    field_id: str | None = None
    transient: bool = False

    def __post_init__(self):
        if (self.purpose not in PURPOSES or self.speaker not in SPEAKERS
                or not isinstance(self.audience, tuple) or len(set(self.audience)) != len(self.audience)
                or any(channel not in CHANNELS for channel in self.audience)
                or type(self.model_context) is not bool or type(self.transient) is not bool
                or not isinstance(self.origin, str) or len(self.origin) > 128
                or any(value is not None and (not isinstance(value, str) or len(value) > 128)
                       for value in (self.form_id, self.field_id))):
            raise ValueError('Invalid message policy')
        if self.transient and self.model_context:
            raise ValueError('Transient messages cannot be model context')

    def allows(self, channel: str) -> bool:
        return channel in self.audience

    def to_dict(self) -> dict:
        value = {'version': POLICY_VERSION, 'purpose': self.purpose, 'speaker': self.speaker,
                 'audience': list(self.audience), 'model_context': self.model_context,
                 'origin': self.origin, 'transient': self.transient}
        if self.form_id is not None:
            value['form_id'] = self.form_id
        if self.field_id is not None:
            value['field_id'] = self.field_id
        return value

    @classmethod
    def from_dict(cls, value: Any) -> MessagePolicy:
        if (not isinstance(value, Mapping) or type(value.get('version')) is not int
                or value['version'] != POLICY_VERSION or not isinstance(value.get('audience'), list)):
            raise ValueError('Unsupported message policy')
        try:
            return cls(purpose=value['purpose'], speaker=value['speaker'],
                       audience=tuple(value['audience']), model_context=value['model_context'],
                       origin=value.get('origin', 'legacy'), form_id=value.get('form_id'),
                       field_id=value.get('field_id'), transient=value.get('transient', False))
        except (KeyError, TypeError) as exc:
            raise ValueError('Incomplete message policy') from exc


def new_policy(kind: str, role=None, *, metadata=None, hint=None, origin=None) -> MessagePolicy:
    """Set message intent once from structural producer fields, not body wording."""
    meta, hint = object_dict(metadata), object_dict(hint)
    if 'message_policy' in meta:
        return MessagePolicy.from_dict(meta['message_policy'])
    if 'message_policy' in hint:
        return MessagePolicy.from_dict(hint['message_policy'])
    purpose, speaker, context = _DEFAULTS.get(kind, ('chat', 'user' if role == 'user' else 'assistant', True))
    if origin is None:
        from .interaction import identity
        origin = identity()[0]
    if meta.get('ask_answer'):
        purpose, speaker, context = 'model_context', 'user', True
    elif meta.get('ask_form') or hint.get('ask_id'):
        purpose, speaker, context = 'form', 'tool', False
    elif (meta.get('ui_audit') and not (meta.get('display_media') or meta.get('compression_task'))
          or kind in {'button_click', 'management_input'}):
        purpose, context = 'audit', False
    elif meta.get('ui_audit') or meta.get('ui_only') or meta.get('compression_auxiliary'):
        context = False
    transient = bool(hint.get('transient'))
    purpose = hint.get('purpose', purpose)
    speaker = hint.get('speaker', speaker)
    context = hint.get('model_context', context)
    if purpose in {'form_input', 'generation', 'form', 'menu', 'usage'}:
        context = False
    if transient:
        context = False
    audience = CHANNELS
    if purpose in {'audit', 'model_context'}:
        audience = ()
    elif purpose == 'usage':
        audience = ('telegram', 'cli')
    elif purpose == 'form_input':
        audience = (origin,) if origin in {'telegram', 'cli'} else ()
    if 'audience' in hint:
        audience = tuple(hint['audience'])
    ask = object_dict(meta.get('ask_form'))
    return MessagePolicy(purpose, speaker, audience, context, str(origin),
                         hint.get('form_id', hint.get('ask_id', ask.get('id'))),
                         hint.get('field_id'), transient)


def read_policy(value: Any) -> MessagePolicy:
    """An invalid explicit policy never falls back to more permissive text guessing."""
    try:
        return MessagePolicy.from_dict(value)
    except (TypeError, ValueError):
        return MessagePolicy('audit', 'system', (), False)


def record_policy(record: Mapping) -> MessagePolicy:
    meta = object_dict(record.get('metadata'))
    explicit = record.get('message_policy', meta.get('message_policy'))
    return read_policy(explicit)


def frame_policy(frame: Mapping) -> MessagePolicy | None:
    if 'message_policy' in frame:
        return read_policy(frame['message_policy'])
    from .presentation import current_message_presentation
    hint = current_message_presentation()
    kind = frame.get('msg_type') or (hint or {}).get('kind')
    if not kind:
        kind = {'user_message': 'user_text', 'notice': 'system_op'}.get(frame.get('type'))
    if not kind:
        from .interaction import identity
        kind = 'ui_message' if identity()[1] in {'management','menu','history'} else (
            'user_text' if frame.get('role')=='user' else 'ai_reply')
    return new_policy(kind, frame.get('role'), hint=hint)


def stamp_frame_policy(frame: dict) -> dict:
    if frame.get('type') not in {'message', 'edit', 'edit_markup', 'record_snapshot',
                                'user_message', 'notice', 'document', 'photo'}:
        return dict(frame)
    policy = frame_policy(frame)
    return {**frame, 'message_policy': policy.to_dict()} if policy is not None else dict(frame)


def stamp_record_metadata(kind, role, metadata=None) -> dict:
    meta = object_dict(metadata)
    from .presentation import current_message_presentation
    hint = current_message_presentation()
    # Recording a result/user input must not inherit an unrelated UI hint.
    if hint and hint.get('kind') != kind:
        hint = None
    meta['message_policy'] = new_policy(kind, role, metadata=meta, hint=hint).to_dict()
    return meta


def current_policy() -> MessagePolicy | None:
    from .presentation import current_message_presentation
    hint = current_message_presentation()
    return read_policy(hint['message_policy']) if hint and 'message_policy' in hint else None
