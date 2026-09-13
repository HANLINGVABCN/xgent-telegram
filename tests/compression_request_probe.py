"""Read-backed restore scenarios using real adapters and isolated SQLite/HTTP."""

import asyncio
import base64
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from tests.attachment_request_probe import FORMATS, Harness, LONG_TEXT, MODEL, unpack_request
from tests.test_attachments import image_bytes
from xgent_app.compression import (
    ARCHIVE_MARKER, COMPRESSION_MARKER, DEFAULT_COMPRESSION_PROMPT,
    INSTRUCTION_NAME, MEMORY_NAME, SUMMARY_NAME, ATTACHMENTS_NAME,
)


def memory(number):
    return f'{number}a{MEMORY_NAME}.txt'


def attachments(number):
    return f'{number}b{ATTACHMENTS_NAME}.txt'


def summary_file(number):
    return f'{number}c{SUMMARY_NAME}.txt'


def contents(body):
    return '\n'.join(unpack_request(body)[0])


class CompressionHarness(Harness):
    finish_reason = None
    raw_sse = None

    async def __aenter__(self):
        await super().__aenter__()
        self.events = []
        self.stack.enter_context(patch.object(self.bot, 'check_authorized_user_middleware', AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(self.bot, 'get_web_outbox', lambda: self.outbox))
        await self.bot.get_or_create_chat_session()
        export = self.bot.save_conversation_export
        begin = self.db.begin_compression
        read = self.bot.AgentExecutor.read_file_ranged

        def exported(*args, **kwargs):
            result = export(*args, **kwargs)
            self.events.append('export')
            return result

        async def cleared(*args, **kwargs):
            result = await begin(*args, **kwargs)
            self.events.append('clear')
            return result

        async def loaded(path):
            result = await read(path)
            self.events.append('read:' + Path(path).name)
            return result

        self.stack.enter_context(patch.object(self.bot, 'save_conversation_export', exported))
        self.stack.enter_context(patch.object(self.db, 'begin_compression', cleared))
        self.stack.enter_context(patch.object(self.bot.AgentExecutor, 'read_file_ranged', loaded))
        return self

    def respond(self, request):
        self.events.append('model')
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

    async def retry(self, job_id, text='RETRIED-SUMMARY'):
        self.replies.clear()
        self.replies.append(text)
        update, context, _ = self.bot.build_web_callback_objects(1, self.outbox, 'retry_compress:' + job_id, 1)
        await self.bot.handle_button_click(update, context)


async def round_trips(bot, root):
    results = {}
    for fmt in FORMATS:
        async with CompressionHarness(bot, Path(root) / fmt) as h:
            await h.seed()
            original = 'OLDEST-REQUIREMENT\n' + 'complete history ' * 9000 + '\nGLOBAL-TAIL'
            await bot.GlobalRecorder.record_user_message(original)
            for i in range(15):
                await bot.GlobalRecorder.record_ai_reply(f'progress {i}')
            await h.compress(fmt, via_callback=(fmt == 'openai_compatible'))
            latest = await h.db.get_latest_compression()
            assert latest['status'] == 'completed', latest
            assert h.events == ['export', 'clear', 'read:' + memory(1), 'read:' + attachments(1), 'model'], h.events
            frames = h.drain_frames()
            assert any(frame['type'] == 'chat_action' and frame['action'] == 'typing' for frame in frames)
            assert [frame for frame in frames if frame['type'] in {'compression_state', 'history_reset'}] == [
                {'type': 'compression_state', 'busy': True}, {'type': 'history_reset'},
                {'type': 'compression_state', 'busy': False, 'committed': True},
            ]
            texts, images, _ = unpack_request(h.requests[-1])
            text = '\n'.join(texts)
            assert not images and LONG_TEXT not in text
            assert 'OLDEST-REQUIREMENT' in text and 'GLOBAL-TAIL' in text and 'progress 14' in text
            assert texts[-1] == latest['instruction']
            assert latest['instruction'].strip() == DEFAULT_COMPRESSION_PROMPT
            rows = await h.db.get_display_history(0)
            assert rows[0]['msg_type'] == 'ai_reply' and rows[0]['content'] == 'SUMMARY-ONE'
            assert not await h.db.get_attachment_records()
            assert len(h.requests) == 1
            frozen_zip = Path(latest['archive_path']).read_bytes()
            with zipfile.ZipFile(latest['archive_path']) as archive:
                assert archive.namelist() == ['\u62e6\u622a\u8bb0\u5f55.txt', '\u63d0\u793a\u8bcd.txt',
                                             INSTRUCTION_NAME, memory(1), attachments(1)]
                assert original in archive.read(memory(1)).decode()
                index = json.loads(archive.read(attachments(1)))
                assert any(Path(item['path']).is_relative_to(bot.ArtifactManager.UPLOAD_DIR) for item in index)
            await h.call(fmt=fmt)
            assert contents(h.requests[-1]).count('SUMMARY-ONE') == 1
            before = len(h.requests)
            await bot.cmd_compress(h.update, h.context)
            assert len(h.requests) == before
            for i in range(15):
                await bot.GlobalRecorder.record_user_message(f'NEW-RECORD-{i}')
            await h.call(fmt=fmt)
            text = contents(h.requests[-1])
            assert 'SUMMARY-ONE' not in text and 'GLOBAL-TAIL' not in text
            assert latest['memory_path'] in text and latest['archive_path'] in text
            assert ARCHIVE_MARKER not in json.dumps(h.requests[-1]) and COMPRESSION_MARKER not in json.dumps(h.requests[-1])
            new_image = image_bytes(color='green')
            await h.add_upload(new_image, 'new.png')
            await h.call(fmt=fmt)
            assert unpack_request(h.requests[-1])[1] == [new_image]
            bot.PromptFileManager.set('compression_prompt', 'SECOND-INSTRUCTION')
            await h.compress(fmt, 'SUMMARY-TWO')
            second = await h.db.get_latest_compression()
            assert second['sequence'] == 2 and second['status'] == 'completed'
            texts, images, _ = unpack_request(h.requests[-1])
            assert not images and 'GLOBAL-TAIL' not in '\n'.join(texts)
            assert '\n'.join(texts).count('SUMMARY-ONE') == 1 and 'NEW-RECORD-14' in '\n'.join(texts)
            assert texts[-1] == 'SECOND-INSTRUCTION'
            assert Path(latest['archive_path']).read_bytes() == frozen_zip
            with zipfile.ZipFile(second['archive_path']) as archive:
                assert archive.read(summary_file(1)).decode() == 'SUMMARY-ONE'
                assert archive.read(memory(2)).decode().count('SUMMARY-ONE') == 1
                assert summary_file(2) not in archive.namelist()
                assert archive.read(INSTRUCTION_NAME).decode() == 'SECOND-INSTRUCTION'
                for name in archive.namelist():
                    assert archive.read(name) == (Path(second['text_dir']) / name).read_bytes()
                assert base64.b64encode(new_image) not in b'\n'.join(archive.read(n) for n in archive.namelist())
            await bot.cmd_export_all(h.update, h.context)
            rows = await bot._web_read_history(0)
            exports = [m for row in rows for m in row['media'] if m['filename'] == '\u7cfb\u7edf\u8bb0\u5fc6.zip']
            with zipfile.ZipFile(exports[-1]['path']) as archive:
                assert archive.read(summary_file(2)).decode() == 'SUMMARY-TWO'
                assert archive.read(memory(3)).decode().count('SUMMARY-TWO') == 1
                assert summary_file(3) not in archive.namelist()
                assert len(archive.namelist()) == 11
            results[fmt] = True
    return results


async def restart(bot, root):
    results = {}
    for fmt in FORMATS:
        async with CompressionHarness(bot, Path(root) / fmt) as h:
            await h.call(fmt=fmt)
            texts, images, _ = unpack_request(h.requests[-1])
            assert not images and 'SUMMARY-TWO' not in '\n'.join(texts)
            assert (await h.db.get_latest_compression())['archive_path'] in '\n'.join(texts)
            rows = await h.db.get_display_history(0)
            assert sum(row['content'] == 'SUMMARY-TWO' for row in rows) == 1
            results[fmt] = True
    return results


async def failures(bot, root):
    results = {}
    before_clear = {'disk', 'database', 'commit', 'archive_verify', 'invalid_prompt'}
    for mode in (*sorted(before_clear), 'empty', 'limit', 'missing_original', 'upstream', 'media',
                 'read_missing', 'read_incomplete', 'changed_archive', 'reply_write'):
        async with CompressionHarness(bot, Path(root) / mode) as h:
            ref = await h.add_upload(image_bytes(), 'original.png')
            await bot.GlobalRecorder.record_user_message('KEEP-ORIGINAL')
            before = await h.db.get_compression_snapshot()
            if mode == 'limit':
                bot.UserDataManager.set('model_request_limits', {f'p/{MODEL}': {'max_request_bytes': 1}})
            elif mode == 'missing_original':
                (Path(bot.ArtifactManager.UPLOAD_DIR) / ref['path']).unlink()
            elif mode == 'disk':
                h.stack.enter_context(patch.object(bot, 'save_conversation_export', side_effect=OSError('disk failed')))
            elif mode == 'database':
                clear = h.db._clear_conversation_memory
                async def fail_clear(*args, **kwargs):
                    await clear(*args, **kwargs)
                    raise OSError('transaction failure after deletes')
                h.stack.enter_context(patch.object(h.db, '_clear_conversation_memory', fail_clear))
            elif mode == 'commit':
                conn = await h.db._get_conn()
                commit = conn.commit
                async def fail_commit():
                    cursor = await conn.execute('SELECT COUNT(*) FROM context_compressions')
                    if (await cursor.fetchone())[0]:
                        raise OSError('commit failed after all writes')
                    await commit()
                h.stack.enter_context(patch.object(conn, 'commit', fail_commit))
            elif mode == 'archive_verify':
                h.stack.enter_context(patch.object(zipfile.ZipFile, 'testzip', side_effect=OSError('zip verification failed')))
            elif mode == 'invalid_prompt':
                Path(bot.PromptFileManager.get_abs_path('compression_prompt')).write_text('', encoding='utf-8')
            elif mode == 'upstream':
                h.error = (400, 'context too long')
            elif mode == 'read_missing':
                h.stack.enter_context(patch.object(bot.AgentExecutor, 'read_file_ranged', AsyncMock(side_effect=OSError('missing archive text'))))
            elif mode == 'read_incomplete':
                h.stack.enter_context(patch.object(bot.AgentExecutor, 'read_file_ranged', AsyncMock(return_value={
                    'start': 1, 'end': 2, 'total_lines': 3, 'message': {'role': 'user', 'content': 'partial'},
                })))
            elif mode == 'changed_archive':
                h.stack.enter_context(patch.object(bot, 'verify_export', side_effect=OSError('archive changed')))
            elif mode == 'reply_write':
                insert = h.db._insert_global_record
                async def fail_reply(conn, chat_id, user_id, msg_type, *args):
                    if msg_type == bot.MessageType.AI_REPLY:
                        raise OSError('reply write failed')
                    return await insert(conn, chat_id, user_id, msg_type, *args)
                h.stack.enter_context(patch.object(h.db, '_insert_global_record', fail_reply))
            summary = '' if mode == 'empty' else 'summary'
            if mode == 'media':
                summary = 'data:image/png;base64,' + base64.b64encode(image_bytes()).decode()
            await h.compress(summary=summary)
            after = await h.db.get_compression_snapshot()
            if mode in before_clear:
                assert after == before, mode
                assert not h.requests
            else:
                entry = after['compressions'][-1]
                assert entry['status'] == ('completed' if mode == 'missing_original' else 'failed'), (mode, entry)
                assert not await h.db.get_attachment_records()
                assert Path(entry['archive_path']).is_file()
                assert 'KEEP-ORIGINAL' in Path(entry['memory_path']).read_text(encoding='utf-8')
                if mode != 'missing_original':
                    assert not entry['summary']
                    rows = await bot._web_read_history(0)
                    assert any(row.get('reply_markup') for row in rows)
                    assert any(row.get('media') and row['media'][0].get('download_url') for row in rows)
            assert not bot._compression_running and not bot._conversation_processing_lock.locked()
            results[mode] = True
    for fmt in FORMATS:
        async with CompressionHarness(bot, Path(root) / f'truncated-{fmt}') as h:
            await bot.GlobalRecorder.record_user_message('KEEP-ORIGINAL')
            h.finish_reason = 'MAX_TOKENS' if fmt in {'gemini', 'vertex'} else 'length'
            await h.compress(fmt)
            entry = await h.db.get_latest_compression()
            assert entry['status'] == 'failed' and not entry['summary'], (fmt, entry)
            results[f'truncated-{fmt}'] = True
    return results


async def races(bot, root):
    results = {}
    before_clear = {'concurrent_export', 'stop_after_archive', 'busy', 'busy_during_init'}
    for mode in ('stop', 'clear', 'cancel', 'concurrent_model', *sorted(before_clear),
                 'delivery', 'reply_delivery', 'complete_and_stop'):
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
            elif mode in {'concurrent_model', 'complete_and_stop'}:
                original = bot.ModelClient.think_and_reply
                async def concurrent(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    if mode == 'concurrent_model':
                        await bot.GlobalRecorder.record_user_message('CONCURRENT-NEW')
                    else:
                        bot._stop_generation_event.set()
                    return result
                with patch.object(bot.ModelClient, 'think_and_reply', concurrent):
                    await h.compress()
            elif mode == 'concurrent_export':
                original = bot.create_conversation_export
                async def export(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    await bot.GlobalRecorder.record_user_message('CONCURRENT-NEW')
                    return result
                with patch.object(bot, 'create_conversation_export', export):
                    await h.compress()
            elif mode == 'stop_after_archive':
                original = bot.save_conversation_export
                loop = asyncio.get_running_loop()
                def stopped(*args, **kwargs):
                    result = original(*args, **kwargs)
                    loop.call_soon_threadsafe(bot._stop_generation_event.set)
                    return result
                with patch.object(bot, 'save_conversation_export', stopped):
                    await h.compress()
            elif mode == 'delivery':
                with patch.object(h.context.bot, 'send_document', AsyncMock(side_effect=OSError('delivery failed'))):
                    await h.compress()
            elif mode == 'reply_delivery':
                with patch.object(bot, 'rich_finalize_text_response', AsyncMock(side_effect=OSError('reply delivery failed'))):
                    await h.compress()
            elif mode == 'busy':
                async with bot._conversation_processing_lock:
                    await h.compress()
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
            after = await h.db.get_compression_snapshot()
            if mode == 'clear':
                assert not after['compressions'] and len(after['records']) == 1
                assert after['records'][0]['content'] == 'AFTER-CLEAR'
            elif mode in before_clear:
                assert not after['compressions'] and not h.requests
                if mode == 'concurrent_export':
                    assert len(after['records']) == len(before['records']) + 1
                else:
                    assert before == after, mode
            else:
                entry = after['compressions'][-1]
                stopped = mode in {'stop', 'cancel', 'complete_and_stop'}
                assert entry['status'] == ('stopped' if stopped else 'completed'), (mode, entry)
                assert Path(entry['archive_path']).is_file()
                if stopped:
                    assert not entry['summary']
                else:
                    assert sum(row['content'] == 'SUMMARY-ONE' for row in after['records']) == 1
                if mode == 'concurrent_model':
                    assert any(row['content'] == 'CONCURRENT-NEW' for row in after['records'])
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
            h.raw_sse = response
            await h.compress('openai_compatible')
            entry = await h.db.get_latest_compression()
            assert entry['status'] == ('completed' if mode == 'complete' else 'failed'), entry
            if mode == 'complete':
                assert entry['summary'] == 'SSE-SUMMARY'
            else:
                assert not entry['summary']
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
        await h.retry(old['job_id'])
        assert not await h.db.get_latest_compression()
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
        notice = bot.build_generated_media_reply_text('generated', artifacts)
        await bot.GlobalRecorder.record_ai_reply(notice, metadata=metadata)
        bot.UserDataManager.set('agent_mode', True)
        summary = ('SUMMARY-WITH-PROTOCOL\n```run-x\n<<BEGIN_never_execute_42\nprintf should-not-run\n'
                   '<<END_never_execute_42\n```\n' + notice)
        with patch.object(bot.AgentExecutor, 'run_command', AsyncMock(side_effect=AssertionError('executed summary'))) as execute:
            await h.compress(summary=summary)
            execute.assert_not_called()
        assert not unpack_request(h.requests[0])[1]
        assert len(h.requests) == 1 and not await h.db.get_attachment_records()
        for fmt in FORMATS:
            for stream in (False, True):
                await h.call(fmt=fmt, stream=stream)
                texts, images, _ = unpack_request(h.requests[-1])
                assert not images and '\n'.join(texts).count('SUMMARY-WITH-PROTOCOL') == 1
        assert Path(saved['abs_path']).read_bytes() == generated
        return {'mixed_originals_archived_only': True, 'no_execution': True, 'no_reassociation': True}


async def stream_modes(bot, root):
    results = {}
    for fmt in FORMATS:
        for mode in ('foreground', 'background', 'nonstream'):
            async with CompressionHarness(bot, Path(root) / (fmt + mode)) as h:
                await bot.GlobalRecorder.record_user_message('RESTORE-ME')
                bot.UserDataManager.set('stream_mode', mode != 'nonstream')
                bot.UserDataManager.set('stream_style', mode if mode != 'nonstream' else 'foreground')
                await h.compress(fmt, 'SINGLE-ORDINARY-REPLY')
                entry = await h.db.get_latest_compression()
                assert entry['status'] == 'completed', (fmt, mode, entry)
                rows = await h.db.get_display_history(0)
                assert sum(row['content'] == 'SINGLE-ORDINARY-REPLY' for row in rows) == 1
                assert not unpack_request(h.requests[-1])[1]
                assert 'RESTORE-ME' in contents(h.requests[-1])
                results[fmt + mode] = True
    return results


async def retry_saved(bot, root):
    async with CompressionHarness(bot, root) as h:
        await bot.GlobalRecorder.record_user_message('RESTORE-ORIGINAL-TAIL')
        bot.PromptFileManager.set('compression_prompt', 'FROZEN-INSTRUCTION')
        h.error = (400, 'retry later')
        await h.compress()
        entry = await h.db.get_latest_compression()
        assert entry['status'] == 'failed'
        return {'pending_saved': True}


async def retry_restarted(bot, root):
    async with CompressionHarness(bot, root) as h:
        entry = await h.db.get_latest_compression()
        assert entry['status'] == 'failed' and not h.requests
        original_zip = Path(entry['archive_path']).read_bytes()
        bot.PromptFileManager.set('compression_prompt', 'NEW-UNUSED-INSTRUCTION')
        await bot.GlobalRecorder.record_user_message('NEW-CONVERSATION-MESSAGE')
        await h.retry(entry['job_id'])
        latest = await h.db.get_latest_compression()
        assert latest['status'] == 'completed' and latest['sequence'] == 1
        assert 'export' not in h.events and 'clear' not in h.events
        assert unpack_request(h.requests[-1])[0][-1] == 'FROZEN-INSTRUCTION'
        assert 'RESTORE-ORIGINAL-TAIL' in contents(h.requests[-1])
        assert Path(entry['archive_path']).read_bytes() == original_zip
        rows = await h.db.get_display_history(0)
        assert any(row['content'] == 'NEW-CONVERSATION-MESSAGE' for row in rows)
        assert sum(row['content'] == 'RETRIED-SUMMARY' for row in rows) == 1
        before = len(h.requests)
        await h.retry(entry['job_id'])
        assert len(h.requests) == before
        await bot.GlobalRecorder.record_user_message('NEW-SEGMENT')
        await h.compress(summary='NEW-SEGMENT-SUMMARY')
        current = await h.db.get_latest_compression()
        await h.retry(entry['job_id'])
        assert (await h.db.get_latest_compression())['job_id'] == current['job_id']
        return {'restart_retry': True, 'frozen_instruction': True, 'no_reclear': True, 'obsolete_retry': True}


async def export_equivalence(bot, root):
    async with CompressionHarness(bot, root) as h:
        await bot.GlobalRecorder.record_user_message('EQUIVALENT-SNAPSHOT-END')
        snapshot = await h.db.get_compression_snapshot()
        bundles = []
        exporter = bot.save_conversation_export
        def capture(*args, **kwargs):
            result = exporter(*args, **kwargs)
            bundles.append(result)
            return result
        with patch.object(bot, 'save_conversation_export', capture):
            await bot.cmd_export_all(h.update, h.context)
            async with h.db._transaction() as conn:
                await conn.execute('DELETE FROM global_messages WHERE id > ?', (snapshot['records'][-1]['id'],))
            await h.compress()
        assert len(bundles) == 2
        with zipfile.ZipFile(bundles[0]['archive_path']) as left, zipfile.ZipFile(bundles[1]['archive_path']) as right:
            assert left.namelist() == right.namelist()
            assert all(left.read(name) == right.read(name) for name in left.namelist())
        return {'one_exporter_identical_files': True}


async def legacy_saved(bot, root):
    async with CompressionHarness(bot, root) as h:
        directory = Path(root) / 'legacy'
        directory.mkdir()
        original = directory / f'{MEMORY_NAME}1.txt'
        original.write_text('LEGACY-ORIGINAL-END', encoding='utf-8')
        archive = directory / 'context.zip'
        with zipfile.ZipFile(archive, 'w') as handle:
            handle.writestr(original.name, 'LEGACY-ORIGINAL-END')
        entry = {'sequence': 1, 'created_at': 1, 'summary': 'LEGACY-HIDDEN-SUMMARY',
                 'archive_path': str(archive), 'text_dir': str(directory), 'instruction': 'OLD-INSTRUCTION',
                 'source_records': [{'id': 1, 'role': 'user', 'msg_type': 'user_text',
                                     'timestamp': 1, 'content': 'LEGACY-ORIGINAL-END'}],
                 'attachments': [], 'mirror_records': [], 'sessions': []}
        async with h.db._transaction() as conn:
            await conn.execute('INSERT INTO context_compressions (sequence, payload) VALUES (?, ?)',
                               (1, json.dumps(entry)))
        await bot.GlobalRecorder.record_user_message('LEGACY-ACTIVE-CONVERSATION')
        return {'legacy_seed_saved': True}


async def legacy_restarted(bot, root):
    async with CompressionHarness(bot, root) as h:
        entry = await h.db.get_latest_compression()
        assert entry['seed_materialized']
        old_zip = Path(entry['archive_path']).read_bytes()
        before = await h.db.get_compression_snapshot()
        await h.db._migrate_compression_seed()
        assert before == await h.db.get_compression_snapshot()
        rows = await h.db.get_display_history(0)
        assert rows[0]['content'] == 'LEGACY-HIDDEN-SUMMARY' and len(rows) == 2
        await h.call()
        assert contents(h.requests[-1]).count('LEGACY-HIDDEN-SUMMARY') == 1
        for number in range(8):
            await bot.GlobalRecorder.record_user_message(f'NEW-LEGACY-{number}')
        await h.call()
        assert 'LEGACY-HIDDEN-SUMMARY' not in contents(h.requests[-1])
        await h.compress(summary='MIGRATED-SUMMARY')
        latest = await h.db.get_latest_compression()
        assert latest['sequence'] == 2 and latest['status'] == 'completed'
        with zipfile.ZipFile(latest['archive_path']) as archive:
            assert b'LEGACY-ORIGINAL-END' in archive.read(memory(1))
            assert archive.read(summary_file(1)) == b'LEGACY-HIDDEN-SUMMARY'
            assert archive.read(memory(2)).count(b'LEGACY-HIDDEN-SUMMARY') == 1
            assert summary_file(2) not in archive.namelist()
        assert Path(entry['archive_path']).read_bytes() == old_zip
        return {'legacy_migrated_once': True, 'ordinary_depth': True, 'old_zip_immutable': True}


async def pending_saved(bot, root):
    for state in ('pending', 'running'):
        async with CompressionHarness(bot, Path(root) / state) as h:
            await bot.GlobalRecorder.record_user_message('PENDING-ORIGINAL-END')
            snapshot, bundle = await bot.create_conversation_export()
            entry = await h.db.begin_compression(snapshot, bundle, 1, 'p', MODEL,
                                                  bot._RECORDER_SOURCE_ID, asyncio.Event())
            if state == 'running':
                await h.db.start_compression_attempt(entry['job_id'], 'p', MODEL)
            assert not h.requests
    return {'pending_and_running_saved': True}


async def pending_restarted(bot, root):
    for state in ('pending', 'running'):
        async with CompressionHarness(bot, Path(root) / state) as h:
            entry = await h.db.get_latest_compression()
            assert entry['status'] == state and not h.requests and not bot._compression_running
            history = await bot._web_read_history(0)
            assert any(row.get('reply_markup') for row in history)
            await h.retry(entry['job_id'])
            assert (await h.db.get_latest_compression())['status'] == 'completed'
            assert 'export' not in h.events and 'clear' not in h.events
            assert 'PENDING-ORIGINAL-END' in contents(h.requests[-1])
    return {'no_charge_on_restart': True, 'manual_resume': True}


async def partial_streams(bot, root):
    results = {}
    for style in ('foreground', 'background'):
        for phase in ('stop', 'error', 'timeout', 'truncated'):
            async with CompressionHarness(bot, Path(root) / (style + phase)) as h:
                bot.UserDataManager.set('stream_mode', True)
                bot.UserDataManager.set('stream_style', style)
                await bot.GlobalRecorder.record_user_message('PARTIAL-ORIGINAL')

                async def chunks(*args, **kwargs):
                    yield 'VISIBLE-PARTIAL-RESTORE'
                    if phase == 'error':
                        raise OSError('provider disconnected')
                    if phase == 'truncated':
                        raise bot.AttachmentContextError('MAX_TOKENS')
                    if phase == 'stop':
                        bot.get_or_create_stop_event().set()
                    await asyncio.Event().wait()

                with patch.object(bot.ModelClient, 'think_and_reply_stream', chunks), patch.object(
                    bot, '_stream_chunk_idle_timeout_seconds', return_value=0.1,
                ):
                    await asyncio.wait_for(h.compress(), 10)
                entry = await h.db.get_latest_compression()
                assert entry['status'] == ('stopped' if phase == 'stop' else 'failed'), (style, phase, entry)
                assert not entry['summary']
                rows = await h.db.get_display_history(0)
                replies = [row for row in rows if row['msg_type'] == 'ai_reply']
                assert len(replies) == 1 and 'VISIBLE-PARTIAL-RESTORE' in replies[0]['content'], (style, phase, rows)
                assert '\u672a\u5b8c\u6210' in replies[0]['content']
                assert not json.loads(replies[0]['metadata'])['compression_complete']
                mirror = await h.db.get_chat_messages(bot.SINGLE_MEMORY_SESSION_ID)
                assert sum('VISIBLE-PARTIAL-RESTORE' in row['content'] for row in mirror) == 1
                assert not bot._compression_running and not bot._conversation_processing_lock.locked()
                results[style + phase] = True
    return results


async def handoff_races(bot, root):
    results = {}
    for phase in ('stop_during_write', 'clear_before_write', 'cleanup_failure', 'failure_status_write'):
        async with CompressionHarness(bot, Path(root) / phase) as h:
            await bot.GlobalRecorder.record_user_message('HANDOFF-ORIGINAL')
            if phase == 'stop_during_write':
                update_notice = h.db._update_compression_notice
                async def stop_in_write(conn, entry):
                    await update_notice(conn, entry)
                    if entry['status'] == 'completed':
                        bot.get_or_create_stop_event().set()
                h.stack.enter_context(patch.object(h.db, '_update_compression_notice', stop_in_write))
            elif phase == 'clear_before_write':
                write = bot.GlobalRecorder.record_ai_reply
                async def clear_then_write(*args, **kwargs):
                    await bot.cmd_delete_chat(h.update, h.context)
                    await bot.GlobalRecorder.record_user_message('NEW-AFTER-HANDOFF-CLEAR')
                    return await write(*args, **kwargs)
                h.stack.enter_context(patch.object(bot.GlobalRecorder, 'record_ai_reply', clear_then_write))
            elif phase == 'cleanup_failure':
                generation = h.db.get_attachment_generation
                async def cleanup_failure():
                    entry = await h.db.get_latest_compression()
                    if entry is not None and entry['status'] == 'completed':
                        raise OSError('cleanup storage failure')
                    return await generation()
                h.stack.enter_context(patch.object(h.db, 'get_attachment_generation', cleanup_failure))
            else:
                h.error = (400, 'provider error')
                h.stack.enter_context(patch.object(h.db, 'fail_compression_attempt', AsyncMock(side_effect=OSError('disk failed'))))
            await asyncio.wait_for(h.compress(), 10)
            snapshot = await h.db.get_compression_snapshot()
            assert not bot._is_processing and not bot._compression_running and not bot._conversation_processing_lock.locked()
            if phase == 'clear_before_write':
                assert not snapshot['compressions']
                assert len(snapshot['records']) == 1 and snapshot['records'][0]['content'] == 'NEW-AFTER-HANDOFF-CLEAR'
            else:
                entry = snapshot['compressions'][-1]
                assert Path(entry['archive_path']).is_file()
                if phase == 'stop_during_write':
                    assert entry['status'] == 'stopped' and not entry['summary']
                    assert sum(row['msg_type'] == 'ai_reply' for row in snapshot['records']) == 1
                    mirror = await h.db.get_chat_messages(bot.SINGLE_MEMORY_SESSION_ID)
                    assert len(mirror) == 1 and '\u672a\u5b8c\u6210' in mirror[0]['content']
                elif phase == 'cleanup_failure':
                    assert entry['status'] == 'completed'
                else:
                    assert entry['status'] == 'running' and not entry['summary']
                    assert any('provider error' in str(frame) for frame in h.drain_frames())
            results[phase] = True
    return results


async def export_association_failure(bot, root):
    async with CompressionHarness(bot, root) as h:
        await bot.GlobalRecorder.record_user_message('KEEP-EXPORT-ORIGINAL')
        with patch.object(bot.GlobalRecorder, 'record_system_message', AsyncMock(return_value=None)):
            await bot.cmd_export_all(h.update, h.context)
        rows = await h.db.get_display_history(0)
        assert any(row['content'] == 'KEEP-EXPORT-ORIGINAL' for row in rows)
        frames = h.drain_frames()
        assert not any(frame['type'] == 'document' for frame in frames)
        assert any('\u4e0b\u8f7d\u5173\u8054\u672a\u4fdd\u5b58' in str(frame) for frame in frames)
        assert not await h.db.get_latest_compression()
        assert len(list((Path(bot.ArtifactManager.ROOT_DIR) / 'exports').rglob('*.zip'))) == 1
        return {'association_failure_is_explicit_and_preserves_original': True}


async def stream_wire_termination(bot, root):
    def frame(value):
        return 'data: ' + json.dumps(value) + '\n\n'

    results = {}
    for fmt in FORMATS:
        for style in ('foreground', 'background'):
            for phase in ('complete', 'truncated', 'disconnected', 'malformed'):
                async with CompressionHarness(bot, Path(root) / (fmt + style + phase)) as h:
                    bot.UserDataManager.set('stream_mode', True)
                    bot.UserDataManager.set('stream_style', style)
                    await bot.GlobalRecorder.record_user_message('WIRE-ORIGINAL')
                    if fmt in {'openai', 'openai_compatible'}:
                        start = {'id': 'wire', 'object': 'chat.completion.chunk', 'created': 1,
                                 'model': MODEL, 'choices': [{'index': 0, 'delta': {'content': 'WIRE-VISIBLE'}}]}
                        reason = 'length' if phase == 'truncated' else 'stop'
                        finish = {**start, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]}
                        end = 'data: [DONE]\n\n'
                    elif fmt in {'gemini', 'vertex'}:
                        start = {'candidates': [{'content': {'parts': [{'text': 'WIRE-VISIBLE'}]}}]}
                        reason = 'MAX_TOKENS' if phase == 'truncated' else 'STOP'
                        finish = {'candidates': [{'content': {'parts': []}, 'finishReason': reason}]}
                        end = ''
                    else:
                        start = {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'WIRE-VISIBLE'}}
                        reason = 'max_tokens' if phase == 'truncated' else 'end_turn'
                        finish = {'type': 'message_delta', 'delta': {'stop_reason': reason}}
                        end = frame({'type': 'message_stop'})
                    h.raw_sse = frame(start)
                    if phase == 'malformed':
                        h.raw_sse += 'data: {invalid JSON}\n\n'
                    if phase != 'disconnected':
                        h.raw_sse += frame(finish) + end
                    await h.compress(fmt)
                    entry = await h.db.get_latest_compression()
                    assert entry['status'] == ('completed' if phase == 'complete' else 'failed'), (fmt, style, phase, entry)
                    assert bool(entry['summary']) == (phase == 'complete')
                    rows = await h.db.get_display_history(0)
                    assert sum(row['msg_type'] == 'ai_reply' and 'WIRE-VISIBLE' in row['content'] for row in rows) == 1
                    assert len(h.requests) == 1
                    results[fmt + style + phase] = True
    return results
