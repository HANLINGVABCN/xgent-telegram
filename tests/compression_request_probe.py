"""Shared compression harness, wire checks, and archive compatibility scenarios."""

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

        def exported(*args, **kwargs):
            result = export(*args, **kwargs)
            self.events.append('export')
            return result

        async def began(*args, **kwargs):
            result = await begin(*args, **kwargs)
            self.events.append('begin')
            return result

        self.stack.enter_context(patch.object(self.bot, 'save_conversation_export', exported))
        self.stack.enter_context(patch.object(self.db, 'begin_compression', began))
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
            from tests.lossless_context_probe import latest_job
            entry = await latest_job(h.db)
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
        assert await h.db.get_display_message(old_id) is not None  # History survives reset.
        assert Path(old['archive_path']).is_file()
        await h.retry(old['job_id'])
        assert not await h.db.get_latest_compression()
        await bot.GlobalRecorder.record_user_message('NEW-CHAIN')
        await h.compress(summary='NEW-CHAIN-SUMMARY')
        assert (await h.db.get_latest_compression())['sequence'] == old['sequence'] + 1
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
        assert unpack_request(h.requests[0])[1] == [uploaded, generated]
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
            assert set(left.namelist()) - set(right.namelist()) == {'上下文范围.txt'}
            assert all(left.read(name) == right.read(name) for name in right.namelist())
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
            await conn.execute("INSERT INTO context_compressions (session_id, sequence, payload) VALUES ('global_memory', ?, ?)",
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
            from tests.lossless_context_probe import latest_job
            entry = await latest_job(h.db)
            assert entry['status'] == state and not h.requests and not bot._compression_running
            history = await bot._web_read_history(0)
            assert any(row.get('content') == 'PENDING-ORIGINAL-END' for row in history)
            await h.retry(entry['job_id'])
            assert (await h.db.get_latest_compression())['status'] == 'completed'
            assert 'export' in h.events and 'begin' in h.events
            assert 'PENDING-ORIGINAL-END' in contents(h.requests[-1])
    return {'no_charge_on_restart': True, 'manual_resume': True}


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
                    from tests.lossless_context_probe import latest_job
                    entry = await latest_job(h.db)
                    assert entry['status'] == ('completed' if phase == 'complete' else 'failed'), (fmt, style, phase, entry)
                    assert bool(entry['summary']) == (phase == 'complete')
                    rows = await h.db.get_display_history(0)
                    assert sum(row['msg_type'] == 'ai_reply' and 'WIRE-VISIBLE' in row['content'] for row in rows) == (1 if phase == 'complete' else 0)
                    assert len(h.requests) == 1
                    results[fmt + style + phase] = True
    return results
