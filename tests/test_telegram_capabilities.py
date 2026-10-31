import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import InlineKeyboardButton,InlineKeyboardMarkup,CopyTextButton,LoginUrl
from xgent_app.web_bridge import _markup_to_frame,markup_from_frame,deliver_op_to_bot
from xgent_app.rich_input import block,text,media
from xgent_app.fanout import Op,OP_DOCUMENT


def test_copy_style_disabled_login_and_inline_buttons_survive_transport_round_trip():
    buttons=[InlineKeyboardButton('复制',copy_text=CopyTextButton('echo one'),style='primary'),
             InlineKeyboardButton('不可用',api_kwargs={'disabled':{}}),
             InlineKeyboardButton('登录',login_url=LoginUrl('https://example.com/login')),
             InlineKeyboardButton('检索',switch_inline_query_current_chat='report')]
    original=InlineKeyboardMarkup([buttons]);restored=markup_from_frame(_markup_to_frame(original))
    assert original.to_dict()==restored.to_dict()


def test_unknown_rich_blocks_and_button_payloads_never_become_user_text():
    assert block({'type':'buttons','buttons':[{'text':'按钮','callback_data':'do-secret'}]})==''
    assert block({'type':'unknown','text':'不应同步','token':'secret'})==''
    assert text({'type':'button','text':'do-secret'})==''
    assert block({'type':'paragraph','text':{'type':'bold','text':'原文'}},True)=='**原文**'
    assert media({'blocks':[{'type':'document','document':{'file_id':'abc'},'caption':{'text':'附件'}},
                            {'type':'unknown','document':{'file_id':'not-an-attachment'}}]})==[('document',{'file_id':'abc'})]


def test_playable_media_delivery_uses_own_native_method_and_keeps_logical_identity():
    async def main():
        native=SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(message_id=7)),send_document=AsyncMock())
        op=Op(OP_DOCUMENT,chat_id=1,logical_id=100,payload={'caption':'视频','kwargs':{'native_media':'video'}},transient={'document':object()})
        assert await deliver_op_to_bot(native,op,None)==7
        native.send_video.assert_awaited_once();native.send_document.assert_not_called()
    asyncio.run(main())
