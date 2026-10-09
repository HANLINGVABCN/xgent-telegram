"""Permanent reported-error summaries and real request/history regressions."""
import unittest
from tests.test_external_sync import ProbeMixin
from xgent_app.error_reporting import error_text


class ErrorSummaryTests(unittest.TestCase):
    def test_fifty_lines_and_permanent_tail_are_idempotent(self):
        raw = '\n'.join('line-%03d' % i for i in range(80))
        summary = error_text(raw)
        self.assertEqual(raw.splitlines()[:50], summary.splitlines()[:50])
        self.assertEqual('已折叠 30 行报错（永久省略）', summary.splitlines()[-1])
        self.assertNotIn('line-050', summary)
        self.assertEqual(summary, error_text(summary))
        self.assertEqual(51, len(summary.splitlines()))
        self.assertEqual('已折叠 31 行报错（永久省略）', error_text('header\n' + summary).splitlines()[-1])

    def test_exact_limit_and_short_text_are_not_folded(self):
        for count in (1, 49, 50):
            text = '\n'.join(str(i) for i in range(count))
            self.assertEqual(text, error_text(text))
        self.assertEqual('first\nsecond', error_text('first\r\nsecond\r\n'))

    def test_remove_only_attachment_wrapper_not_the_actual_cause(self):
        raw = 'AttachmentContextError: 完整上下文请求失败，未获得有效回复，也没有自动减少附件：\nOpenAI compatible API error (500): EOF'
        self.assertEqual('OpenAI compatible API error (500): EOF', error_text(raw))
        self.assertEqual('ValueError: invalid', error_text('ValueError: invalid'))
        self.assertEqual('missing file', error_text('AttachmentContextError: missing file'))


class ErrorPersistenceTests(ProbeMixin, unittest.TestCase):
    def test_actual_500_eof_is_visible_persisted_and_sent_on_next_request(self):
        result = self.run_probe(r'''
import asyncio, html, tempfile
import xgent_server as bot
from tests.attachment_request_probe import Harness, unpack_request
async def main():
    result = {}
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot, folder) as h:
            bot.UserDataManager.get('providers')['p']['api_format'] = 'openai_compatible'
            bot.UserDataManager.set('global_depth', 50)
            for stream, style in ((False,'foreground'),(True,'foreground'),(True,'background')):
                await h.db.clear_all_conversation_memory()
                bot.UserDataManager.set('stream_mode', stream)
                bot.UserDataManager.set('stream_style', style)
                h.error=(500, 'Post https://daily-cloudcode-pa.googleapis.com/v1internal:generateContent: EOF')
                h.drain_frames()
                await h.turn('first question')
                frames=html.unescape(json.dumps(h.drain_frames(),ensure_ascii=False))
                rows=await h.db.get_global_messages(100, active_context=True)
                errors=[r for r in rows if r['msg_type']=='runtime_error']
                context=await h.db.get_conversation_messages(100)
                assert len(errors)==1,rows
                text=errors[0]['content']
                assert 'OpenAI compatible API error (500)' in text and 'EOF' in text
                assert 'AttachmentContextError:' not in text and '没有自动减少附件' not in text
                assert 'EOF' in frames
                assert not any(r['msg_type']=='ai_reply' for r in rows)
                assert any(m['content']==text and m['role']=='user' for m in context)
                h.error=None
                await h.turn('what failed?')
                texts,_,_=unpack_request(h.requests[-1])
                assert any(text in t for t in texts),texts
                result[str(stream)+'/'+style]=True
    print(json.dumps(result))
asyncio.run(main())
''')
        self.assertEqual(3, len(result))
        self.assertTrue(all(result.values()))

    def test_long_errors_have_same_saved_and_model_summary_in_all_renderers(self):
        result = self.run_probe(r'''
import asyncio, html, tempfile
from unittest.mock import AsyncMock, patch
import xgent_server as bot
from tests.attachment_request_probe import Harness, unpack_request
from xgent_app.attachments import AttachmentContextError
from xgent_app.web_history import build_history_message
async def main():
    result={}
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot, folder) as h:
            bot.UserDataManager.set('global_depth',50)
            bot.register_runtime_secret('TEST_SECRET_NEVER_SAVED')
            for hide in (True,False):
              bot.UserDataManager.set('hide_protocol_blocks',hide)
              for stream,style in ((False,'foreground'),(True,'foreground'),(True,'background')):
                await h.db.clear_all_conversation_memory()
                bot.UserDataManager.set('stream_mode',stream);bot.UserDataManager.set('stream_style',style)
                message='完整上下文请求失败，未获得有效回复，也没有自动减少附件：\n'+'\n'.join('VISIBLE-%03d TEST_SECRET_NEVER_SAVED'%i for i in range(80))
                async def broken_stream(*args,**kwargs):
                    raise AttachmentContextError(message)
                    yield 'never'
                h.drain_frames()
                with patch.object(bot.ModelClient,'think_and_reply',AsyncMock(side_effect=AttachmentContextError(message))), patch.object(bot.ModelClient,'think_and_reply_stream',broken_stream):
                    await h.turn('cause failure')
                rows=await h.db.get_global_messages(100, active_context=True)
                errors=[r for r in rows if r['msg_type']=='runtime_error']
                assert len(errors)==1,rows
                row=errors[0];text=row['content'];frames=html.unescape(json.dumps(h.drain_frames(),ensure_ascii=False))
                assert 'VISIBLE-049' in text and 'VISIBLE-050' not in text
                assert text.endswith('已折叠 30 行报错（永久省略）'),text
                assert 'VISIBLE-049' in frames and 'VISIBLE-050' not in frames
                assert 'TEST_SECRET_NEVER_SAVED' not in json.dumps(row)
                assert 'AttachmentContextError:' not in frames
                display=build_history_message(row,bot.ArtifactManager.ROOT_DIR,folder)
                assert text in html.unescape(display['content']) and 'data-raw' not in display['content']
                assert not any(r['msg_type']=='ai_reply' for r in rows)
                await h.turn('continue')
                texts,_,_=unpack_request(h.requests[-1])
                assert any(text in t for t in texts),texts
                assert not any('VISIBLE-050' in t for t in texts)
                result[str(hide)+'/'+str(stream)+'/'+style]=True
    print(json.dumps(result))
asyncio.run(main())
''')
        self.assertEqual(6,len(result)); self.assertTrue(all(result.values()))

    def test_generic_error_tuple_empty_reply_and_timeout_are_not_ai_answers(self):
        result=self.run_probe(r'''
import asyncio,tempfile
from unittest.mock import AsyncMock,patch
import xgent_server as bot
from tests.attachment_request_probe import Harness
async def main():
    result={}
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot,folder) as h:
            for case in ('exception','tuple','empty','timeout'):
                await h.db.clear_all_conversation_memory()
                async def slow(*args,**kwargs):
                    await asyncio.sleep(10)
                    return 'unexpected', None
                call=AsyncMock(side_effect=RuntimeError('GENERIC ERROR')) if case=='exception' else (
                    AsyncMock(side_effect=slow) if case=='timeout' else AsyncMock(return_value=(None,'TUPLE ERROR') if case=='tuple' else ('',None)))
                with patch.object(bot.ModelClient,'think_and_reply',call), patch.object(bot,'_nonstream_hard_timeout_seconds',return_value=.05):
                    await h.turn('request')
                rows=await h.db.get_global_messages(100, active_context=True)
                assert len([r for r in rows if r['msg_type']=='runtime_error'])==1,(case,rows)
                assert not any(r['msg_type']=='ai_reply' for r in rows),(case,rows)
                result[case]=True
    print(json.dumps(result))
asyncio.run(main())
''')
        self.assertEqual(4,len(result));self.assertTrue(all(result.values()))

    def test_outer_errors_deduplicate_and_restart_preserves_context(self):
        result=self.run_probe(r'''
import asyncio,tempfile
from unittest.mock import AsyncMock,patch
import xgent_server as bot
from tests.attachment_request_probe import Harness
from xgent_app.error_reporting import runtime_error_scope
async def main():
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot,folder) as h:
            error=RuntimeError('context assembly failed\n'+'\n'.join('line%d'%i for i in range(80)))
            with patch.object(bot,'_process_conversation_inner',AsyncMock(side_effect=error)):
                try:await h.turn('question')
                except RuntimeError:pass
            once=await bot.GlobalRecorder.record_error(error,1,source='outer')
            twice=await bot.GlobalRecorder.record_error(error,1,source='outer again')
            rows=await h.db.get_global_messages(100, active_context=True)
            assert len([r for r in rows if r['msg_type']=='runtime_error'])==1
            await h.db.close()
            await h.db._get_conn()
            context=await h.db.get_conversation_messages(100)
            assert any(r['content']==once for r in context)
            old_generation=await h.db.get_attachment_generation()
            await h.db.clear_all_conversation_memory()
            old_error=ValueError('old failure')
            with runtime_error_scope(1,old_generation):
                await bot.GlobalRecorder.record_error(old_error,1)
            await bot.GlobalRecorder.record_error(old_error,1,source='outer')
            assert not await h.db.get_conversation_messages(100)
            print(json.dumps({'deduplicated':once==twice,'restart':True,'clear_guard':True}))
asyncio.run(main())
''')
        self.assertTrue(all(result.values()))

    def test_tool_failure_tail_is_not_returned_in_context_or_presentation(self):
        from xgent_app.agent_results import normalize_edit_result, failed_result
        from xgent_app.agent_presenter import build_standard_operation_presentation
        body='\n'.join('tool-line-%03d'%i for i in range(80))
        for result in (normalize_edit_result({'success':False,'output':body,'error':body}), failed_result('edit',body)):
            self.assertNotIn('tool-line-050',json_text(result))
            self.assertNotIn('tool-line-050',build_standard_operation_presentation(result))
            self.assertIn('永久省略',result['context_message']['content'])


def json_text(value):
    import json
    return json.dumps(value,ensure_ascii=False)
