"""A turn-local bot facade; presentation never changes execution or persistence."""
from __future__ import annotations

from .agent_status import compact_agent_round_status
from .presentation import (
    current_message_presentation, message_presentation, operation_html,
    technical_notice, successful_status,
)


def _generation_status(text):
    return str(text).startswith(('前台流式输出中', '流式输出中', '非流式输出中',
                                 '后台流式生成中', '后台流式输出中'))


class PresentationMessage:
    def __init__(self, bot, message, text='', parse_mode=None, kind='ai_reply', hidden=False):
        self.bot, self.original = bot, message
        self.text, self.parse_mode, self.kind = text, parse_mode, kind
        self.hidden = hidden
        self.base_text = text
        self.record_id = None

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def edit_text(self, text, **kwargs):
        if self.hidden:
            if technical_notice(text):
                if compact_agent_round_status(text):
                    with message_presentation({'kind': 'agent_status', 'mode': self.bot.turn.mode}):
                        return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)
                self.bot.remember_notice(text)
                return self
            # A hidden progress placeholder may later carry an actionable failure.
            # Materialize that failure instead of silently swallowing it.
            with message_presentation({'kind': 'runtime_error', 'mode': self.bot.turn.mode}):
                return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)
        return await self.bot.edit_message_text(text, chat_id=self.chat_id,
            message_id=self.message_id, **kwargs)

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        if self.hidden:
            return True
        return await self.bot.edit_message_reply_markup(chat_id=self.chat_id,
            message_id=self.message_id, reply_markup=reply_markup, **kwargs)

    async def delete(self):
        if self.hidden:
            return True
        return await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)


class HiddenMessage:
    message_id = None

    def __init__(self, chat_id):
        self.chat_id = chat_id


class PresentationBot:
    def __init__(self, bot, turn):
        self.original, self.turn = bot, turn
        self.messages = {}

    def __getattr__(self, name):
        return getattr(self.original, name)

    def remember_notice(self, text):
        if technical_notice(text) and not successful_status(text) and not str(text).startswith('系统提示：'):
            if any(word in str(text) for word in ('停止', '失败', '超过', '超限')):
                self.turn.reason = ('⏹️ 已停止。' if '停止' in str(text) else
                                    '⚠️ 已达到执行上限。' if any(word in str(text) for word in ('超过', '超限')) else
                                    '⚠️ 本次处理失败，已有结果已保留。')

    def _kind(self, text):
        if _generation_status(text):
            return 'ai_reply'
        explicit = current_message_presentation()
        if explicit:
            if explicit.get('kind') == 'runtime_error':
                self.turn.reason = '⚠️ 本次处理失败，已有结果已保留。'
            return explicit.get('kind', 'ai_reply')
        from .web_display import current_web_display
        if current_web_display() is not None:
            return 'ai_reply'
        if technical_notice(text):
            return 'agent_status'
        if self.turn.active_reply is not None:
            return 'ai_reply'
        return 'agent_result' if self.turn.active_operation is not None else 'ai_reply'

    def _prepare(self, text, kind, markup, parse_mode=None):
        text = str(text)
        if not self.turn.folded:
            return text
        if kind == 'runtime_error':
            return text
        if _generation_status(text):
            return text
        from .web_display import current_web_display
        if technical_notice(text) and current_web_display() is None:
            caption = compact_agent_round_status(text)
            if caption:
                return caption
            self.remember_notice(text)
            return None
        op = self.turn.active_operation
        # A form is actionable UI, not an output preview.
        if (kind == 'agent_result' and op and op['kind'] != 'ask' and not markup
                and 'data-compact="1"' not in text
                and ('output' not in op or op.get('detailed_html') == text)):
            if 'detailed_html' not in op:
                op['detailed_html'] = text
            import html,re
            output = html.unescape(re.sub(r'<[^>]+>', '', text)) if 'html' in str(parse_mode).lower() else text
            op.setdefault('output', output)
            if str(text).startswith(('⚠️', '❌')) or '写入失败' in text:
                op['success'] = False
            return operation_html(op)
        return text

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kwargs):
        kind = self._kind(text)
        prepared = self._prepare(text, kind, reply_markup, parse_mode)
        if prepared is None:
            return PresentationMessage(self, HiddenMessage(chat_id), hidden=True, kind='agent_status')
        if prepared != text and prepared.startswith(('<blockquote', '⚠️', '🖥️')):
            parse_mode = 'HTML'
        op = self.turn.active_operation
        if self.turn.folded and kind == 'agent_result' and op and op.get('output_path') and reply_markup is None and self.turn.last_record_id is not None:
            from telegram import InlineKeyboardMarkup, InlineKeyboardButton
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton('完整输出',
                callback_data=f'present_output:{self.turn.last_record_id}')]])
        hint = {'kind': kind, 'mode': self.turn.mode}
        record_ids = getattr(self.turn, 'record_ids', {})
        if kind in {'agent_result', 'runtime_error', 'presentation_note'} and record_ids.get(kind) == self.turn.last_record_id:
            hint['record_id'] = self.turn.last_record_id
        elif kind == 'agent_status' and str(text).startswith('✅') and record_ids.get(kind):
            hint['record_id'] = record_ids[kind]
        with message_presentation(hint):
            original = await self.original.send_message(chat_id=chat_id, text=prepared,
                reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
        message = PresentationMessage(self, original, prepared, parse_mode, kind)
        message.record_id = hint.get('record_id')
        message.reply_markup = reply_markup
        self.messages[message.message_id] = message
        self.turn.last_message = message
        return message

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                                reply_markup=None, parse_mode=None, **kwargs):
        previous = self.messages.get(message_id)
        kind = previous.kind if previous is not None else self._kind(text)
        prepared = self._prepare(text, kind, reply_markup, parse_mode)
        if prepared is None:
            return previous
        hint = {'kind': kind, 'mode': self.turn.mode}
        if previous is not None and previous.record_id is not None:
            hint['record_id'] = previous.record_id
        with message_presentation(hint):
            original = await self.original.edit_message_text(prepared, chat_id=chat_id,
                message_id=message_id, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
        if previous is None:
            previous = PresentationMessage(self, original, prepared, parse_mode, kind)
            self.messages[message_id] = previous
        previous.text, previous.parse_mode = prepared, parse_mode
        previous.reply_markup = reply_markup
        previous.base_text = prepared
        self.turn.last_message = previous
        return previous

    async def edit_message_reply_markup(self, **kwargs):
        message = self.messages.get(kwargs.get('message_id'))
        hint = {'kind': message.kind if message else 'ai_reply', 'mode': self.turn.mode}
        if message is not None and message.record_id is not None:
            hint['record_id'] = message.record_id
        with message_presentation(hint):
            result = await self.original.edit_message_reply_markup(**kwargs)
        if message is not None:
            message.reply_markup = kwargs.get('reply_markup')
        return result

    async def delete_message(self, chat_id=None, message_id=None, **kwargs):
        result = await self.original.delete_message(chat_id=chat_id, message_id=message_id, **kwargs)
        message = self.messages.pop(message_id, None)
        if self.turn.last_message is message:
            self.turn.last_message = None
        return result

    async def _send_media(self, method, *args, **kwargs):
        original = await getattr(self.original, method)(*args, **kwargs)
        message = PresentationMessage(self, original, str(kwargs.get('caption') or ''), kwargs.get('parse_mode'), 'media_reply')
        message.media = True
        message.reply_markup = kwargs.get('reply_markup')
        self.messages[message.message_id] = message
        self.turn.last_message = message
        return original

    async def send_document(self, *args, **kwargs):
        return await self._send_media('send_document', *args, **kwargs)

    async def send_photo(self, *args, **kwargs):
        return await self._send_media('send_photo', *args, **kwargs)


class PresentationContext:
    def __init__(self, context, turn):
        self.original = context
        self.bot = PresentationBot(context.bot, turn)

    def __getattr__(self, name):
        return getattr(self.original, name)
