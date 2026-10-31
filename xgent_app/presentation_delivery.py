"""A turn-local bot facade; presentation never changes execution or persistence."""
from __future__ import annotations

from .agent_status import compact_agent_round_status
from .presentation import current_message_presentation, message_presentation, operation_html
from .telegram_presentation import chat_display


class PresentationMessage:
    def __init__(self, bot, message, text='', parse_mode=None, kind='ai_reply', hidden=False):
        self.bot, self.original = bot, message
        self.text, self.parse_mode, self.kind = text, parse_mode, kind
        self.hidden = hidden
        self.base_text = text
        self.record_id = None
        self.policy = None
        self.telegram_body = text
        self.telegram_parse_mode = parse_mode
        self.telegram_complete = False
        self.telegram_extra_messages = []
        self.telegram_extra_texts = []
        self.telegram_suppressed = False
        self.telegram_owner_id = None
        self.telegram_native = None

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def edit_text(self, text, **kwargs):
        if self.hidden:
            # The producer edits a status placeholder; its terminal state keeps
            # the same type, regardless of the words in its body.
            with message_presentation({'kind': self.kind, 'mode': self.bot.turn.mode}):
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
        self._telegram_final_replies = {}
        self._telegram_generation = None

    async def send_generated_group(self, chat_id, artifacts, text=''):
        original = await self.original.send_generated_group(chat_id, artifacts, text)
        message = PresentationMessage(self, original, text, kind='media_reply')
        message.media = True
        message.media_payload = {'artifacts': artifacts, 'text': text}
        message.record_id = self.turn.last_record_id
        self.messages[message.message_id] = message
        return message

    def _native_enabled(self):
        from .telegram_native import request_capable
        bot = self.original
        for _ in range(6):
            if request_capable(bot):
                return True
            if getattr(bot, '_is_xgent_cli_bot', False):
                return True  # The backend chooses its actual Telegram capability.
            next_bot = getattr(bot, 'real_bot', None) or getattr(bot, 'original', None)
            if next_bot is None or next_bot is bot:
                return False
            bot = next_bot
        return False

    def begin_telegram_generation(self, mode):
        import secrets
        from .conversations import current_scope
        from .stop_targets import begin_phase
        scope = current_scope(required=False)
        run_id = scope.run_id if scope else None
        self._telegram_generation = {'ref': begin_phase(run_id, 'model'),
            'run_id': run_id, 'mode': mode, 'draft_id': secrets.randbits(63) or 1,
            'raw': '', 'owner_id': None}

    def _native_spec(self, hint, kind, previous, legacy_chunks=None):
        if not self._native_enabled():
            return None
        from .telegram_rich import render_rich, legacy_chunks as fallback, output_rich
        content = hint.get('telegram_content')
        display = hint.get('telegram_display') or {}
        operation = hint.get('telegram_operation') or self.turn.active_operation
        if kind == 'agent_result' and operation and operation.get('kind') != 'ask':
            from .conversations import current_scope
            from .telegram_chat import output_chunks
            scope = current_scope(required=False)
            return {'phase': 'reply', 'html': output_rich(operation), 'fallback': output_chunks(operation),
                    'ref': operation.get('_stop_phase'), 'running': bool(operation.get('running')),
                    'run_id': scope.run_id if scope else None, 'record_id': hint.get('record_id')}
        if kind == 'runtime_error' and 'telegram_error' in hint:
            from .telegram_rich import Block, split_blocks
            raw = hint['telegram_error']
            owner_id = hint.get('error_owner_id')
            result = {'phase': 'reply', 'record_id': hint.get('record_id'),
                      'html': split_blocks([Block('p', text=raw)]),
                      'fallback': split_blocks([Block(text=raw)], budget=3800),
                      'owner_id': owner_id}
            if owner_id is not None:
                result['auxiliary'] = previous is None or previous.message_id != owner_id
            return result
        if kind != 'ai_reply':
            return previous.telegram_native if previous is not None else None
        generation = self._telegram_generation
        if (hint.get('purpose') == 'generation' or (hint.get('message_policy') or {}).get('purpose') == 'generation') and content is None:
            return {'phase': 'control', **(generation or {})}
        if content is None and 'raw_markdown' in display:
            content = {'raw': display['raw_markdown'], 'owner_id': display.get('owner_id'), 'complete': True}
        if content is None:
            if previous is not None and previous.telegram_native:
                return dict(previous.telegram_native)
            return None
        raw = str(content.get('raw') or '')
        complete = bool(content.get('complete'))
        owner_id = content.get('owner_id') or (previous.telegram_owner_id if previous else None)
        if generation:
            generation['raw'] = raw
            generation['owner_id'] = owner_id or generation.get('owner_id')
            if complete:
                from .stop_targets import end_phase
                end_phase(generation['run_id'], generation['ref'])
        phase = 'reply' if complete else 'draft' if (generation or {}).get('mode') == 'foreground' else 'control'
        record_id = hint.get('record_id') or (self.turn.active_reply or {}).get('record_id')
        result = {'phase': phase, **{key: (generation or {}).get(key) for key in ('ref', 'draft_id', 'mode')},
            'raw': raw, 'owner_id': owner_id, 'control_markup': (generation or {}).get('control_markup'),
            'html': render_rich(raw, generating=not complete),
            'fallback': fallback(raw, generating=not complete), 'record_id': record_id,
            'empty': complete and not raw.strip()}
        if owner_id is not None:
            result['auxiliary'] = previous is None or previous.message_id != owner_id
        return result

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def start_command_display(self, chat_id, reply_markup):
        """Show the command's result slot before awaiting the actual process."""
        operation = self.turn.active_operation
        if not operation or operation.get('kind') != 'run':
            return
        with message_presentation({'kind': 'agent_result', 'mode': self.turn.mode, 'transient': True,
                'telegram_operation': {**operation, 'running': True, 'output': '等待命令返回…'}}):
            message = await self.send_message(chat_id=chat_id,
                text=operation_html({'kind': 'run', 'running': True, 'notice': '等待命令返回…'}),
                parse_mode='HTML', reply_markup=reply_markup)
        message.finalized = False
        self.turn.operation_messages[operation['sequence']] = message

    def _telegram_hint(self, hint, kind, previous=None):
        hint = dict(hint)
        display = {'compact_chat': True, **hint.get('telegram_display', {})}
        if previous is not None and previous.telegram_complete:
            display['remove_stop'] = True
        chunks = None
        operation = hint.get('telegram_operation') or self.turn.active_operation
        policy = hint.get('message_policy') or {}
        if (kind == 'agent_result' and operation and operation.get('kind') != 'ask'
                and policy.get('purpose', 'tool_result') == 'tool_result'
                and 'text' not in display and not display.get('skip')):
            from .telegram_chat import output_chunks
            from .web_display import operation_state
            chunks = output_chunks(operation)
            display.update(text=chunks[0], parse_mode='HTML', remove_stop=operation_state(operation) != 'running')
        hint['telegram_display'] = display
        spec = self._native_spec(hint, kind, previous, chunks)
        if spec:
            display['telegram_native'] = spec
            if kind == 'agent_result':
                chunks = None  # Native renderer owns continuations of this output.
        return hint, chunks

    @staticmethod
    def _remember_telegram_body(message, hint, text, parse_mode):
        display = hint.get('telegram_display') or {}
        message.telegram_suppressed = bool(display.get('skip'))
        message.telegram_native = display.get('telegram_native')
        if message.telegram_native:
            message.telegram_owner_id = message.telegram_native.get('owner_id') or message.message_id
        if not message.telegram_suppressed:
            message.telegram_body = display.get('text', text)
            message.telegram_parse_mode = display.get('parse_mode', parse_mode)

    def complete_telegram_reply(self, facts):
        for mid in facts.get('message_ids') or []:
            message = self.messages.get(mid)
            if message is not None:
                message.telegram_complete = True
        generation = self._telegram_generation
        if generation:
            from .stop_targets import end_phase
            end_phase(generation['run_id'], generation['ref'])

    async def finish_telegram_controls(self):
        from .stop_targets import end_phase
        from .conversations import current_scope
        scope = current_scope(required=False)
        if scope:
            end_phase(scope.run_id)
        generation = self._telegram_generation
        if generation:
            end_phase(generation['run_id'], generation['ref'])
        if not self._native_enabled():
            return
        owners = {message.telegram_owner_id or message.message_id: message for message in self.messages.values()
                  if message.telegram_native and message.telegram_native.get('ref')}
        for owner_id, message in owners.items():
            await self._telegram_only('edit_message_reply_markup', native={'phase': 'cleanup'},
                chat_id=message.chat_id, message_id=owner_id, reply_markup=None)

    async def _telegram_only(self, method, *, kind='ai_reply', native=None, record_id=None, **kwargs):
        display = {'compact_chat': True, 'remove_stop': True}
        if native:
            display['telegram_native'] = native
        with message_presentation({'kind': kind, 'mode': self.turn.mode, 'audience': ['telegram'],
                'record_id': record_id, 'telegram_display': display}), chat_display(display):
            return await getattr(self.original, method)(**kwargs)

    async def _update_telegram_tail(self, message, chunks):
        for index, text in enumerate(chunks):
            if index < len(message.telegram_extra_messages):
                extra = message.telegram_extra_messages[index]
                if text != message.telegram_extra_texts[index]:
                    await self._telegram_only('edit_message_text', kind=message.kind,
                        chat_id=message.chat_id, message_id=extra.message_id, text=text, parse_mode='HTML', reply_markup=None)
            else:
                extra = await self._telegram_only('send_message', kind=message.kind,
                    chat_id=message.chat_id, text=text, parse_mode='HTML', reply_markup=None)
                message.telegram_extra_messages.append(extra)
        for extra in message.telegram_extra_messages[len(chunks):]:
            await self._telegram_only('delete_message', kind=message.kind,
                chat_id=message.chat_id, message_id=extra.message_id)
        del message.telegram_extra_messages[len(chunks):]
        message.telegram_extra_texts = list(chunks)

    async def finish_telegram_reply(self, row, facts, summary=None):
        """Append once to the last AI reply; all edits stay on Telegram only."""
        from .telegram_chat import append_details, reply_details
        from .protocols import ProtocolParser
        import html

        details = reply_details(row, facts, summary)
        if self._native_enabled():
            for mid in reversed(facts.get('message_ids') or []):
                media = self.messages.get(mid)
                if media is not None and getattr(media, 'media', False) and media.record_id == row['id']:
                    await self._telegram_only('finalize_generated_group', chat_id=media.chat_id, message_id=mid,
                        artifacts=media.media_payload['artifacts'], text=media.media_payload['text'],
                        details=''.join(details), target_record_id=row['id'], record_id=row['id'])
                    return {'version': 1, 'record_id': row['id'], 'message_ids': [mid], 'details': details, 'native_format': 'rich_media'}
            from .telegram_rich import render_rich, legacy_chunks
            from .telegram_chat import append_details
            ids = facts.get('message_ids') or []
            owner_id = ids[0] if ids else None
            message = self.messages.get(owner_id)
            if message is not None and not getattr(message, 'media', False):
                from telegram import InlineKeyboardButton,InlineKeyboardMarkup,CopyTextButton
                rows=[]
                if 0<len(row['content'])<=256:
                    rows.append([InlineKeyboardButton('复制',copy_text=CopyTextButton(row['content']))])
                buttons=InlineKeyboardMarkup(rows) if rows else None
                native = {'phase': 'reply', 'owner_id': owner_id, 'record_id': row['id'],
                          'html': render_rich(row['content'], details=''.join(details)),
                          'fallback': []}
                fallback = legacy_chunks(row['content'])
                native['fallback'] = [*fallback[:-1], *append_details(fallback[-1], details)]
                # Footer retries edit the same native parts, never append again.
                await self._telegram_only('edit_message_text', native=native, record_id=row['id'],
                    chat_id=message.chat_id, message_id=owner_id, text=message.telegram_body,
                    parse_mode='HTML', reply_markup=buttons)
                return {'version': 1, 'record_id': row['id'], 'message_ids': [owner_id], 'details': details,
                        'native_format': 'rich'}
        saved = self._telegram_final_replies.get(row['id'])
        if saved is None:
            ids = facts.get('message_ids') or []
            target_id = facts.get('footer_message_id')
            message = self.messages.get(target_id) if target_id is not None else next(
                (self.messages[mid] for mid in reversed(ids) if mid in self.messages
                 and not self.messages[mid].telegram_suppressed), None)
            if message is None or message.hidden or getattr(message, 'media', False):
                # Media captions have their own 1024-character limit. A detail
                # continuation remains attached to this actual AI reply.
                original = await self._telegram_only('send_message', chat_id=getattr(self.original, 'chat_id', None) if message is None else message.chat_id,
                    text=details[0], parse_mode='HTML', reply_markup=None)
                message = PresentationMessage(self, original, '', 'HTML')
                message.telegram_body = details[0]
                base = ''
            else:
                base = message.telegram_body
                if 'html' not in str(message.telegram_parse_mode).lower():
                    base = html.escape(base)
            saved = self._telegram_final_replies[row['id']] = (message, base)
        message, base = saved
        chunks = append_details(base, details)
        if ProtocolParser.to_telegram_html(message.telegram_body) != chunks[0]:
            await self._telegram_only('edit_message_text', chat_id=message.chat_id,
                message_id=message.message_id, text=chunks[0], parse_mode='HTML', reply_markup=None)
            message.telegram_body = chunks[0]
            message.telegram_parse_mode = 'HTML'
        await self._update_telegram_tail(message, chunks[1:])
        return {'version': 1, 'record_id': row['id'], 'message_ids': [message.message_id,
                *[extra.message_id for extra in message.telegram_extra_messages]], 'details': details}

    def remember_notice(self, text):
        if any(word in str(text) for word in ('停止', '失败', '超过', '超限')):
            self.turn.reason = ('⏹️ 已停止。' if '停止' in str(text) else
                                '⚠️ 已达到执行上限。' if any(word in str(text) for word in ('超过', '超限')) else
                                '⚠️ 本次处理失败，已有结果已保留。')

    def _kind(self, text):
        explicit = current_message_presentation()
        if explicit:
            if explicit.get('kind') == 'runtime_error':
                self.turn.reason = '⚠️ 本次处理失败，已有结果已保留。'
            return explicit.get('kind', 'ai_reply')
        from .web_display import current_web_display
        if current_web_display() is not None:
            return 'ai_reply'
        if self.turn.active_reply is not None:
            return 'ai_reply'
        return 'agent_result' if self.turn.active_operation is not None else 'ai_reply'

    def _prepare(self, text, kind, markup, parse_mode=None):
        text = str(text)
        if not self.turn.folded:
            return text
        if kind in {'runtime_error','ai_reply','media_reply'}:
            return text
        from .message_policy import current_policy
        policy = current_policy()
        if policy is not None and policy.purpose in {'chat', 'media', 'generation'}:
            return text
        if kind == 'agent_status':
            self.remember_notice(text)
            if policy is not None and policy.transient:
                return None
            caption = compact_agent_round_status(text)
            if caption:
                return caption
            return text
        op = self.turn.active_operation
        # A form is actionable UI, not an output preview.
        if (kind == 'agent_result' and op and op['kind'] != 'ask' and not markup
                and (policy is None or policy.purpose == 'tool_result')
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
        if self._telegram_generation and (current_message_presentation() or {}).get('purpose') == 'generation':
            from .web_bridge import _markup_to_frame
            self._telegram_generation['control_markup'] = _markup_to_frame(reply_markup)
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
        explicit = current_message_presentation() or {}
        hint = ({**explicit} if explicit.get('kind') == kind else {})
        hint.update(kind=kind, mode=self.turn.mode)
        transient = bool((current_message_presentation() or {}).get('transient'))
        record_ids = getattr(self.turn, 'record_ids', {})
        if not transient and kind in {'agent_result', 'runtime_error', 'presentation_note'} and record_ids.get(kind) == self.turn.last_record_id:
            hint['record_id'] = self.turn.last_record_id
        elif (kind == 'agent_status' and record_ids.get(kind)
              and getattr(self.turn, 'record_contents', {}).get(kind) == str(text)):
            hint['record_id'] = record_ids[kind]
        hint, telegram_chunks = self._telegram_hint(hint, kind)
        progress = self.turn.operation_messages.get(op.get('sequence')) if op else None
        if (kind == 'agent_result' and progress is not None and not progress.finalized
                and not transient and (not explicit or explicit['message_policy']['purpose'] == 'tool_result')):
            progress.record_id = hint.get('record_id')
            result = await self.edit_message_text(prepared, chat_id=chat_id,
                message_id=progress.message_id, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
            progress.finalized = True
            return result
        with message_presentation(hint), chat_display(hint['telegram_display']):
            original = await self.original.send_message(chat_id=chat_id, text=prepared,
                reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
        message = PresentationMessage(self, original, prepared, parse_mode, kind)
        from .message_policy import new_policy
        message.policy = new_policy(kind, hint=hint)
        message.record_id = hint.get('record_id')
        message.reply_markup = reply_markup
        self._remember_telegram_body(message, hint, prepared, parse_mode)
        self.messages[message.message_id] = message
        self.turn.last_message = message
        if telegram_chunks is not None:
            await self._update_telegram_tail(message, telegram_chunks[1:])
        return message

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                                reply_markup=None, parse_mode=None, **kwargs):
        previous = self.messages.get(message_id)
        explicit = current_message_presentation() or {}
        kind = ('runtime_error' if explicit.get('kind') == 'runtime_error' else
                previous.kind if previous is not None else self._kind(text))
        prepared = self._prepare(text, kind, reply_markup, parse_mode)
        if prepared is None:
            return previous
        hint = ({**explicit} if explicit.get('kind') == kind else {})
        hint.update(kind=kind, mode=self.turn.mode)
        if previous is not None and previous.kind == kind and previous.record_id is not None:
            hint['record_id'] = previous.record_id
        hint, telegram_chunks = self._telegram_hint(hint, kind, previous)
        with message_presentation(hint), chat_display(hint['telegram_display']):
            original = await self.original.edit_message_text(prepared, chat_id=chat_id,
                message_id=message_id, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
        if previous is None:
            previous = PresentationMessage(self, original, prepared, parse_mode, kind)
            self.messages[message_id] = previous
        previous.text, previous.parse_mode = prepared, parse_mode
        previous.kind, previous.record_id = kind, hint.get('record_id')
        previous.reply_markup = reply_markup
        from .message_policy import new_policy
        previous.policy = new_policy(kind, hint=hint)
        previous.base_text = prepared
        self._remember_telegram_body(previous, hint, prepared, parse_mode)
        self.turn.last_message = previous
        if telegram_chunks is not None:
            await self._update_telegram_tail(previous, telegram_chunks[1:])
        return previous

    async def edit_message_reply_markup(self, **kwargs):
        message = self.messages.get(kwargs.get('message_id'))
        hint = {'kind': message.kind if message else 'ai_reply', 'mode': self.turn.mode}
        if message is not None and message.policy is not None:
            hint['message_policy'] = message.policy.to_dict()
        if message is not None and message.record_id is not None:
            hint['record_id'] = message.record_id
        hint, _ = self._telegram_hint(hint, hint['kind'], message)
        # Keyboard-only updates never rewrite a command's text.
        hint['telegram_display'].pop('text', None)
        hint['telegram_display'].pop('parse_mode', None)
        with message_presentation(hint), chat_display(hint['telegram_display']):
            result = await self.original.edit_message_reply_markup(**kwargs)
        if message is not None:
            message.reply_markup = kwargs.get('reply_markup')
        return result

    async def delete_message(self, chat_id=None, message_id=None, **kwargs):
        result = await self.original.delete_message(chat_id=chat_id, message_id=message_id, **kwargs)
        message = self.messages.pop(message_id, None)
        if message is not None and message.telegram_extra_messages:
            await self._update_telegram_tail(message, [])
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
