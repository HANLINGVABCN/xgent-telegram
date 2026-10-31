import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from xgent_app.media_response import MediaCapture, current_media, separate, reply
from xgent_app.trace_payload import summarize
from xgent_app.telegram_media_reply import send
from xgent_app.fanout import Op
from xgent_app.telegram_native import OP_NATIVE_MEDIA


def test_native_media_is_not_prose_and_quoted_preview_is_not_a_second_image():
    capture=MediaCapture();token=current_media.set(capture)
    try:
        data=separate({'choices':[{'message':{'content':'说明 `data:image/png;base64,AAAAA`',
            'images':[{'image_url':{'url':'data:image/png;base64,QUJD'}}]}}]})
        result=reply(data['choices'][0]['message']['content'])
        assert 'base64' not in result and len(result.media)==1
        assert result.media[0]['source_field']=='message.images.0'
    finally:
        current_media.reset(token)


def test_media_trace_is_summarized_before_serializing():
    body={'messages':[{'content':[{'type':'image','data':'A'*20000}]}],
          'response':'图 data:image/png;base64,'+'B'*20000}
    result=summarize(body)
    assert 'A'*200 not in str(result) and 'B'*200 not in str(result)
    assert '字符数' in str(result)


def test_native_media_and_prose_share_one_message_and_retry_does_not_duplicate(tmp_path):
    async def run():
        paths=[]
        for n in range(2):
            p=tmp_path/f'{n}.png';p.write_bytes(b'fixture');paths.append(p)
        native=SimpleNamespace(_post=AsyncMock(return_value={'message_id':51}))
        op=Op(OP_NATIVE_MEDIA,chat_id=1,logical_id=100,payload={'artifacts':[
            {'path':str(p),'mime_type':'image/png'} for p in paths],'text':'生成文字在下面'})
        assert await send(native,op,{})==51
        assert await send(native,op,{})==51
        assert native._post.await_count==1
        args=native._post.call_args.args[1]
        html=args['rich_message']['html']
        assert html.index('</tg-collage>')<html.index('生成文字在下面')
        assert len(args['rich_message']['media'])==2
    asyncio.run(run())


def test_media_details_edit_same_reply_without_resending_images(tmp_path):
    async def run():
        path=tmp_path/'one.png';path.write_bytes(b'fixture')
        native=SimpleNamespace(_post=AsyncMock(return_value={'message_id':61}))
        op=Op(OP_NATIVE_MEDIA,chat_id=1,logical_id=100,payload={'artifacts':[
            {'path':str(path),'mime_type':'image/png'}],'text':'生成正文'})
        await send(native,op,{})
        op.payload.update(_edit=True,details_html='<blockquote expandable>回复详情</blockquote>')
        await send(native,op,{})
        assert [call.args[0] for call in native._post.call_args_list]==['sendRichMessage','editMessageText']
        html=native._post.call_args.args[1]['rich_message']['html']
        assert html.count('生成正文')==1 and html.count('回复详情')==1
        await send(native,op,{})
        assert native._post.await_count==2
    asyncio.run(run())


def test_full_native_generation_adds_details_to_same_media_reply(tmp_path):
    from tests.test_thinking_params import run_in_app
    result = run_in_app("import asyncio\nfrom tests.bot_media_repair_probe import exercise\n" +
                        f"print(json.dumps(asyncio.run(exercise(bot,{str(tmp_path)!r}))))")
    assert result['media_messages']==1 and result['details']==1
