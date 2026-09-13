"""Compression scenarios with real adapters and isolated SQLite/files/HTTP."""

import asyncio
import base64
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from tests.attachment_request_probe import FORMATS, Harness, LONG_TEXT, MODEL, assert_complete, unpack_request
from tests.test_attachments import image_bytes
from xgent_app.compression import COMPRESSION_MARKER, DEFAULT_COMPRESSION_PROMPT


class CompressionHarness(Harness):
    finish_reason = None
    raw_sse = None

    async def __aenter__(self):
        await super().__aenter__()
        self.bot.PromptFileManager.init()
        self.stack.enter_context(patch.object(self.bot, 'check_authorized_user_middleware', AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(self.bot, 'get_web_outbox', lambda: self.outbox))
        await self.bot.get_or_create_chat_session()
        return self

    def respond(self, request):
        response = super().respond(request)
        if self.raw_sse is not None:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=self.raw_sse)
        if self.finish_reason and response.status_code == 200 and not self.force_sse:
            data = response.json()
            data['choices'][0]['finish_reason'] = self.finish_reason
            data['candidates'][0]['finishReason'] = self.finish_reason
            data['stop_reason'] = self.finish_reason
            return httpx.Response(200, json=data)
        return response

    async def compress(self, fmt='openai', summary='SUMMARY-ONE', via_callback=False):
        self.bot.UserDataManager.get('providers')['p']['api_format'] = fmt
        self.replies.append(summary)
        if via_callback:
            update, context, _ = self.bot.build_web_callback_objects(1, self.outbox, 'cmd_compress', 1)
            await self.bot.handle_button_click(update, context)
        else:
            await self.bot.cmd_compress(self.update, self.context)


async def round_trips(bot, root):
    results = {}
    for fmt in FORMATS:
        async with CompressionHarness(bot, Path(root) / fmt) as h:
            await h.seed()
            await bot.GlobalRecorder.record_user_message('OLDEST-REQUIREMENT')
            await bot.GlobalRecorder.record(
                msg_type=bot.MessageType.AGENT_STATUS, role='system', content='PERSISTED-STATUS-TAIL',
            )
            for i in range(15):
                await bot.GlobalRecorder.record_ai_reply(f'progress {i}')
            await h.compress(fmt, via_callback=(fmt == 'openai_compatible'))
            latest = await h.db.get_latest_compression()
            assert latest, h.drain_frames()
            events = [frame for frame in h.drain_frames()
                      if frame['type'] in {'compression_state', 'history_reset'}]
            assert events == [
                {'type': 'compression_state', 'busy': True}, {'type': 'history_reset'},
                {'type': 'compression_state', 'busy': False, 'committed': True},
            ]
            assert_complete(h.requests[-1], latest['instruction'])
            assert 'OLDEST-REQUIREMENT' in '\n'.join(unpack_request(h.requests[-1])[0])
            assert 'PERSISTED-STATUS-TAIL' in '\n'.join(unpack_request(h.requests[-1])[0])
            assert len(h.requests) == 1, 'compression must not continue Agent execution'
            assert not await h.db.get_attachment_records()
            assert list(Path(bot.ArtifactManager.UPLOAD_DIR).rglob('*'))
            before = len(h.requests)
            await bot.cmd_compress(h.update, h.context)
            assert len(h.requests) == before, 'no new content must be a no-op'
            for i in range(15):
                await bot.GlobalRecorder.record_user_message(f'NEW-RECORD-{i}')
            await h.call(fmt=fmt)
            texts, images, _ = unpack_request(h.requests[-1])
            assert not images and LONG_TEXT not in '\n'.join(texts)
            assert '\n'.join(texts).count('SUMMARY-ONE') == 1
            for name in ('\u5168\u5c40\u8bb0\u5fc61.txt', 'manifest.json'):
                path = Path(latest['text_dir']) / name
                assert path.is_file() and str(path) in '\n'.join(texts)
            assert COMPRESSION_MARKER not in json.dumps(h.requests[-1])
            new_image = image_bytes(color='green')
            await h.add_upload(new_image, 'new.png')
            await h.call(fmt=fmt)
            assert unpack_request(h.requests[-1])[1] == [new_image]
            bot.PromptFileManager.set('compression_prompt', 'SECOND-INSTRUCTION')
            await h.compress(fmt, 'SUMMARY-TWO')
            latest = await h.db.get_latest_compression()
            assert latest['sequence'] == 2
            texts, images, _ = unpack_request(h.requests[-1])
            assert images == [new_image] and LONG_TEXT not in '\n'.join(texts)
            assert 'NEW-RECORD-0' in '\n'.join(texts) and 'SUMMARY-ONE' in '\n'.join(texts)
            assert texts[-1] == 'SECOND-INSTRUCTION'
            with zipfile.ZipFile(latest['archive_path']) as archive:
                assert archive.read('\u538b\u7f29\u63d0\u793a\u8bcd2.txt').decode() == 'SUMMARY-ONE'
                assert archive.read('\u538b\u7f29\u63d0\u793a\u8bcd4.txt').decode() == 'SUMMARY-TWO'
                assert archive.read('\u538b\u7f29\u6307\u4ee4_\u7b2c1\u6b21.txt').decode().strip() == DEFAULT_COMPRESSION_PROMPT
                assert archive.read('\u538b\u7f29\u6307\u4ee4_\u7b2c2\u6b21.txt').decode() == 'SECOND-INSTRUCTION'
                for name in archive.namelist():
                    assert archive.read(name) == (Path(latest['text_dir']) / name).read_bytes()
                data = b'\n'.join(archive.read(n) for n in archive.namelist())
                assert base64.b64encode(new_image) not in data
            await bot.cmd_export_all(h.update, h.context)
            rows = await bot._web_read_history(0)
            exports = [m for row in rows for m in row['media'] if m['filename'] == '\u7cfb\u7edf\u8bb0\u5fc6.zip']
            with zipfile.ZipFile(exports[-1]['path']) as archive:
                assert '\u5168\u5c40\u8bb0\u5fc65.txt' in archive.namelist()
            results[fmt] = True
    return results


async def restart(bot, root):
    results = {}
    for fmt in FORMATS:
        async with CompressionHarness(bot, Path(root) / fmt) as h:
            await h.call(fmt=fmt)
            texts, images, _ = unpack_request(h.requests[-1])
            assert not images
            assert '\n'.join(texts).count('SUMMARY-TWO') == 1
            assert 'SUMMARY-ONE' not in '\n'.join(texts)
            results[fmt] = True
    return results


async def failures(bot, root):
    results = {}
    for mode in ('empty', 'limit', 'missing', 'disk', 'database', 'commit', 'archive_verify',
                 'upstream', 'invalid_prompt', 'media'):
        async with CompressionHarness(bot, Path(root) / mode) as h:
            ref = await h.add_upload(image_bytes(), 'original.png')
            await bot.GlobalRecorder.record_user_message('KEEP-ORIGINAL')
            before = await h.db.get_compression_snapshot()
            if mode == 'limit':
                bot.UserDataManager.set('model_request_limits', {f'p/{MODEL}': {'max_request_bytes': 1}})
            if mode == 'missing':
                (Path(bot.ArtifactManager.UPLOAD_DIR) / ref['path']).unlink()
            if mode == 'disk':
                h.stack.enter_context(patch.object(bot, 'save_compression_archive', side_effect=OSError('disk failed')))
            if mode == 'database':
                conn = await h.db._get_conn()
                execute = conn.execute
                async def fail(sql, *args, **kwargs):
                    if 'INSERT INTO global_messages' in sql:
                        raise OSError('insert failure after deletes')
                    return await execute(sql, *args, **kwargs)
                h.stack.enter_context(patch.object(conn, 'execute', fail))
            if mode == 'commit':
                conn = await h.db._get_conn()
                commit = conn.commit
                async def fail_commit():
                    cursor = await conn.execute('SELECT COUNT(*) FROM context_compressions')
                    if (await cursor.fetchone())[0]:
                        raise OSError('commit failed after all writes')
                    await commit()
                h.stack.enter_context(patch.object(conn, 'commit', fail_commit))
            if mode == 'archive_verify':
                h.stack.enter_context(patch.object(zipfile.ZipFile, 'testzip', side_effect=OSError('zip verification failed')))
            if mode == 'upstream':
                h.error = (400, 'context too long')
            if mode == 'invalid_prompt':
                Path(bot.PromptFileManager.get_abs_path('compression_prompt')).write_text('', encoding='utf-8')
            summary = '' if mode == 'empty' else 'summary'
            if mode == 'media':
                summary = 'data:image/png;base64,' + base64.b64encode(image_bytes()).decode()
            await h.compress(summary=summary)
            assert await h.db.get_compression_snapshot() == before, mode
            if mode in {'limit', 'missing', 'invalid_prompt'}:
                assert not h.requests
            results[mode] = True
    for fmt in FORMATS:
        async with CompressionHarness(bot, Path(root) / f'truncated-{fmt}') as h:
            await bot.GlobalRecorder.record_user_message('KEEP-ORIGINAL')
            before = await h.db.get_compression_snapshot()
            h.finish_reason = 'MAX_TOKENS' if fmt in {'gemini', 'vertex'} else 'length'
            await h.compress(fmt)
            assert await h.db.get_compression_snapshot() == before, fmt
            results[f'truncated-{fmt}'] = True
    return results


async def races(bot, root):
    results = {}
    for mode in ('stop', 'clear', 'concurrent', 'stop_after_archive', 'delivery', 'cancel',
                 'busy', 'busy_during_init'):
        async with CompressionHarness(bot, Path(root) / mode) as h:
            ref = await h.add_upload(image_bytes(), 'original.png')
            await bot.GlobalRecorder.record_user_message('KEEP-ORIGINAL')
            before = await h.db.get_compression_snapshot()
            if mode in {'stop', 'clear', 'cancel'}:
                entered = asyncio.Event()
                async def waiting(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()
                with patch.object(bot.ModelClient, 'think_and_reply', waiting):
                    task = asyncio.create_task(h.compress())
                    await asyncio.wait_for(entered.wait(), 5)
                    if mode == 'cancel':
                        task.cancel()
                    elif mode == 'clear':
                        await bot.cmd_delete_chat(h.update, h.context)
                        await bot.GlobalRecorder.record_user_message('AFTER-CLEAR')
                    else:
                        update, context, _ = bot.build_web_callback_objects(1, h.outbox, 'act_stop_generation', 1)
                        await bot.handle_button_click(update, context)
                    try:
                        await task
                    except asyncio.CancelledError:
                        assert mode == 'cancel'
            elif mode == 'concurrent':
                original = bot.ModelClient.think_and_reply
                async def concurrent(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    await bot.GlobalRecorder.record_user_message('CONCURRENT-NEW')
                    return result
                with patch.object(bot.ModelClient, 'think_and_reply', concurrent):
                    await h.compress()
            elif mode == 'stop_after_archive':
                original = bot.save_compression_archive
                loop = asyncio.get_running_loop()
                def stopped(*args, **kwargs):
                    result = original(*args, **kwargs)
                    loop.call_soon_threadsafe(bot._stop_generation_event.set)
                    return result
                with patch.object(bot, 'save_compression_archive', stopped):
                    await h.compress()
            elif mode == 'delivery':
                with patch.object(h.context.bot, 'send_document', AsyncMock(side_effect=OSError('delivery failed'))):
                    await h.compress()
            elif mode == 'busy':
                async with bot._conversation_processing_lock:
                    await h.compress()
                assert not h.requests
            elif mode == 'busy_during_init':
                initialize = bot.UserDataManager.init
                async def busy_init():
                    await initialize()
                    await bot._conversation_processing_lock.acquire()
                try:
                    with patch.object(bot.UserDataManager, 'init', busy_init):
                        await asyncio.wait_for(h.compress(), 2)
                finally:
                    if bot._conversation_processing_lock.locked():
                        bot._conversation_processing_lock.release()
                assert not h.requests
            after = await h.db.get_compression_snapshot()
            if mode == 'delivery':
                assert len(after['compressions']) == 1
                row = (await bot._web_read_history(0))[0]
                assert row['media'][0]['download_url']
            elif mode == 'clear':
                assert not after['compressions'] and len(after['records']) == 1
                assert after['records'][0]['content'] == 'AFTER-CLEAR'
            elif mode == 'concurrent':
                assert not after['compressions']
                assert len(after['records']) == len(before['records']) + 1
            else:
                assert before == after, mode
            assert (Path(bot.ArtifactManager.UPLOAD_DIR) / ref['path']).is_file()
            assert not bot._compression_running and not bot._conversation_processing_lock.locked()
            results[mode] = True
    return results


async def sse_compression(bot, root):
    def frame(value):
        return 'data: ' + json.dumps(value) + '\n\n'

    text = frame({'choices': [{'delta': {'content': 'SSE-SUMMARY'}}]})
    cases = {
        'complete': text + 'data: [DONE]\n\n',
        'truncated': text + frame({'choices': [{'finish_reason': 'length'}]}) + 'data: [DONE]\n\n',
        'malformed': text + 'data: {broken JSON}\n\ndata: [DONE]\n\n',
        'disconnected': text,
        'upstream_error': text + frame({'error': {'message': 'upstream failed'}}) + 'data: [DONE]\n\n',
    }
    results = {}
    for mode, response in cases.items():
        async with CompressionHarness(bot, Path(root) / mode) as h:
            await bot.GlobalRecorder.record_user_message('KEEP-ORIGINAL')
            before = await h.db.get_compression_snapshot()
            h.raw_sse = response
            await h.compress('openai_compatible')
            if mode == 'complete':
                assert (await h.db.get_latest_compression())['summary'] == 'SSE-SUMMARY'
            else:
                assert await h.db.get_compression_snapshot() == before
            assert len(h.requests) == 1
            results[mode] = True
    return results


async def clear_chain(bot, root):
    async with CompressionHarness(bot, root) as h:
        await bot.GlobalRecorder.record_user_message('original')
        await h.compress()
        old = await h.db.get_latest_compression()
        old_id = (await h.db.get_display_history(0))[0]['id']
        await bot.cmd_delete_chat(h.update, h.context)
        assert not await h.db.get_latest_compression()
        assert await h.db.get_display_message(old_id) is None
        assert Path(old['archive_path']).is_file()
        await bot.GlobalRecorder.record_user_message('NEW-CHAIN')
        await h.compress(summary='NEW-CHAIN-SUMMARY')
        assert (await h.db.get_latest_compression())['sequence'] == 1
        return {'clear_resets_chain_without_deleting_files': True}


async def generated_and_agent(bot, root):
    async with CompressionHarness(bot, root) as h:
        uploaded, generated = image_bytes(color='red'), image_bytes(color='blue')
        await h.add_upload(uploaded, 'upload.png')
        saved = bot.ArtifactManager.save_generated_media('assistant_image.png', generated, 'image/png')
        artifacts = [{'path': saved['abs_path'], 'mime_type': 'image/png', 'source': 'chat_native_media'}]
        metadata = await bot.generated_image_metadata(artifacts, await h.db.get_attachment_generation())
        await bot.GlobalRecorder.record_ai_reply(bot.build_generated_media_reply_text('generated', artifacts), metadata=metadata)
        bot.UserDataManager.set('agent_mode', True)
        summary = 'SUMMARY-WITH-PROTOCOL\n```run-x\n<<BEGIN_never_execute_42\nprintf should-not-run\n<<END_never_execute_42\n```'
        with patch.object(bot.AgentExecutor, 'run_command', AsyncMock(side_effect=AssertionError('executed summary'))) as execute:
            await h.compress(summary=summary)
            execute.assert_not_called()
        assert unpack_request(h.requests[0])[1] == [uploaded, generated]
        assert len(h.requests) == 1
        for fmt in FORMATS:
            for stream in (False, True):
                await h.call(fmt=fmt, stream=stream)
                texts, images, _ = unpack_request(h.requests[-1])
                assert not images and '\n'.join(texts).count('SUMMARY-WITH-PROTOCOL') == 1
        assert Path(saved['abs_path']).read_bytes() == generated
        return {'mixed_originals': True, 'no_execution': True, 'all_later_paths': True}
