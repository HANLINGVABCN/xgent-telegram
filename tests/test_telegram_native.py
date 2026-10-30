"""Native previews are ephemeral; formal sends and retry identities are durable."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter

from tests.telegram_rich_probe import RichCapture
from xgent_app.fanout import ChannelWorker, Op, OpTransientError
from xgent_app.stop_targets import active_target, begin_phase, end_phase
from xgent_app.telegram_native import OP_NATIVE_CONTROL, OP_NATIVE_DRAFT, OP_NATIVE_RENDER
from xgent_app.web_bridge import deliver_op_to_bot


def operation(kind, ref, *, html=None, durable=True):
    return Op(kind, logical_id=1000001, chat_id=31, durable=durable,
              payload={'text':'前台流式输出中…', 'telegram_native': {'phase':'reply', 'ref':ref,
                'draft_id':901, 'html': html or ['<p>回答</p>'], 'fallback':['<b>回答</b>'],
                'control_markup': [[{'text':'❌ 停止回答','callback_data':'act_stop_generation:run:'+str(ref)}]]},
                '_conversation_context':{'run_id':'run','conversation_id':'c','generation':1}})


def test_status_and_draft_never_bind_the_formal_id_and_finalization_is_idempotent():
    async def main():
        native=RichCapture();ref=begin_phase('run','model')
        control=operation(OP_NATIVE_CONTROL,ref,durable=False)
        assert await deliver_op_to_bot(native,control,None) is None
        preview=operation(OP_NATIVE_DRAFT,ref,durable=False)
        assert await deliver_op_to_bot(native,preview,None) is None
        assert len(native.drafts)==1 and not any(m.get('rich') for m in native.messages.values())
        end_phase('run',ref)
        final=operation(OP_NATIVE_RENDER,ref)
        mid=await deliver_op_to_bot(native,final,None)
        assert len(native.messages)==1 and native.messages[mid]['rich']
        assert await deliver_op_to_bot(native,final,mid)==mid
        assert len(native.messages)==1
        assert sum(method=='sendRichMessage' for method,_ in native.api_calls)==1
    asyncio.run(main())


def test_stale_controls_do_not_stop_next_phase_and_draft_recovers_missing_control():
    async def main():
        native=RichCapture();old=begin_phase('run','model');current=begin_phase('run','operation')
        assert not active_target('run',old) and active_target('run',current)
        assert await deliver_op_to_bot(native,operation(OP_NATIVE_CONTROL,old),None) is None
        assert not native.messages
        await deliver_op_to_bot(native,operation(OP_NATIVE_DRAFT,current,durable=False),None)
        assert len(native.messages)==1
        controls=next(iter(native.messages.values()))['reply_markup']
        assert controls.inline_keyboard[0][0].text=='❌ 停止回答'
        end_phase('run',current)
        previous_calls=len(native.api_calls)
        await deliver_op_to_bot(native,operation(OP_NATIVE_DRAFT,current,durable=False),None)
        assert len(native.api_calls)==previous_calls
    asyncio.run(main())


def test_known_unsupported_api_uses_complete_fallback_but_network_error_does_not():
    async def main():
        native=RichCapture();original=native._post
        async def legacy(method,data=None,**kwargs):
            if method=='sendRichMessage':raise BadRequest('Method not found')
            return await original(method,data,**kwargs)
        native._post=legacy
        op=operation(OP_NATIVE_RENDER,None)
        mid=await deliver_op_to_bot(native,op,None)
        assert native.messages[mid]['text']=='<b>回答</b>' and not native.messages[mid]['rich']
        failing=RichCapture()
        failing._post=AsyncMock(side_effect=NetworkError('temporary failure'))
        with pytest.raises(OpTransientError):
            await deliver_op_to_bot(failing,operation(OP_NATIVE_RENDER,None),None)
        assert failing._post.call_args.args[0]=='sendRichMessage'
        assert failing._post.await_count==1
    asyncio.run(main())


def test_partial_formal_send_retry_reuses_already_delivered_parts():
    async def main():
        native=RichCapture();original=native._post;failed=False
        async def second_fails(method,data=None,**kwargs):
            nonlocal failed
            if method=='sendRichMessage' and data['rich_message']['html']=='<p>二</p>' and not failed:
                failed=True;raise NetworkError('lost connection')
            return await original(method,data,**kwargs)
        native._post=second_fails
        op=operation(OP_NATIVE_RENDER,None,html=['<p>一</p>','<p>二</p>'])
        with pytest.raises(OpTransientError):await deliver_op_to_bot(native,op,None)
        assert len(native.messages)==1 and len(op.payload['_native_parts'])==1
        retry=Op.from_row(op.to_row())
        await deliver_op_to_bot(native,retry,None)
        assert len(native.messages)==2
        assert sum(method=='sendRichMessage' for method,_ in native.api_calls)==2
    asyncio.run(main())


def test_old_previews_are_not_replayed_and_draft_429_is_not_persisted():
    class Store:
        async def count(self,*args):return 0
        async def fetch(self,*args,**kwargs):return []
        def __init__(self):self.append=AsyncMock()
    async def main():
        native=RichCapture();ref=begin_phase('run','model')
        stale=operation(OP_NATIVE_DRAFT,ref,durable=False);stale.created_at=time.time()-31
        await deliver_op_to_bot(native,stale,None)
        assert not native.api_calls
        native._post=AsyncMock(side_effect=RetryAfter(1));store=Store()
        async def send(op,mid):return await deliver_op_to_bot(native,op,mid)
        channel=ChannelWorker('draft-test',send,is_configured=lambda:True,store=store)
        await channel._handle(operation(OP_NATIVE_DRAFT,ref,durable=False))
        assert not store.append.await_count
        end_phase('run',ref)
    asyncio.run(main())
