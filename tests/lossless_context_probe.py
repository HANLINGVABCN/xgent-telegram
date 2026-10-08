"""Lossless tool replay and transactional compression using real SQLite/adapters."""
import asyncio
import base64
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.attachment_request_probe import FORMATS, MODEL, LONG_TEXT, unpack_request, expected_images
from tests.compression_request_probe import CompressionHarness, contents
from tests.test_attachments import image_bytes
from xgent_app.attachments import AttachmentContextError
from xgent_app.compression import metadata_of, DEFAULT_COMPRESSION_PROMPT


async def latest_job(db):
    conn = await db._get_conn()
    cur = await conn.execute('SELECT payload FROM context_compression_jobs ORDER BY rowid DESC LIMIT 1')
    row = await cur.fetchone()
    return json.loads(row['payload']) if row else None


def assert_preserved(before, after):
    assert after['generation'] == before['generation']
    original = {r['id']: r for r in before['records']}
    retained = {r['id']: r for r in after['records']}
    assert all(retained.get(key) == value for key, value in original.items())
    assert after['mirror_records'] == before['mirror_records']
    assert after['sessions'] == before['sessions']
    assert after['compressions'] == before['compressions']
    assert all(r['msg_type'] != 'ai_reply' for r in after['records'] if r['id'] not in original)


async def depth(bot, root):
    async with CompressionHarness(bot, root) as h:
        for i in range(300):
            for kind, role, text in [
                (bot.MessageType.USER_TEXT, 'user', f'Q-{i}'),
                (bot.MessageType.AI_REPLY, 'assistant', f'A-{i}'),
                (bot.MessageType.TOKEN_USAGE, 'system', 'usage'),
                (bot.MessageType.AGENT_STATUS, 'assistant', 'status'),
            ]:
                await h.db.record_global_message(1, 1, kind, role, text)
        history = await h.db.get_conversation_messages(1000)
        assert len(history) == 600 and history[0]['content'] == 'Q-0'
        short = await h.db.get_conversation_messages(10)
        assert len(short) == 10 and short[0]['content'] == 'Q-295'
        return {'ui_not_counted': True, 'effective_limit': True}


async def tool_replay(bot, root):
    results = {}
    for fmt in FORMATS:
        async with CompressionHarness(bot, Path(root) / fmt) as h:
            bot.UserDataManager.set('agent_mode', True)
            bot.UserDataManager.set('readx_persist_context', True)
            bot.UserDataManager.set('global_depth', 1000)
            bot.UserDataManager.get('providers')['p']['api_format'] = fmt
            text = h.root / 'source.txt'
            text.write_text('EXACT-READ-TEXT-ORIGINAL', encoding='utf-8')
            image = h.root / 'source.png'
            raw = image_bytes(color='blue')
            image.write_bytes(raw)
            blocks = ''.join(f"```read-x\n<<BEGIN_read_{i}\n{p}\n<<END_read_{i}\n```\n"
                             for i, p in enumerate((text, image)))
            h.replies = [blocks, 'DONE']
            await h.turn('read both')
            assert len(h.requests) == 2
            live_texts, live_images, _ = unpack_request(h.requests[-1])
            assert live_images == [raw]
            assert sum(t.count('EXACT-READ-TEXT-ORIGINAL') for t in live_texts) == 1
            text.write_text('CHANGED-ON-DISK', encoding='utf-8')
            image.write_bytes(image_bytes(color='red'))
            h.replies = ['NEXT']
            await h.turn('new question')
            texts, images, _ = unpack_request(h.requests[-1])
            assert images == [raw]
            assert sum(t.count('EXACT-READ-TEXT-ORIGINAL') for t in texts) == 1
            assert 'CHANGED-ON-DISK' not in '\n'.join(texts)
            rows = await h.db.get_tool_context_records()
            assert len(rows) == 2
            assert base64.b64encode(raw).decode() not in json.dumps(rows)
            # Turning off affects new operations only; old retained content remains.
            bot.UserDataManager.set('readx_persist_context', False)
            bot.UserDataManager.set('global_depth', 1)
            await h.turn('outside ordinary history window')
            assert unpack_request(h.requests[-1])[1] == [raw]
            assets = [a for r in rows for a in metadata_of(r)['model_context']['assets']]
            assert assets
            Path(assets[0]['path']).write_bytes(b'corrupted')
            count = len(h.requests)
            try:
                await h.call(fmt)
            except AttachmentContextError:
                pass
            else:
                raise AssertionError('changed retained payload silently dropped')
            assert len(h.requests) == count
            results[fmt] = True
    return results


async def standard_results(bot, root):
    from xgent_app.agent_context import (build_run_context_message, build_edit_context_message,
        build_grep_context_message, build_search_context_message, build_fetch_context_message,
        build_intel_context_message)
    builders = dict(run=build_run_context_message, edit=build_edit_context_message,
                    grep=build_grep_context_message, search=build_search_context_message,
                    fetch=build_fetch_context_message, intel=build_intel_context_message)
    results = {}
    async with CompressionHarness(bot, root) as h:
        bot.UserDataManager.set('agent_mode', True)
        bot.UserDataManager.set('readx_persist_context', True)
        bot.UserDataManager.set('global_depth', 1000)
        for kind, build in builders.items():
            expected = build(f'EXACT-{kind}-RESULT')
            operation = {'success': True, 'kind': kind, 'notice': f'DISPLAY-{kind}',
                         'output': f'DISPLAY-{kind}', 'context_message': expected}
            h.replies = [f"```run-x\n<<BEGIN_run_test\necho test\n<<END_run_test\n```", 'DONE']
            with patch.object(bot, 'dispatch_standard_protocol', AsyncMock(return_value=operation)):
                await h.turn('execute ' + kind)
            assert expected['content'] in contents(h.requests[-1])
            await h.turn('continue ' + kind)
            assert contents(h.requests[-1]).count(expected['content']) == 1
            results[kind] = True
        return results


async def provider_matrix(bot, root):
    results = {}
    for fmt in FORMATS:
        for style in ('foreground', 'background', 'nonstream'):
            async with CompressionHarness(bot, Path(root) / (fmt + style)) as h:
                await h.seed()
                original = 'OLDEST-REQUIREMENT\n' + 'complete original ' * 3000 + 'GLOBAL-TAIL'
                await bot.GlobalRecorder.record_user_message(original)
                bot.PromptFileManager.set('assistant_prompt', 'EXCLUDED-SYSTEM-PROMPT')
                bot.PromptFileManager.set('agent_prompt_addon', 'EXCLUDED-AGENT-PROMPT')
                memory_dir = h.root / 'memory'
                memory_dir.mkdir()
                (memory_dir / 'fixture.txt').write_text('EXCLUDED-LONG-TERM-MEMORY', encoding='utf-8')
                h.stack.enter_context(patch.object(bot, 'MEMORY_DIR', str(memory_dir)))
                bot.UserDataManager.set('stream_mode', style != 'nonstream')
                bot.UserDataManager.set('stream_style', style if style != 'nonstream' else 'foreground')
                before = await h.db.get_compression_snapshot()
                async def observe(provider, data, model, history, stop_event, usages):
                    live = await h.db.get_compression_snapshot()
                    assert_preserved(before, live)
                    assert not live['compressions']
                    return await generate(provider, data, model, history, stop_event, usages)
                generate = bot.generate_compression_summary
                with patch.object(bot, 'generate_compression_summary', observe):
                    await h.compress(fmt)
                latest = await h.db.get_latest_compression()
                assert latest and latest['status'] == 'completed', (fmt, style, latest)
                texts, images, _ = unpack_request(h.requests[-1])
                joined = '\n'.join(texts)
                assert images == expected_images()
                assert joined.count(LONG_TEXT) == 1 and original in joined
                assert texts[-1] == latest['instruction']
                assert texts[-1].strip() == DEFAULT_COMPRESSION_PROMPT
                assert joined.count(DEFAULT_COMPRESSION_PROMPT) == 1
                assert all(marker not in json.dumps(h.requests[-1]) for marker in (
                    'EXCLUDED-SYSTEM-PROMPT', 'EXCLUDED-AGENT-PROMPT', 'EXCLUDED-LONG-TERM-MEMORY'))
                assert await h.db.get_attachment_generation() == before['generation'] + 1
                assert not await h.db.get_attachment_records()
                with zipfile.ZipFile(latest['archive_path']) as archive:
                    assert archive.testzip() is None
                frames = h.drain_frames()
                events = [f for f in frames if f['type'] in {'history_reset', 'compression_state'}]
                assert all(f.get('conversation_id') == 'global_memory' for f in events)
                events = [{k:v for k,v in f.items() if k not in {'conversation_id','generation','run_id','conversation_name'}} for f in events]
                assert events == [{'type': 'compression_state', 'busy': True}, {'type': 'history_reset'},
                                  {'type': 'compression_state', 'busy': False, 'committed': True}]
                await h.call(fmt)
                assert contents(h.requests[-1]).count('SUMMARY-ONE') == 1
                results[fmt + style] = True
    return results


async def failures(bot, root):
    results = {}
    modes = ('upstream', 'empty', 'truncated', 'media', 'limit', 'missing', 'export',
             'archive', 'prompt', 'delete_write', 'reply_write', 'commit', 'stop', 'cancel')
    for mode in modes:
        async with CompressionHarness(bot, Path(root) / mode) as h:
            ref = await h.add_upload(image_bytes(), 'original.png')
            await bot.GlobalRecorder.record_user_message('KEEP-ORIGINAL')
            before = await h.db.get_compression_snapshot()
            if mode == 'upstream': h.error = (400, 'provider failure')
            if mode == 'truncated': h.finish_reason = 'length'
            if mode == 'missing': (Path(bot.ArtifactManager.UPLOAD_DIR) / ref['path']).unlink()
            if mode == 'limit': bot.UserDataManager.set('model_request_limits', {f'p/{MODEL}': {'max_request_bytes': 1}})
            if mode == 'prompt': Path(bot.PromptFileManager.get_abs_path('compression_prompt')).write_text('', encoding='utf-8')
            if mode == 'export': h.stack.enter_context(patch.object(bot, 'save_conversation_export', side_effect=OSError('disk')))
            if mode == 'archive': h.stack.enter_context(patch.object(bot, 'verify_export', side_effect=OSError('archive')))
            if mode == 'delete_write':
                clear = h.db._clear_conversation_memory
                async def fail_clear(*a, **k):
                    await clear(*a, **k)
                    raise OSError('delete rolled back')
                h.stack.enter_context(patch.object(h.db, '_clear_conversation_memory', fail_clear))
            if mode == 'reply_write':
                insert = h.db._insert_global_record
                async def fail_insert(conn, chat_id, user_id, msg_type, *a):
                    if msg_type == bot.MessageType.AI_REPLY: raise OSError('reply write')
                    return await insert(conn, chat_id, user_id, msg_type, *a)
                h.stack.enter_context(patch.object(h.db, '_insert_global_record', fail_insert))
            if mode == 'commit':
                conn = await h.db._get_conn()
                commit = conn.commit
                async def fail_commit():
                    cur = await conn.execute('SELECT COUNT(*) FROM context_compressions')
                    if (await cur.fetchone())[0]: raise OSError('commit')
                    await commit()
                h.stack.enter_context(patch.object(conn, 'commit', fail_commit))
            if mode in {'stop', 'cancel'}:
                async def interrupted(*a):
                    if mode == 'cancel': raise asyncio.CancelledError()
                    bot._stop_generation_event.set()
                    return 'PARTIAL-NOT-TO-PERSIST'
                h.stack.enter_context(patch.object(bot, 'generate_compression_summary', interrupted))
            try:
                await h.compress(summary='' if mode == 'empty' else 'data:image/png;base64,AAAA' if mode == 'media' else 'SUMMARY')
            except asyncio.CancelledError:
                assert mode == 'cancel'
            after = await h.db.get_compression_snapshot()
            assert_preserved(before, after)
            extra = [r for r in after['records'] if r['id'] not in {r['id'] for r in before['records']}]
            assert len(extra) == 1 and extra[0]['msg_type'] == 'system_op', (mode, extra)
            assert '原上下文已保留' in extra[0]['content']
            assert '原上下文已保留' in json.dumps(await h.db.get_conversation_messages(1000), ensure_ascii=False)
            assert not any(f['type'] == 'history_reset' for f in h.drain_frames())
            assert not bot._compression_running and not bot._conversation_processing_lock.locked()
            results[mode] = True
    return results


async def races(bot, root):
    results = {}
    for mode in ('new_message', 'clear', 'stop_inside_commit', 'ui_only', 'delivery'):
        async with CompressionHarness(bot, Path(root) / mode) as h:
            await bot.GlobalRecorder.record_user_message('ORIGINAL')
            before = await h.db.get_compression_snapshot()
            commit = h.db.commit_compression
            async def changed(entry, summary, stop):
                if mode == 'new_message': await bot.GlobalRecorder.record_user_message('NEW-DURING-COMPRESSION')
                if mode == 'clear': await h.db.clear_all_conversation_memory()
                if mode == 'ui_only': await h.db.record_global_message(1, 0, bot.MessageType.AGENT_STATUS, 'assistant', 'UI-ONLY')
                return await commit(entry, summary, stop)
            h.stack.enter_context(patch.object(h.db, 'commit_compression', changed))
            if mode == 'stop_inside_commit':
                clear = h.db._clear_conversation_memory
                async def stopped(*a, **k):
                    value = await clear(*a, **k)
                    bot._stop_generation_event.set()
                    return value
                h.stack.enter_context(patch.object(h.db, '_clear_conversation_memory', stopped))
            if mode == 'delivery': h.stack.enter_context(patch.object(bot, 'safe_send_message', AsyncMock(side_effect=OSError('telegram'))))
            await h.compress()
            after = await h.db.get_compression_snapshot()
            if mode == 'clear':
                assert not after['records'] and not after['compressions']
            elif mode in {'ui_only', 'delivery'}:
                assert after['compressions'][-1]['status'] == 'completed'
                assert after['compressions'][-1]['summary'] == 'SUMMARY-ONE'
            else:
                assert_preserved(before, after)
                if mode == 'new_message': assert any(r['content'] == 'NEW-DURING-COMPRESSION' for r in after['records'])
            results[mode] = True
    return results


async def retry_saved(bot, root):
    async with CompressionHarness(bot, root) as h:
        await bot.GlobalRecorder.record_user_message('ORIGINAL-BEFORE-FAILURE')
        h.error = (400, 'fail')
        await h.compress()
        job = await latest_job(h.db)
        assert job['status'] == 'failed'
        Path(job['archive_path']).write_bytes(b'old archive corrupted intentionally')
        return {'saved': True}


async def retry_restarted(bot, root):
    async with CompressionHarness(bot, root) as h:
        old = await latest_job(h.db)
        await bot.GlobalRecorder.record_user_message('LATEST-AFTER-FAILURE')
        bot.PromptFileManager.set('compression_prompt', 'LATEST-INSTRUCTION')
        await h.retry(old['job_id'])
        current = await h.db.get_latest_compression()
        assert current and current['status'] == 'completed'
        assert current['archive_path'] != old['archive_path']
        text = contents(h.requests[-1])
        assert 'ORIGINAL-BEFORE-FAILURE' in text and 'LATEST-AFTER-FAILURE' in text
        assert unpack_request(h.requests[-1])[0][-1] == 'LATEST-INSTRUCTION'
        assert text.count('LATEST-INSTRUCTION') == 1
        count = len(h.requests)
        await h.retry(current['job_id'])
        assert len(h.requests) == count
        return {'latest_history_and_instruction': True, 'no_duplicate_commit': True}


async def nonstandard_results(bot, root):
    results = {}
    async with CompressionHarness(bot, root) as h:
        bot.UserDataManager.set('agent_mode', True)
        bot.UserDataManager.set('readx_persist_context', True)
        bot.UserDataManager.set('global_depth', 1000)
        for kind in ('sendfile', 'file', 'file_base64', 'shell', 'stdin', 'shellkill', 'ask'):
            block = {'type': kind, 'body': 'invalid-ask-body' if kind == 'ask' else 'body',
                     'path': str(h.root / 'output.txt')}
            async def fake_shell(*a, **k):
                return {'session_id': 'session-1', 'command': 'echo hello',
                        'output': 'EXACT-SHELL-' + kind,
                        'result': {'success': True, 'running': False, 'exit_code': 0}}
            with patch.object(bot.AgentExecutor, 'extract_protocol_blocks',
                              side_effect=lambda text: [block] if text == 'DO-TOOL' else []), \
                 patch.object(bot, 'dispatch_standard_protocol', AsyncMock(return_value=None)), \
                 patch.object(bot, 'execute_sendfile_protocol', AsyncMock(return_value='EXACT-SENDFILE')), \
                 patch.object(bot, 'write_text_protocol_file', AsyncMock(return_value={})), \
                 patch.object(bot, 'write_base64_protocol_file', AsyncMock(return_value={})), \
                 patch.object(bot, 'send_written_agent_file', AsyncMock(return_value='EXACT-FILE-' + kind)), \
                 patch.object(bot, 'execute_shell_protocol', fake_shell):
                h.replies = ['DO-TOOL', 'DONE']
                await h.turn('perform ' + kind)
            rows = await h.db.get_tool_context_records()
            expected = metadata_of(rows[-1])['model_context']['messages'][0]['content']
            assert expected in contents(h.requests[-1]), kind
            await h.turn('next question ' + kind)
            assert contents(h.requests[-1]).count(expected) == 1, kind
            results[kind] = True
        answer = await bot.persist_ask_answer('SAFE-FORM-ANSWER', 1, await h.db.get_attachment_generation())
        history = await h.db.get_conversation_messages(1000)
        assert history[-1]['content'] == answer['content']
        results['ask_answer'] = True
        return results


async def replay_saved(bot, root):
    async with CompressionHarness(bot, root) as h:
        from xgent_app.tool_context import freeze_tool_message
        message = {'role': 'user', 'content': [
            {'type': 'text', 'text': 'RESTART-TOOL-CONTENT'},
            {'type': 'binary', 'filename': 'original.pdf', 'mime_type': 'application/pdf',
             'data': base64.b64encode(b'%PDF-1.7 RETAINED-ORIGINAL').decode()},
        ]}
        payload = freeze_tool_message(message, bot.ArtifactManager.save_binary_upload)
        await h.db.persist_tool_context([], payload, 1, await h.db.get_attachment_generation())
        await bot.GlobalRecorder.record_user_message('RESTART-QUESTION')
        return {'persisted': True}


async def replay_restarted(bot, root):
    async with CompressionHarness(bot, root) as h:
        await h.call()
        request = json.dumps(h.requests[-1])
        assert 'RESTART-TOOL-CONTENT' in request
        assert base64.b64encode(b'%PDF-1.7 RETAINED-ORIGINAL').decode() in request
        await h.compress()
        request = json.dumps(h.requests[-1])
        assert 'RESTART-TOOL-CONTENT' in request
        assert base64.b64encode(b'%PDF-1.7 RETAINED-ORIGINAL').decode() in request
        assert (await h.db.get_latest_compression())['status'] == 'completed'
        return {'restart_and_compression_native_binary': True}


async def blocked_binary(bot, root):
    async with CompressionHarness(bot, root) as h:
        raw = b'\x00\x01\x02 native binary'
        saved = bot.ArtifactManager.save_binary_upload('input.bin', raw)
        ref = bot.ArtifactManager.attachment_reference(saved, 'input.bin', raw,
                                                     mime_type='application/octet-stream', expected_binary=True)
        await bot.GlobalRecorder.record_attachment_message('BINARY', bot.MessageType.USER_FILE, 1, [ref])
        before = await h.db.get_compression_snapshot()
        await h.compress('openai')
        assert not h.requests
        assert_preserved(before, await h.db.get_compression_snapshot())
        assert (await latest_job(h.db))['status'] == 'failed'
        return {'unsupported_binary_not_degraded': True}


async def live_stop(bot, root):
    results = {}
    for mode in ('stop', 'timeout'):
        async with CompressionHarness(bot, Path(root) / mode) as h:
            await bot.GlobalRecorder.record_user_message('LIVE-ORIGINAL')
            before = await h.db.get_compression_snapshot()
            bot.UserDataManager.set('stream_mode', True)
            closed = asyncio.Event()
            async def stream(*a, **k):
                try:
                    yield 'PARTIAL-NEVER-SHOWN'
                    if mode == 'stop': bot._stop_generation_event.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            with patch.object(bot.ModelClient, 'think_and_reply_stream', stream), \
                 patch.object(bot, '_stream_chunk_idle_timeout_seconds', return_value=0.02):
                await h.compress()
            assert closed.is_set()
            assert_preserved(before, await h.db.get_compression_snapshot())
            assert 'PARTIAL-NEVER-SHOWN' not in json.dumps(h.drain_frames())
            results[mode] = True
    return results


async def compressed_restarted(bot, root):
    async with CompressionHarness(bot, root) as h:
        latest = await h.db.get_latest_compression()
        assert latest and latest['status'] == 'completed'
        await h.call()
        wire = json.dumps(h.requests[-1])
        assert wire.count('SUMMARY-ONE') == 1
        assert 'RESTART-TOOL-CONTENT' not in wire
        assert not await h.db.get_tool_context_records()
        return {'committed_summary_survives_restart': True}


async def export_before_compression_status(bot, root):
    results = {}
    for mode in ('command', 'callback', 'retry', 'export_failure', 'delivery_failure'):
        async with CompressionHarness(bot, Path(root) / mode) as h:
            await bot.GlobalRecorder.record_user_message('ORDER-ORIGINAL')
            retry_id = None
            if mode == 'retry':
                h.error = (400, 'first attempt failed')
                await h.compress()
                retry_id = (await latest_job(h.db))['job_id']
                h.error = None
                h.drain_frames()
            if mode == 'export_failure':
                h.stack.enter_context(patch.object(bot, 'save_conversation_export', side_effect=OSError('export failed')))
            if mode == 'delivery_failure':
                h.stack.enter_context(patch.object(h.context.bot, 'send_document', AsyncMock(side_effect=OSError('delivery failed'))))
            if retry_id:
                await h.retry(retry_id)
            else:
                await h.compress(via_callback=mode == 'callback')
            frames = h.drain_frames()
            archives = [i for i, f in enumerate(frames)
                        if f['type'] == 'document' and f.get('filename') == '系统记忆.zip']
            statuses = [i for i, f in enumerate(frames)
                        if f['type'] == 'message' and f.get('text', '').startswith('正在压缩')]
            if mode == 'export_failure':
                assert not archives and not statuses and not h.requests
            else:
                assert len(statuses) == 1
                assert frames[statuses[0]].get('reply_markup'), 'Stop button must remain available'
                if mode == 'delivery_failure':
                    assert not archives
                    warnings = [i for i, f in enumerate(frames)
                                if f['type'] == 'message' and '文件投递失败' in f.get('text', '')]
                    assert len(warnings) == 1 and warnings[0] < statuses[0]
                else:
                    assert len(archives) == 1 and archives[0] < statuses[0], frames
                    assert frames[archives[0]]['message_id'] < frames[statuses[0]]['message_id']
                assert (await h.db.get_latest_compression())['status'] == 'completed'
            results[mode] = True
    return results
