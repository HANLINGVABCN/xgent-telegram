import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.attachment_request_probe import Harness, unpack_request
from xgent_app.attachment_versions import AttachmentVersions, VersionError
from xgent_app.attachment_version_api import VersionDownload
from xgent_app.conversations import current_scope, bind_conversation
from xgent_app.workbench import Workbench, WorkbenchError


async def create(h, text=b'original UTF8\n'):
    payload = h.bot.build_document_attachment_payload('notes.txt', text, 'caption remains')
    ref = payload['attachment']
    row = await h.bot.GlobalRecorder.record_attachment_message(payload['index_text'], 'user_file', 1, [ref])
    return row, ref, Path(payload['saved_file']['abs_path'])


async def api_cycle(bot, root):
    async with Harness(bot, root) as h:
        w = Workbench(vars(bot))
        row, ref, path = await create(h)
        cid = current_scope().conversation_id
        data = {'id': f"{row}:{ref['id']}", 'conversation_id': cid}
        state = await w.handle('GET', 'artifacts/versions', data)
        assert state['seed_kind'] == 'original' and state['can_restore']
        original = state['seed']
        raw = 'latest UTF16 中文\n' * 7000
        path.write_bytes(raw.encode('utf-16'))
        latest = await w.handle('GET', 'artifacts/versions', data)
        assert latest['count'] == 2 and latest['versions'][0]['encoding'] == 'utf-16'
        current = latest['current_file']['id']
        preview = await w.handle('GET', 'artifacts/version_preview', {**data, 'version_id': current})
        next_page = await w.handle('GET', 'artifacts/version_preview', {**data, 'version_id': current, 'offset': preview['next_offset']})
        assert (preview['text'] + next_page['text']) == raw[:next_page['next_offset']]
        download = await w.handle('GET', 'artifacts/version_download', {**data, 'version_id': original})
        assert isinstance(download, VersionDownload) and download.content == b'original UTF8\n'
        before = await h.db.get_display_message(row)
        h.drain_frames()
        request = {**data, 'version_id': original, 'generation': latest['generation'],
                   'fingerprint': latest['current_file']['fingerprint'], 'confirm': True}
        async with bot.get_conversations().operation(execution=True):
            try:
                await w.handle('POST', 'artifacts/restore_version', request)
            except WorkbenchError as exc:
                assert exc.status == 409
            else:
                raise AssertionError('restore stole active execution slot')
        assert path.read_bytes() == raw.encode('utf-16')
        with patch.object(bot, 'get_web_outbox', return_value=h.outbox):
            result = await w.handle('POST', 'artifacts/restore_version', request)
        assert path.read_bytes() == b'original UTF8\n' and result['restored_version'] == original
        after = await h.db.get_display_message(row)
        assert (before['content'], before['timestamp']) == (after['content'], after['timestamp'])
        assert json.loads(after['metadata'])['attachments'][0]['encoding'] == 'utf-8'
        assert [frame['type'] for frame in h.drain_frames()] == ['attachment_changed']
        await h.call()
        text = '\n'.join(unpack_request(h.requests[-1])[0])
        assert 'original UTF8' in text and raw[:50] not in text
        assert len((await h.db.get_export_snapshot())['records']) == 1, 'restore must not add chat messages'
        return {'roundtrip': True, 'busy_no_mutation': True, 'pagination_exact': True, 'current_only': True}


async def rejected_and_rollback(bot, root):
    async with Harness(bot, root) as h:
        w = Workbench(vars(bot))
        row, ref, path = await create(h)
        cid = current_scope().conversation_id
        data = {'id': f"{row}:{ref['id']}", 'conversation_id': cid}
        path.write_bytes(b'must keep before restore')
        state = await w.handle('GET', 'artifacts/versions', data)
        request = {**data, 'version_id': state['seed'], 'fingerprint': state['current_file']['fingerprint'],
                   'generation': state['generation'], 'confirm': True}
        original_tx = h.db._transaction
        @asynccontextmanager
        async def fail_commit():
            async with original_tx() as conn:
                yield conn
                raise OSError('fixture failure before commit')
        with patch.object(h.db, '_transaction', fail_commit):
            try: await w.handle('POST', 'artifacts/restore_version', request)
            except WorkbenchError: pass
            else: raise AssertionError('commit failure swallowed')
        assert path.read_bytes() == b'must keep before restore'
        assert json.loads((await h.db.get_display_message(row))['metadata'])['attachments'][0]['sha256'] == ref['sha256']
        # A deleted upload can be recovered; a reset still blocks restoration.
        path.unlink()
        request['fingerprint'] = 'missing'
        await w.handle('POST', 'artifacts/restore_version', request)
        await h.db.clear_all_conversation_memory()
        state = await w.handle('GET', 'artifacts/versions', data)
        assert not state['can_restore']
        try: await w.handle('POST', 'artifacts/restore_version', {**request, 'generation': state['generation']})
        except WorkbenchError: pass
        else: raise AssertionError('restored prior-context attachment')
        await bot.get_conversations().manage('archive', cid)
        assert (await w.handle('GET', 'artifacts/versions', data))['versions']
        await h.db.begin_conversation_delete(cid)
        await h.db.finish_conversation_delete(cid)
        try: await w.handle('GET', 'artifacts/version_download', {**data, 'version_id': ref['sha256']})
        except WorkbenchError: pass
        else: raise AssertionError('deleted association still authorizes versions')
        return {'rollback': True, 'missing_restore': True, 'generation_guard': True, 'deleted_denied': True}


async def ingress_failure_and_isolation(bot, root):
    async with Harness(bot, root) as h:
        with patch.object(AttachmentVersions, '_write', side_effect=OSError('fixture no disk')):
            await bot.process_incoming_document(h.update, h.context, doc_name='ordinary.txt', content_bytes=b'ordinary', caption='')
        assert not await h.db.get_attachment_records() and not h.requests
        bot.UserDataManager.set('state', bot.BotState.SET_COMMAND_BLACKLIST)
        with patch.object(AttachmentVersions, 'original', side_effect=AssertionError('configuration was backed up')):
            await bot.process_incoming_document(h.update, h.context, doc_name='rules.txt', content_bytes=b'fixture-blocked-command', caption='')
        assert not list((Path(bot.ArtifactManager.ROOT_DIR) / 'attachment_versions').glob('*/index.json'))
        return {'failed_upload_not_accepted': True, 'configuration_excluded': True}


async def set_bot(bot, root):
    async with Harness(bot, root) as h:
        manager = bot.get_conversations()
        w = Workbench(vars(bot))
        a = current_scope().conversation_id
        b = (await manager.manage('create', name='Target Bot'))['conversation_id']
        generation = (await h.db.get_session(b))['generation']
        before = await manager.state()
        request = {'id': b, 'generation': generation, 'expected_bot': a}
        records_before = len((await h.db.get_export_snapshot())['records'])
        notices = []
        with patch.object(bot, '_notify_owner_soon', side_effect=notices.append):
            result = await w.handle('POST', 'conversations/set_bot', request)
            assert result['telegram_conversation_id'] == b and result['current_chat_id'] == before['current_chat_id']
            assert (await w.handle('POST', 'conversations/set_bot', request))['revision'] == result['revision']
        assert len(notices) == 1 and '「Target Bot」' in notices[0], notices
        assert len((await h.db.get_export_snapshot())['records']) == records_before, 'notice must not enter history'
        c = (await manager.manage('create', name='Another'))['conversation_id']
        await manager.manage('switch', a, selector='telegram')
        for invalid in ({**request, 'expected_bot': c}, {**request, 'generation': generation + 99}):
            try: await w.handle('POST', 'conversations/set_bot', invalid)
            except WorkbenchError: pass
            else: raise AssertionError('stale selection accepted')
        with patch.object(bot.BotConfig, 'TOKEN', ''):
            assert not (await w.handle('GET', 'conversations', {}))['telegram_configured']
            try: await w.handle('POST', 'conversations/set_bot', request)
            except WorkbenchError: pass
            else: raise AssertionError('unconfigured Bot selectable')
        return {'cas': True, 'independent_selection': True, 'no_token_disabled': True, 'notice_once_unrecorded': True}


async def set_bot_joins_current_reply(bot, root):
    from xgent_app.interaction import interaction
    from xgent_app.fanout import ChannelWorker
    from xgent_app.web_bridge import MirrorBot, deliver_op_to_bot, markup_from_frame
    from xgent_app.reply_presentation import track_reply_presentation
    from tests.test_web_run_delivery import NativeBot
    async with Harness(bot, root) as h:
        manager = bot.get_conversations()
        a = current_scope().conversation_id
        b = (await manager.manage('create', name='Web reply'))['conversation_id']
        native = NativeBot()
        async def deliver(op, mid):
            return await deliver_op_to_bot(native, op, mid, markup_builder=markup_from_frame)
        channel = ChannelWorker('set-bot-test', deliver)
        mirror = MirrorBot(h.outbox, 1, native, channel=channel)
        w = Workbench(vars(bot))
        try:
            with interaction('web', 'chat'):
                async with manager.operation(b, execution=True, fresh=True):
                    @track_reply_presentation
                    async def reply():
                        message = await mirror.send_message(text='already produced')
                        assert not native.messages
                        request = {'id': b, 'generation': current_scope().generation, 'expected_bot': a}
                        await w.handle('POST', 'conversations/set_bot', request)
                        await message.edit_text('continued answer')
                        assert await mirror.flush_telegram()
                        await w.handle('POST', 'conversations/set_bot', request)
                        assert await mirror.flush_telegram()
                        assert len(native.messages) == 1 and native.messages[1]['text'] == 'continued answer'
                    await reply()
        finally:
            await channel.aclose()
        return {'joins_current_reply': True, 'no_duplicate_send': True}


async def upload_acknowledgement(bot, root):
    from concurrent.futures import Future
    from types import SimpleNamespace
    async with Harness(bot, root) as h:
        old_server = bot._web_chat_server
        entered = asyncio.Event()
        release = asyncio.Event()
        original_model = bot.ModelClient.think_and_reply
        async def waiting_model(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_model(*args, **kwargs)
        try:
            bot._web_chat_server = SimpleNamespace(config=SimpleNamespace(loop=asyncio.get_running_loop()), outbox=h.outbox)
            cid = current_scope().conversation_id
            with patch.object(bot.ModelClient, 'think_and_reply', waiting_model):
                receipt = bot._web_submit_upload('upload.txt', b'original ack content', '', h.outbox, conversation_id=cid)
                assert isinstance(receipt, Future)
                result = await asyncio.wait_for(asyncio.wrap_future(receipt), 5)
                assert result['ok']
                await asyncio.wait_for(entered.wait(), 5)
                records = await h.db.get_attachment_records()
                assert len(records) == 1
                ref = json.loads(records[0]['metadata'])['attachments'][0]
                assert ref['versions']['seed_kind'] == 'original'
                snapshot = AttachmentVersions(bot.ArtifactManager.UPLOAD_DIR, ref)
                assert snapshot.read_version(ref['sha256'])[0] == b'original ack content'
                assert not h.requests, 'receipt must not wait for a completed model response'
                release.set()
                for _ in range(200):
                    if not bot.get_conversations().busy():
                        break
                    await asyncio.sleep(.02)
                assert not bot.get_conversations().busy()
            with patch.object(AttachmentVersions, '_write', side_effect=OSError('fixture backup failure')):
                failed = bot._web_submit_upload('fail.txt', b'not accepted', '', h.outbox, conversation_id=cid)
                assert not (await asyncio.wait_for(asyncio.wrap_future(failed), 5))['ok']
            bot._web_chat_server = None
            unavailable = bot._web_submit_upload('fail.txt', b'not accepted', '', h.outbox, conversation_id=cid)
            assert not unavailable.result()['ok']
        finally:
            release.set()
            bot._web_chat_server = old_server
        return {'ack_after_backup': True, 'ack_before_model_finish': True, 'failed_upload_not_acknowledged': True}


async def restore_cancellation_and_delete_race(bot, root):
    from xgent_app.attachment_version_api import AttachmentVersionAPI
    async with Harness(bot, root) as h:
        w = Workbench(vars(bot))
        row, ref, path = await create(h)
        cid = current_scope().conversation_id
        data = {'id': f"{row}:{ref['id']}", 'conversation_id': cid}
        path.write_bytes(b'before cancelled request')
        state = await w.handle('GET', 'artifacts/versions', data)
        request = {**data, 'version_id': state['seed'], 'generation': state['generation'],
                   'fingerprint': state['current_file']['fingerprint'], 'confirm': True}
        entered, release = asyncio.Event(), asyncio.Event()
        original_restore = AttachmentVersionAPI._restore
        async def gated(api, values):
            entered.set()
            await release.wait()
            return await original_restore(api, values)
        with patch.object(AttachmentVersionAPI, '_restore', gated):
            task = asyncio.create_task(w.handle('POST', 'artifacts/restore_version', request))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(.03)
            assert not task.done(), 'cancellation must await the small filesystem/DB transaction'
            release.set()
            try: await task
            except asyncio.CancelledError: pass
            else: raise AssertionError('cancellation not propagated')
        assert path.read_bytes() == b'original UTF8\n'
        assert json.loads((await h.db.get_display_message(row))['metadata'])['attachments'][0]['sha256'] == ref['sha256']
        assert not bot.get_conversations().busy()
        original_read = AttachmentVersions.read_version
        def deleting_read(store, version):
            result = original_read(store, version)
            # The API re-check below must not release bytes from a deleted record.
            return result
        with patch.object(AttachmentVersions, 'read_version', deleting_read), patch.object(w, 'db', wraps=w.db):
            api = AttachmentVersionAPI(w)
            target = api.target
            calls = 0
            async def revoked(values):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise VersionError('deleted during download')
                return await target(values)
            with patch.object(api, 'target', revoked):
                try: await api.read('version_download', {**data, 'version_id': ref['sha256']})
                except VersionError: pass
                else: raise AssertionError('stale download released after revocation')
        return {'cancel_finishes_restore': True, 'slot_released': True, 'post_read_authorization': True}
