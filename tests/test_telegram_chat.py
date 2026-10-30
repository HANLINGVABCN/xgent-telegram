"""Telegram-only formatting preserves existing preview budgets and source data."""
import asyncio
import html
import io
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from xgent_app.protocols import ProtocolParser, _telegram_units
from xgent_app.presentation import message_presentation
from xgent_app.telegram_chat import append_details, output_chunks, reply_details
from xgent_app.telegram_presentation import chat_display, chat_payload, presentation_snapshot, replay_presentation


@pytest.mark.parametrize('kind,protocol', [('run','run-x'),('file','file-x'),('file_base64','file-x'),
    ('read','read-x'),('shell','shell-x'),('stdin','stdin-x'),('shellkill','shellkill-x')])
def test_output_header_uses_actual_protocol_and_result(kind, protocol):
    output = output_chunks({'kind':kind,'success':True,'output':'失败是这里的普通文字'})[0]
    assert output.startswith('<blockquote expandable><b>🟢 '+protocol+' · 输出</b> · 1 行')
    assert '<pre>' not in output and 'data-' not in output
    failed = output_chunks({'kind':kind,'success':False,'output':'成功是这里的普通文字'})[0]
    assert '<b>🔴 '+protocol+' · 输出 · 失败</b>' in failed
    stopped = output_chunks({'kind':kind,'state':'stopped','success':False,'output':'already received'})[0]
    assert '<b>⚪ '+protocol+' · 输出 · 已停止</b>' in stopped


def test_preview_and_path_order_keep_the_same_50_line_character_limits():
    body = '\n'.join('  line '+str(index) for index in range(60))
    path = '/home/hanling/xgent_storage/command_outputs/test.txt'
    output = output_chunks({'kind':'run','success':True,'output':body,'output_path':path})[0]
    preview, note = ProtocolParser._telegram_preview(body)
    assert preview + note + ' · 存档：' + path + '</blockquote>' in output
    assert '  line 49' in output and '  line 50' not in output
    assert ProtocolParser._FOLD_MAX_LINES == 50 and ProtocolParser._FOLD_MAX_CHARS == 3200
    assert _telegram_units(output) <= 3900
    short = output_chunks({'kind':'run','success':True,'output':'two  spaces','output_path':path})[0]
    assert 'two  spaces\n存档：'+path+'</blockquote>' in short


def test_exceptional_long_path_preserves_preview_and_full_path_without_broken_entities():
    body = '\n'.join('line'+str(index) for index in range(80))
    path = '/home/' + ('😀<& directory/'*600) + 'last.txt'
    chunks = output_chunks({'kind':'run','success':True,'output':body,'output_path':path})
    assert len(chunks) > 1
    assert 'line49' in chunks[0] and '已折叠 30 行' in chunks[0]
    plain = ''.join(html.unescape(re.sub(r'<[^>]+>', '', chunk)) for chunk in chunks[1:])
    assert plain == '存档：'+path
    assert all(_telegram_units(chunk) <= 3900 for chunk in chunks)
    assert all(chunk.count('<blockquote expandable>') == chunk.count('</blockquote>') == 1 for chunk in chunks)


def test_final_details_use_last_reply_and_missing_values_remain_explicit():
    row = {'timestamp':1700000000, 'metadata':{'ai_generation':{'provider':'P<&','model':'actual-model','generated_at':1700000000}}}
    facts = {'model':'incorrect-fallback','round':3,'usage':{'input_tokens':333,'output_tokens':33},'elapsed_seconds':2}
    summary = {'rounds':3,'operations':2,'elapsed_seconds':5,'reason':'stopped'}
    details = reply_details(row, facts, summary)[0]
    assert details.startswith('<blockquote expandable><b>回复详情</b>\n')
    assert '模型：P&lt;&amp; / actual-model' in details
    assert '时间：2023-11-15 06:13:20' in details
    assert '↑ 333 · ↓ 33 · cache 0 · 约 2.0 秒' in details
    assert '共执行 3 轮，2 个操作' in details and 'stopped' not in details
    missing = reply_details({'metadata':{}}, {})[0]
    assert '模型：未提供' in missing and '时间：未记录' in missing
    assert '↑ 未提供 · ↓ 未提供 · cache 0 · 耗时未记录' in missing


def test_details_overflow_becomes_only_the_final_continuation():
    details = reply_details({'timestamp':1700000000,'metadata':{}}, {'usage':{'input_tokens':1}})
    assert len(append_details('body', details)) == 1
    chunks = append_details('a'*3850, details)
    assert chunks == ['a'*3850, *details]
    assert sum(chunk.count('<b>回复详情</b>') for chunk in chunks) == 1
    assert all(_telegram_units(chunk) <= 3900 for chunk in chunks)


def test_telegram_payload_changes_neither_caller_text_nor_shared_keyboard():
    payload = {'text':'Shared footer','parse_mode':'HTML','reply_markup':[[
        {'text':'完整输出','callback_data':'present_output:1'},
        {'text':'用量详情','callback_data':'present_usage:1:1'},
        {'text':'❌ 停止回答','callback_data':'id-1','callback_action':'act_stop_generation:run'},
        {'text':'专属操作','callback_data':'own-action'}]]}
    hint = {'kind':'ai_reply','telegram_display':{'compact_chat':True,'remove_stop':True,'text':'Body only'}}
    with message_presentation(hint):
        snapshot = presentation_snapshot()
        native = chat_payload(payload)
    assert native['text'] == 'Body only'
    assert native['reply_markup'] == [[payload['reply_markup'][0][-1]]]
    assert len(payload['reply_markup'][0]) == 4 and payload['text'] == 'Shared footer'
    # A worker may inherit an unrelated producer hint. The queued snapshot wins.
    with message_presentation({'kind':'ai_reply','telegram_display':{'text':'unrelated'}}):
        with replay_presentation(snapshot):
            assert chat_payload(payload) == native
        with replay_presentation({'mode':'auto'}):
            assert chat_payload(payload) == payload
        with replay_presentation(snapshot), chat_display({'text':'new producer'}):
            assert chat_payload(payload)['text'] == 'new producer'


def test_bot_only_details_do_not_render_in_cli_or_create_web_frames():
    from xgent_app import cli_bridge
    from xgent_app.cli_render import TerminalScreen
    from xgent_app.web_bridge import WebBot, WebOutbox

    async def scenario():
        stream = io.StringIO()
        screen = TerminalScreen(stream=stream,color=False,width=80)
        relay = cli_bridge._CliRelay();relay._enabled = True
        outbox = WebOutbox()
        with patch.object(cli_bridge, '_RELAY', relay), message_presentation({'kind':'ai_reply','audience':['telegram']}):
            await cli_bridge.CliBot(31,screen).send_message(chat_id=31,text='private detail')
            web = WebBot(outbox,31)
            await web.send_message(chat_id=31,text='private detail')
            await web.edit_message_text('private detail',chat_id=31,message_id=1)
            await web.delete_message(chat_id=31,message_id=1)
        assert stream.getvalue() == ''
        assert not outbox.snapshot()['frames']
        _, payload = relay._queue.get_nowait()
        assert payload['message_presentation']['message_policy']['audience'] == ['telegram']
    asyncio.run(scenario())


def test_native_bot_boundary_keeps_web_text_and_filters_bot_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from xgent_app.web_bridge import WebOutbox, install_tg_to_web_mirror

    class Native:
        def __init__(self):self.calls=[]
        async def send_message(self,chat_id,text,reply_markup=None,**kwargs):
            self.calls.append((text,reply_markup))
            return SimpleNamespace(chat_id=chat_id,message_id=7)
        async def edit_message_reply_markup(self,chat_id,message_id,reply_markup=None,**kwargs):
            self.calls.append(('markup',reply_markup));return True

    async def scenario():
        native, outbox = Native(), WebOutbox()
        restore = install_tg_to_web_mirror(native,outbox)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton('完整输出',callback_data='present_output:1'),
            InlineKeyboardButton('❌ 停止回答',callback_data='act_stop_generation:run')]])
        try:
            with message_presentation({'kind':'ai_reply','telegram_display':{'text':'body only','compact_chat':True,'remove_stop':True}}):
                await native.send_message(chat_id=31,text='shared footer',reply_markup=markup)
                await native.edit_message_reply_markup(chat_id=31,message_id=7,reply_markup=markup)
            assert native.calls == [('body only',None),('markup',None)]
            assert outbox.snapshot()['frames'][0]['text']=='shared footer'
        finally:restore()
    asyncio.run(scenario())


def test_media_caption_is_not_changed_when_final_detail_needs_a_continuation():
    from tests.telegram_chat_probe import TelegramCapture
    from xgent_app.presentation import PresentationTurn
    from xgent_app.presentation_delivery import PresentationBot, PresentationMessage

    async def scenario():
        native = TelegramCapture()
        bot = PresentationBot(native,PresentationTurn('folded'))
        media = PresentationMessage(bot,SimpleNamespace(chat_id=31,message_id=90),'caption '*120,None,'media_reply')
        media.media=True
        bot.messages[90]=media
        row={'id':8,'chat_id':31,'timestamp':1700000000,'metadata':{}}
        facts={'message_ids':[90],'usage':{'input_tokens':12,'output_tokens':2}}
        first = await bot.finish_telegram_reply(row,facts)
        calls = len(native.calls)
        second = await bot.finish_telegram_reply(row,facts)
        assert first==second
        assert len(native.calls)==calls
        assert media.base_text=='caption '*120
        assert len(native.messages)==1
        assert next(iter(native.messages.values()))['text'].count('<b>回复详情</b>')==1
    asyncio.run(scenario())


def test_replayed_identical_telegram_edit_is_successful_and_does_not_create_a_message():
    from telegram.error import BadRequest
    from unittest.mock import AsyncMock
    from xgent_app.fanout import Op, OP_EDIT, OP_EDIT_MARKUP
    from xgent_app.web_bridge import deliver_op_to_bot

    async def scenario():
        native = SimpleNamespace(edit_message_text=AsyncMock(side_effect=BadRequest('Message is not modified')),
                                 edit_message_reply_markup=AsyncMock(side_effect=BadRequest('Message is not modified')),
                                 send_message=AsyncMock())
        for kind in (OP_EDIT, OP_EDIT_MARKUP):
            op = Op(kind,logical_id=1,chat_id=31,payload={'text':'already delivered'})
            assert await deliver_op_to_bot(native,op,101) is None
        native.send_message.assert_not_called()
    asyncio.run(scenario())
