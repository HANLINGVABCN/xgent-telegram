"""Fixed message intent, not body wording, controls transport and model history."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xgent_app.message_policy import MessagePolicy, new_policy, record_policy
from xgent_app.presentation import message_presentation, current_message_presentation
from xgent_app.web_history import build_history_message
from xgent_app.conversation_search import visible_text


def test_contract_is_fixed_and_does_not_inherit_mutated_caller_state():
    hint={'kind':'ai_reply','audience':['web']}
    with message_presentation(hint):
        hint['audience'].append('telegram')
        seen=current_message_presentation()
        assert seen['message_policy']['audience']==['web']
        seen['message_policy']['audience'].append('cli')
        assert current_message_presentation()['message_policy']['audience']==['web']
    assert current_message_presentation() is None


@pytest.mark.parametrize('body',['请填写：新文案','✅ Agent 第 3 轮 · 已完成',
                               '🔒 已录入变量 $PRIVATE，任意未来文案','[用户已提交表单]'])
def test_explicit_chat_policy_preserves_user_and_model_prose_even_with_status_words(body,tmp_path):
    for kind,role in [('user_text','user'),('ai_reply','assistant')]:
        policy=new_policy(kind)
        row={'id':1,'role':role,'msg_type':kind,'content':body,
             'metadata':{'message_policy':policy.to_dict()}}
        display=build_history_message(row,tmp_path,tmp_path)
        assert not display.get('hidden')
        assert body in display['content']
        assert body in visible_text(kind,role,body,row['metadata'])


@pytest.mark.parametrize('kind,purpose,context',[('agent_result','tool_result',True),
    ('agent_status','status',False),('ui_message','menu',False),('token_usage','usage',False)])
def test_registry_defaults_define_speaker_purpose_and_context(kind,purpose,context):
    policy=new_policy(kind,origin='web')
    assert policy.purpose==purpose and policy.model_context is context
    assert MessagePolicy.from_dict(policy.to_dict())==policy


def test_field_notices_and_hidden_model_answers_have_separate_audiences(tmp_path):
    policy=new_policy('ask_input_notice',origin='telegram',hint={'form_id':'form-a','field_id':'q2'})
    assert policy.audience==('telegram',) and not policy.model_context
    assert (policy.form_id,policy.field_id)==('form-a','q2')
    row={'id':1,'role':'user','msg_type':'user_text','content':'测试',
         'metadata':{'message_policy':new_policy('user_text',metadata={'ask_answer':True}).to_dict()}}
    assert build_history_message(row,tmp_path,tmp_path)['hidden']
    assert record_policy(row).model_context
    assert visible_text('user_text','user','测试',row['metadata'])==''


def test_invalid_or_missing_contract_does_not_guess_a_more_permissive_type(tmp_path):
    for raw in (None,{}, {'version':2},new_policy('user_text').to_dict()|{'audience':['unknown']}):
        meta={} if raw is None else {'message_policy':raw}
        row={'id':1,'role':'assistant','msg_type':'ai_reply','content':'ambiguous', 'metadata':meta}
        assert build_history_message(row,tmp_path,tmp_path)['hidden']
        assert not record_policy(row).model_context


def test_native_web_only_message_does_not_become_a_telegram_notification():
    from xgent_app.fanout import Op, OP_SEND
    from xgent_app.web_bridge import deliver_op_to_bot
    bot=SimpleNamespace(send_message=AsyncMock())
    with message_presentation({'kind':'ai_reply','audience':['web']}):
        row=Op(OP_SEND,logical_id=1,payload={'text':'Web only'}).to_row()
    restored=Op.from_row(row)
    asyncio.run(deliver_op_to_bot(bot,restored,None))
    bot.send_message.assert_not_awaited()


def test_transient_generation_does_not_enter_model_history():
    policy=new_policy('ai_reply',origin='web',hint={'purpose':'generation','transient':True})
    assert not policy.model_context and policy.allows('web')
    with pytest.raises(ValueError):
        replace(policy,model_context=True)


def test_cli_relay_stamps_plain_producers_before_persisting_the_envelope():
    from xgent_app import cli_bridge
    writer = cli_bridge._CliRelay()
    writer._enabled = True
    try:
        cli_bridge.set_turn_kind('cmd')
        writer.emit('send_message', text='请填写：这是命令输出')
        op, payload = writer._queue.get_nowait()
        assert op == 'send_message'
        policy = MessagePolicy.from_dict(payload['message_presentation']['message_policy'])
        assert policy.purpose == 'tool_result' and policy.speaker == 'tool'
        assert policy.origin == 'cli' and policy.allows('web')
        writer.emit('user_echo', text='用户的输入')
        _, payload = writer._queue.get_nowait()
        policy = MessagePolicy.from_dict(payload['message_presentation']['message_policy'])
        assert policy.purpose == 'chat' and policy.speaker == 'user'
    finally:
        cli_bridge.set_turn_kind('chat')
