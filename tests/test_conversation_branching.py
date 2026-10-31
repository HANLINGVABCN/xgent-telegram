import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_database_write_lock import _load_db_class
from xgent_app.conversation_branching import (
    BranchingError,
    ConversationBranchService,
    capture_manifest,
    record_operation,
)
from xgent_app.conversations import ConversationManager


class ConversationBranchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        (self.root / '.gitignore').write_text('xgent_storage/\nxgent_memory.db*\n', encoding='utf-8')
        (self.root / 'note.txt').write_text('before\n', encoding='utf-8')
        for command in (
            ['git', '-C', str(self.root), 'init', '-q'],
            ['git', '-C', str(self.root), 'config', 'user.email', 'test@example.com'],
            ['git', '-C', str(self.root), 'config', 'user.name', 'test'],
            ['git', '-C', str(self.root), 'add', '.'],
            ['git', '-C', str(self.root), 'commit', '-qm', 'initial'],
        ):
            await asyncio.to_thread(subprocess.run, command, check=True)
        cls = _load_db_class()
        core = (Path(__file__).resolve().parents[1] / 'xgent_app/sections/core.py').read_text(encoding='utf-8')
        constants = core[core.index('class MessageType:'):core.index('def _read_int_env(')]
        exec(constants, cls._init_db.__globals__)  # noqa: S102 - isolated test fixture mirrors existing tests
        from xgent_app.web_history import display_media_reference
        cls._init_db.__globals__['display_media_reference'] = display_media_reference
        self.db = cls(str(self.root / 'xgent_memory.db'))
        await self.db._init_db()
        async def factory():
            return self.db
        self.manager = ConversationManager(factory, self.db.db_path, code_root=self.root)
        self.service = ConversationBranchService(
            self.db, code_root=self.root, storage_root=self.root / 'xgent_storage')

    async def asyncTearDown(self):
        await self.manager.close()
        self.manager.lock.release()
        await self.db.close()
        await asyncio.to_thread(
            subprocess.run, ['git', '-C', str(self.root), 'worktree', 'prune'],
            check=False, capture_output=True,
        )
        self.temp.cleanup()

    async def make_edit_turn(self):
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            user = await self.db.record_global_message(1, 1, 'user_text', 'user', '修改文件')
            (self.root / 'note.txt').write_text('after\n', encoding='utf-8')
            await record_operation(self.db, 'edit', 'local_reversible', {'path': 'note.txt'})
            answer = await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '完成')
        return user, answer

    def test_snapshot_excludes_nested_mounts_and_symlinks(self):
        mounted = self.root / 'nodequality-runtime' / 'proc'
        mounted.mkdir(parents=True)
        (mounted / 'virtual-status').write_text('must not be read', encoding='utf-8')
        outside = Path(self.temp.name) / 'outside-secret.txt'
        outside.write_text('outside', encoding='utf-8')
        link = self.root / 'outside-link.txt'
        try:
            link.symlink_to(outside)
        except OSError:
            link = None
        objects = Path(self.temp.name) / 'objects'
        with patch('xgent_app.conversation_branching._nested_mounts',
                   return_value={'nodequality-runtime/proc'}):
            manifest = capture_manifest(self.root, objects)
        paths = {item['path'] for item in manifest['entries']}
        self.assertNotIn('nodequality-runtime/proc/virtual-status', paths)
        if link is not None:
            self.assertNotIn('outside-link.txt', paths)

    def test_installed_tree_excludes_nodequality_runtime_without_git_metadata(self):
        installed = Path(self.temp.name) / 'installed'
        installed.mkdir()
        (installed / 'app.py').write_text('print("kept")\n', encoding='utf-8')
        runtime = installed / '.nodequality2026_10_28_01_31_43' / 'BenchOs'
        runtime.mkdir(parents=True)
        (runtime / 'guest-image.bin').write_bytes(b'x' * 4096)
        manifest = capture_manifest(installed, Path(self.temp.name) / 'installed-objects')
        paths = {item['path'] for item in manifest['entries']}
        self.assertIn('app.py', paths)
        self.assertFalse(any(path.startswith('.nodequality') for path in paths))

    def test_turn_snapshot_timeout_is_bounded_without_disabling_manual_snapshots(self):
        installed = Path(self.temp.name) / 'timeout-project'
        installed.mkdir()
        (installed / 'app.py').write_text('content\n', encoding='utf-8')
        original = capture_manifest(installed, Path(self.temp.name) / 'manual-objects')
        self.assertEqual(['app.py'], [item['path'] for item in original['entries']])

        def slow_paths(*_args, **_kwargs):
            time.sleep(.03)
            return ['app.py']

        with patch('xgent_app.conversation_branching.managed_paths', side_effect=slow_paths):
            with self.assertRaisesRegex(BranchingError, '安全上限'):
                capture_manifest(
                    installed, Path(self.temp.name) / 'timed-objects', timeout=.001)

    async def test_chat_turn_never_scans_files(self):
        scope = await self.manager.resolve()
        with patch('xgent_app.conversation_branching.capture_manifest',
                   side_effect=AssertionError('turns must not scan files')) as capture:
            async with self.manager.operation(scope, execution=True):
                await self.db.record_global_message(1, 1, 'user_text', 'user', '正常聊天')
                await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '已完成')
        self.assertEqual(0, capture.call_count)
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT * FROM conversation_turns ORDER BY started_at DESC LIMIT 1')
        turn = dict(await cursor.fetchone())
        await cursor.close()
        self.assertEqual('completed', turn['status'])
        self.assertIsNone(turn['before_snapshot_id'])
        self.assertIsNone(turn['after_snapshot_id'])
        self.assertIsNotNone(turn['user_record_id'])
        self.assertIsNotNone(turn['final_record_id'])
        cursor = await conn.execute('SELECT COUNT(*) n FROM conversation_snapshots')
        self.assertEqual(0, (await cursor.fetchone())['n'])
        await cursor.close()

    async def test_empty_cancelled_admission_records_empty_turn(self):
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            pass
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT * FROM conversation_turns ORDER BY started_at DESC LIMIT 1')
        turn = dict(await cursor.fetchone())
        await cursor.close()
        self.assertEqual('empty', turn['status'])

    async def _withdraw(self, record_id, request_id):
        preview = await self.service.withdraw_preview('global_memory', record_id)
        self.assertTrue(preview['can_withdraw'], preview)
        result = await self.service.withdraw('global_memory', record_id, {
            'confirm': True, 'turn_id': preview['turn_id'], 'current_digest': preview['current_digest'],
            'request_id': request_id,
        })
        return preview, result

    async def test_edit_turn_withdraws_with_warning_and_keeps_disk_files(self):
        user, _answer = await self.make_edit_turn()
        preview, result = await self._withdraw(user, 'withdraw-edit-0001')
        self.assertTrue(any('文件写入' in item for item in preview['warnings']))
        self.assertEqual(0, result['restored_files'])
        # Withdrawal only removes history; the file written by the turn stays.
        self.assertEqual('after\n', (self.root / 'note.txt').read_text(encoding='utf-8'))
        conn = await self.db._get_conn()
        row = await (await conn.execute('SELECT COUNT(*) n FROM global_messages WHERE withdrawn=0')).fetchone()
        self.assertEqual(0, row['n'])

    async def test_command_and_external_side_effects_warn_but_never_block(self):
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            user = await self.db.record_global_message(1, 1, 'user_text', 'user', '运行命令')
            await record_operation(self.db, 'run', 'unknown')
            await record_operation(self.db, 'sendfile', 'external')
            await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '完成')
        # Files changed after the turn no longer matter either.
        (self.root / 'note.txt').write_text('changed later\n', encoding='utf-8')
        preview, result = await self._withdraw(user, 'withdraw-run-00001')
        self.assertEqual('', preview['reason'])
        self.assertTrue(any('命令执行' in item and 'run' in item for item in preview['warnings']))
        self.assertTrue(any('外部发送' in item for item in preview['warnings']))
        self.assertEqual(1, result['withdrawn_turns'])
        self.assertEqual('changed later\n', (self.root / 'note.txt').read_text(encoding='utf-8'))

    async def test_read_only_turn_has_no_warning_and_legacy_history_withdraws(self):
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            user = await self.db.record_global_message(1, 1, 'user_text', 'user', '只读取')
            await record_operation(self.db, 'read', 'read_only')
            await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '读取完成')
        preview = await self.service.withdraw_preview('global_memory', user)
        self.assertEqual([], preview['warnings'])

        async with self.db._transaction() as tx:
            cursor = await tx.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,1,'user_text','user','旧消息',1,'global_memory')")
            legacy_id = cursor.lastrowid
        legacy = await self.service.branch_preview('global_memory', legacy_id, 'user')
        self.assertEqual('', legacy['warning'])
        _preview, result = await self._withdraw(legacy_id, 'withdraw-legacy-01')
        self.assertGreaterEqual(result['withdrawn_messages'], 1)

    async def test_pending_queue_still_prevents_withdrawal(self):
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            user = await self.db.record_global_message(1, 1, 'user_text', 'user', '问题')
            await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '回答')
        async with self.db._transaction() as tx:
            await tx.execute("INSERT INTO message_queue_controls(conversation_id,generation,state,reason) "
                             "VALUES('global_memory',?,'ask','ask')", (scope.generation,))
        preview = await self.service.withdraw_preview('global_memory', user)
        self.assertFalse(preview['can_withdraw'])
        self.assertIn('待发消息', preview['reason'])

    async def test_withdraw_collects_reply_parts_media_and_footer_ids(self):
        from xgent_app.telegram_records import INSTANCE
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            user = await self.db.record_global_message(1, 1, 'user_text', 'user', '问题',
                metadata={'source_message_id': 41})
            await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '回复',
                metadata={'telegram_logical_instance': INSTANCE, 'telegram_native_parts': {'ids': [42, 43]},
                          'telegram_reply': {'message_ids': [100001, 100002]},
                          'telegram_delivery': {'ids': [44]}, 'telegram_logical_ids': [100003]})
        _, result = await self._withdraw(user, 'withdraw-all-parts')
        self.assertEqual([41, 42, 43, 44], result['telegram_native_message_ids'])
        self.assertEqual([100001, 100002, 100003], result['telegram_logical_message_ids'])

    async def test_stale_preview_is_rejected(self):
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            user = await self.db.record_global_message(1, 1, 'user_text', 'user', '问题')
            await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '回答')
        preview = await self.service.withdraw_preview('global_memory', user)
        async with self.manager.operation(scope, execution=True):
            await self.db.record_global_message(1, 1, 'user_text', 'user', '新消息')
        with self.assertRaisesRegex(BranchingError, '重新预览'):
            await self.service.withdraw('global_memory', user, {
                'confirm': True, 'turn_id': preview['turn_id'],
                'current_digest': preview['current_digest'], 'request_id': 'withdraw-stale-01',
            })

    async def test_branch_copies_history_without_workspace(self):
        _user, answer = await self.make_edit_turn()
        preview = await self.service.branch_preview('global_memory', answer, 'assistant')
        self.assertFalse(preview['exact_files'])
        with patch('xgent_app.conversation_branching.create_git_worktree',
                   side_effect=AssertionError('branches must not copy workspaces')):
            branch = await self.service.create_branch(
                'global_memory', answer, 'assistant', request_id='branch-request-0001')
        self.assertEqual('context', branch['branch_mode'])
        session = await self.db.get_session(branch['conversation_id'])
        self.assertIsNone(session['workspace_path'])
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT content FROM global_messages WHERE session_id=? ORDER BY id',
                                    (branch['conversation_id'],))
        self.assertEqual(['修改文件', '完成'], [row['content'] for row in await cursor.fetchall()])
        await cursor.close()

    async def test_branches_share_immutable_original_attachment_object(self):
        uploads = self.root / 'xgent_storage' / 'uploads'; uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / 'sample.txt'; original = b'original attachment\n'; path.write_bytes(original)
        digest = hashlib.sha256(original).hexdigest()
        identity = hashlib.sha256(path.relative_to(uploads).as_posix().encode()).hexdigest()
        ref = {'version': 1, 'id': identity, 'path': 'sample.txt', 'name': 'sample.txt',
               'caption': '', 'context_prefix': '', 'order': 0, 'size': len(original),
               'sha256': digest, 'kind': 'text', 'mime_type': 'text/plain', 'encoding': 'utf-8'}
        from xgent_app.attachment_versions import AttachmentVersions
        ref['versions'] = AttachmentVersions(uploads, ref).original(original)
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            await self.db.record_global_message(1, 1, 'user_file', 'user', '附件消息',
                                                metadata={'attachments': [ref]})
            path.write_text('later mutable content\n', encoding='utf-8')
            output = self.root / 'xgent_storage' / 'command_outputs' / 'run.txt'
            output.parent.mkdir(parents=True, exist_ok=True); output.write_text('immutable command output\n', encoding='utf-8')
            await self.db.record_global_message(1, 0, 'agent_result', 'system',
                f'<blockquote expandable data-output-path="{output}"><pre>done</pre></blockquote>')
            answer = await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '已读取原件')
        first = await self.service.create_branch('global_memory', answer, 'assistant', request_id='attachment-branch-01')
        second = await self.service.create_branch('global_memory', answer, 'assistant', request_id='attachment-branch-02')
        conn = await self.db._get_conn(); working_paths = []; output_paths = []
        for branch in (first, second):
            cursor = await conn.execute("SELECT metadata FROM global_messages WHERE session_id=? AND msg_type='user_file'", (branch['conversation_id'],))
            metadata = json.loads((await cursor.fetchone())['metadata']); await cursor.close()
            copied = metadata['attachments'][0]
            self.assertTrue(copied['branch_working_copy'])
            self.assertFalse(copied['immutable_object'])
            working = uploads / copied['path']; self.assertEqual(original, working.read_bytes())
            working_paths.append(copied['path'])
            cursor = await conn.execute("SELECT content FROM global_messages WHERE session_id=? AND msg_type='agent_result'", (branch['conversation_id'],))
            content = (await cursor.fetchone())['content']; await cursor.close()
            archived = Path(re.search(r'data-output-path="([^"]+)"', content)[1])
            self.assertEqual('immutable command output\n', archived.read_text(encoding='utf-8'))
            output_paths.append(archived)
        self.assertNotEqual(working_paths[0], working_paths[1])
        self.assertEqual(output_paths[0], output_paths[1])
        cursor = await conn.execute("SELECT COUNT(*) n FROM artifact_objects WHERE kind='attachment' AND sha256=?", (digest,))
        self.assertEqual(1, (await cursor.fetchone())['n']); await cursor.close()

    async def test_v5_history_is_grouped_into_legacy_turns(self):
        async with self.db._transaction() as conn:
            await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,1,'user_text','user','旧问题',1,'global_memory')")
            await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,0,'ai_reply','assistant','中间回答',2,'global_memory')")
            await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,0,'agent_result','system','工具结果',3,'global_memory')")
            cursor = await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,0,'ai_reply','assistant','最终回答',4,'global_memory')")
            final_id = cursor.lastrowid
            cursor = await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,1,'command','user','/update',5,'global_memory')")
            cmd_id = cursor.lastrowid
            await conn.execute("UPDATE config SET value='5' WHERE key='conversation_schema_version'")
        await self.db.migrate_conversations()
        conn = await self.db._get_conn()
        cursor = await conn.execute("SELECT DISTINCT turn_id FROM global_messages WHERE content IN ('旧问题','中间回答','工具结果','最终回答')")
        turns = [row['turn_id'] for row in await cursor.fetchall()]; await cursor.close()
        self.assertEqual(1, len(set(turns)))
        cursor = await conn.execute('SELECT turn_id FROM global_messages WHERE id=?', (cmd_id,))
        self.assertIsNone((await cursor.fetchone())['turn_id']); await cursor.close()
        cursor = await conn.execute('SELECT * FROM conversation_turns WHERE id=?', (turns[0],))
        turn = dict(await cursor.fetchone()); await cursor.close()
        self.assertEqual('legacy', turn['status'])
        self.assertEqual(final_id, turn['final_record_id'])
        self.assertFalse(turn['snapshot_complete'])

    async def test_branch_excludes_trailing_command_and_allows_command_withdrawal(self):
        user, answer = await self.make_edit_turn()
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT turn_id FROM global_messages WHERE id=?', (answer,))
        turn_id = (await cursor.fetchone())['turn_id']; await cursor.close()
        # Simulate token_usage recorded right after ai_reply, followed by /update wrongly attached to the turn.
        async with self.db._transaction() as tx:
            cursor = await tx.execute(
                "INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id,turn_id) "
                "VALUES(1,0,'token_usage','assistant','↑ 100 · ↓ 50 · 20 tokens/s',998,'global_memory',?)",
                (turn_id,),
            )
            tok_id = cursor.lastrowid
            cursor = await tx.execute(
                "INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id,turn_id) "
                "VALUES(1,1,'command','user','/update',999,'global_memory',?)",
                (turn_id,),
            )
            cmd_id = cursor.lastrowid
        # Branching from `answer` must KEEP `token_usage` and EXCLUDE `/update`.
        branch = await self.service.create_branch('global_memory', answer, 'assistant', request_id='no-cmd-branch-01')
        cursor = await conn.execute('SELECT content FROM global_messages WHERE session_id=? AND withdrawn=0 ORDER BY id',
                                    (branch['conversation_id'],))
        branch_contents = [row['content'] for row in await cursor.fetchall()]; await cursor.close()
        self.assertEqual(['修改文件', '完成', '↑ 100 · ↓ 50 · 20 tokens/s'], branch_contents)
        # Re-running migrate_conversations detaches `/update` from `turn_id` while keeping `token_usage` on `turn_id`.
        await self.db.migrate_conversations()
        cursor = await conn.execute('SELECT turn_id FROM global_messages WHERE id=?', (tok_id,))
        self.assertEqual(turn_id, (await cursor.fetchone())['turn_id']); await cursor.close()
        cursor = await conn.execute('SELECT turn_id FROM global_messages WHERE id=?', (cmd_id,))
        self.assertIsNone((await cursor.fetchone())['turn_id']); await cursor.close()
        # Withdrawing `/update` directly succeeds without requiring a turn snapshot.
        preview = await self.service.withdraw_preview('global_memory', cmd_id)
        self.assertTrue(preview['can_withdraw'])
        self.assertEqual(1, preview['message_count'])
        result = await self.service.withdraw('global_memory', cmd_id, {
            'confirm': True, 'turn_id': preview['turn_id'], 'current_digest': preview['current_digest'],
            'request_id': 'withdraw-cmd-01',
        })
        self.assertEqual(1, result['withdrawn_messages'])
        cursor = await conn.execute('SELECT content FROM global_messages WHERE session_id=? AND withdrawn=0 ORDER BY id',
                                    ('global_memory',))
        remaining = [row['content'] for row in await cursor.fetchall()]; await cursor.close()
        self.assertEqual(['修改文件', '完成', '↑ 100 · ↓ 50 · 20 tokens/s'], remaining)

    async def test_multiple_branches_from_same_conversation_get_unique_names(self):
        _user, answer = await self.make_edit_turn()
        long_title = '看下你对应的项目的最后一次提交内容。给我说一下更新信息' + ('长' * 60)
        async with self.db._transaction() as tx:
            await tx.execute('UPDATE chat_sessions SET name=? WHERE id=?', (long_title[:80], 'global_memory'))
        first = await self.service.create_branch('global_memory', answer, 'assistant', request_id='multi-branch-01')
        second = await self.service.create_branch('global_memory', answer, 'assistant', request_id='multi-branch-02')
        third = await self.service.create_branch('global_memory', answer, 'assistant', request_id='multi-branch-03')
        self.assertTrue(first['name'].endswith(' · 分支'))
        self.assertTrue(second['name'].endswith(' · 分支 2'))
        self.assertTrue(third['name'].endswith(' · 分支 3'))
        self.assertEqual(3, len({first['name'], second['name'], third['name']}))
        for item in (first, second, third):
            self.assertLessEqual(len(item['name']), 80)

    def test_incremental_snapshot_reuses_unchanged_file_digests_without_rereading(self):
        project = Path(self.temp.name) / 'incremental-project'
        project.mkdir()
        (project / 'stable.py').write_text('print("stable")\n', encoding='utf-8')
        (project / 'mutable.py').write_text('v1\n', encoding='utf-8')
        objects = Path(self.temp.name) / 'incremental-objects'
        first = capture_manifest(project, objects)
        self.assertEqual(2, first['file_count'])

        with patch('xgent_app.conversation_branching._digest_file') as mocked_digest:
            second = capture_manifest(project, objects)
            self.assertEqual(0, mocked_digest.call_count)
        self.assertEqual(first['root_digest'], second['root_digest'])

        # Modifying only mutable.py hashes only the changed file and updates root_digest.
        (project / 'mutable.py').write_text('v2-modified\n', encoding='utf-8')
        from xgent_app.conversation_branching import _digest_file as real_digest
        digested_paths = []

        def spy_digest(path, *args, **kwargs):
            digested_paths.append(Path(path).name)
            return real_digest(path, *args, **kwargs)

        with patch('xgent_app.conversation_branching._digest_file', side_effect=spy_digest):
            third = capture_manifest(project, objects)
        self.assertNotEqual(first['root_digest'], third['root_digest'])
        self.assertIn('mutable.py', digested_paths)
        self.assertNotIn('stable.py', digested_paths)

    def test_snapshot_aborts_immediately_when_cancelled(self):
        project = Path(self.temp.name) / 'cancel-project'
        project.mkdir()
        (project / 'a.txt').write_text('hello\n', encoding='utf-8')
        objects = Path(self.temp.name) / 'cancel-objects'
        with self.assertRaisesRegex(BranchingError, '取消或停止'):
            capture_manifest(project, objects, should_cancel=lambda: True)


if __name__ == '__main__':
    unittest.main()
