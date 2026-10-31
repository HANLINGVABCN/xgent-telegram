"""Display-only submitted forms; private values are resolved at Telegram delivery."""
import html

from .agent_ask import SECRET_STORE
from .protocols import _telegram_units


def build(pending):
    parts = []
    labels = {'single': '单选', 'multi': '多选', 'text': '填写', 'secret': '密钥'}
    for index, question in enumerate(pending.form.questions):
        answer = pending.draft.get(question.qid) or {}
        parts.append(f'{question.label or "Q" + str(index + 1)} · {labels[question.qtype]}\n{question.prompt}\n')
        selected = set(answer.get('selected') or [])
        for number, option in enumerate(question.options):
            parts.append(('✅ ' if number in selected else '▫️ ') + option + '\n')
        if question.qtype == 'secret':
            version = SECRET_STORE.version(pending.conversation_id, question.var)
            if answer.get('filled') and version:
                parts.extend(['✅ $' + question.var + '：', {'variable': question.var, 'version': version}])
                parts.append('\n')
            else:
                parts.append('（密钥明文已不可用）\n')
        elif question.qtype == 'text':
            parts.append('✅ 答案：' + str(answer.get('custom') or '（未作答）') + '\n')
        elif answer.get('custom'):
            parts.append('✅ 自定义：' + answer['custom'] + '\n')
        parts.append('\n')
    refs = {}
    rich = _chunks(parts, pending.conversation_id, refs, rich=True)
    fallback = _chunks(parts, pending.conversation_id, refs, rich=False)
    return {'phase': 'reply', 'form_receipt': True, 'record_id': pending.form_record_id,
            'html': rich, 'fallback': fallback, 'secret_references': refs,
            'secret_conversation_id': pending.conversation_id}


def _chunks(parts, conversation_id, refs, *, rich):
    opening = ('<details><summary>查看问题与答案</summary><p>' if rich else '<blockquote expandable>')
    closing = '</p></details>' if rich else '</blockquote>'
    title = '<p>✅ 表单已提交</p>' if rich else '✅ 表单已提交\n'
    limit = 30000 if rich else 3800
    chunks, current = [], []
    used = _telegram_units(title + opening + closing)
    values = SECRET_STORE.environment(conversation_id)
    def encode(value):
        result = html.escape(value, quote=False)
        return result.replace('\n', '<br>') if rich else result
    def flush():
        nonlocal used
        chunks.append((title if not chunks else '') + opening + ''.join(current) + closing)
        current.clear()
        used = _telegram_units(opening + closing)
    for part in parts:
        if isinstance(part, str):
            for char in part:
                encoded = encode(char)
                width = _telegram_units(encoded)
                if used + width > limit:
                    flush()
                current.append(encoded); used += width
            continue
        value = values.get(part['variable'], '')
        start = 0
        while start < len(value):
            end, width = start, 0
            while end < len(value):
                size = _telegram_units(encode(value[end]))
                if used + width + size > limit:
                    break
                width += size; end += 1
            if end == start:
                flush(); continue
            token = '__private_' + part['version'] + ('_r_' if rich else '_l_') + str(start) + '_' + str(end) + '__'
            refs[token] = {**part, 'start': start, 'end': end, 'rich': rich}
            current.append(token); used += width; start = end
    if current:
        flush()
    return chunks


def materialize(body, spec, op):
    """Never write the returned private string back into an Op or display record."""
    policy = op.payload.get('_message_policy') or {}
    cid = spec.get('secret_conversation_id')
    if (not spec.get('form_receipt') or cid != (op.conversation_context or {}).get('conversation_id')
            or policy.get('purpose') != 'form' or policy.get('model_context') is not False
            or policy.get('audience') != ['telegram']):
        return body
    values = SECRET_STORE.environment(cid)
    for token, reference in (spec.get('secret_references') or {}).items():
        if token not in body:
            continue
        var = reference['variable']
        if SECRET_STORE.version(cid, var) != reference['version'] or var not in values:
            replacement = '（密钥已更新或清理，原明文不再保留）'
        else:
            replacement = html.escape(values[var][reference['start']:reference['end']], quote=False)
            if reference['rich']:
                replacement = replacement.replace('\n', '<br>')
        body = body.replace(token, replacement)
    return body
