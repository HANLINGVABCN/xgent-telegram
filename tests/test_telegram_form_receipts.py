import html
import json
import re

from xgent_app.agent_ask import PendingAsk, SECRET_STORE, parse_ask_form
from xgent_app.conversations import ConversationScope, bind_conversation
from xgent_app.fanout import Op
from xgent_app.presentation import message_presentation
from xgent_app.protocols import _telegram_units
from xgent_app.telegram_form_receipts import build, materialize
from xgent_app.telegram_native import OP_NATIVE_RENDER


def test_receipt_has_complete_questions_checked_answers_and_private_delivery_only():
    form, error = parse_ask_form('Q1|single|完整问题\n- 第一项\n- 第二项\n+other\n'
                               'Q2|multi|多选问题\n- 苹果\n- 香蕉\nQ3|text|备注\nQ4|secret:RECEIPT_KEY|密钥')
    assert not error
    secret = 'PRIVATE<&>RECEIPT-VALUE'
    SECRET_STORE.set('receipt-test', 'RECEIPT_KEY', secret)
    try:
        pending = PendingAsk('receipt', form, 7, [], 1, 1, conversation_id='receipt-test',
            draft={'0': {'selected': [1]}, '1': {'selected': [0, 1]}, '2': {'custom': '完整\n备注'}, '3': {'filled': True}})
        spec = build(pending)
        assert secret not in json.dumps(spec) and html.escape(secret) not in json.dumps(spec)
        with bind_conversation(ConversationScope('receipt-test', 7)), message_presentation({
            'kind': 'agent_result', 'purpose': 'form', 'model_context': False, 'audience': ['telegram']}):
            op = Op(OP_NATIVE_RENDER, chat_id=1, logical_id=1, payload={'telegram_native': spec})
        assert secret not in json.dumps(op.to_row())
        rendered = materialize(spec['html'][0], spec, op)
        assert '<details><summary>查看问题与答案</summary>' in rendered
        assert '<details open' not in rendered
        text = html.unescape(rendered.replace('<br>', '\n'))
        assert '完整问题' in text and '▫️ 第一项' in text and '✅ 第二项' in text
        assert '✅ 苹果' in text and '✅ 香蕉' in text and '完整\n备注' in text
        assert '✅ $RECEIPT_KEY：' + secret in text
        assert secret not in form.assemble_answer(pending.draft)
        op.payload['_message_policy']['model_context'] = True
        assert secret not in html.unescape(materialize(spec['html'][0], spec, op))
        op.payload['_message_policy']['model_context'] = False
        SECRET_STORE.set('receipt-test', 'RECEIPT_KEY', 'replacement')
        changed = html.unescape(materialize(spec['html'][0], spec, op))
        assert secret not in changed and 'replacement' not in changed and '密钥已更新' in changed
    finally:
        SECRET_STORE.purge('receipt-test')


def test_long_private_answer_is_split_safely_without_persisting_or_losing_plaintext():
    form, _ = parse_ask_form('Q1|secret:LONG_KEY|完整密钥')
    secret = '<&' * 5000
    SECRET_STORE.set('long-receipt', 'LONG_KEY', secret)
    try:
        pending = PendingAsk('long', form, 7, [], 1, 1, conversation_id='long-receipt', draft={'0': {'filled': True}})
        spec = build(pending)
        with bind_conversation(ConversationScope('long-receipt', 7)), message_presentation({
            'kind': 'agent_result', 'purpose': 'form', 'model_context': False, 'audience': ['telegram']}):
            op = Op(OP_NATIVE_RENDER, chat_id=1, logical_id=1, payload={'telegram_native': spec})
        assert len(spec['html']) > 1 and secret not in json.dumps(op.to_row())
        for rich, limit in ((True, 30000), (False, 3800)):
            chunks = [materialize(part, spec, op) for part in spec['html' if rich else 'fallback']]
            assert all(_telegram_units(part) <= limit for part in chunks)
            bodies = [re.search(r'<details><summary>.*?</summary><p>(.*?)</p></details>' if rich else
                                r'<blockquote expandable>(.*?)</blockquote>', part, re.S)[1] for part in chunks]
            assert secret in html.unescape(''.join(bodies).replace('<br>', '\n'))
    finally:
        SECRET_STORE.purge('long-receipt')


def test_actual_submission_edits_same_bot_message_and_keeps_secret_out_of_history(tmp_path):
    from tests.test_thinking_params import run_in_app
    result = run_in_app('import asyncio\nfrom tests.telegram_form_receipt_probe import exercise\n' +
                        f'print(json.dumps(asyncio.run(exercise(bot,{str(tmp_path)!r}))))')
    assert result['rich'] and result['legacy']
