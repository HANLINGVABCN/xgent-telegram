import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
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

    async def test_before_snapshot_failure_never_blocks_chat_or_fakes_complete_snapshot(self):
        after = {'root_digest': 'a' * 64, 'entries': [], 'file_count': 0, 'total_size': 0}
        scope = await self.manager.resolve()
        with patch('xgent_app.conversation_branching.capture_manifest',
                   side_effect=[BranchingError('fixture virtual filesystem'), after]):
            async with self.manager.operation(scope, execution=True):
                await self.db.record_global_message(1, 1, 'user_text', 'user', '仍要正常处理')
                await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '已完成')
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT * FROM conversation_turns ORDER BY started_at DESC LIMIT 1')
        turn = dict(await cursor.fetchone())
        await cursor.close()
        self.assertEqual('completed', turn['status'])
        self.assertIsNone(turn['before_snapshot_id'])
        self.assertIsNotNone(turn['after_snapshot_id'])
        self.assertFalse(turn['snapshot_complete'])
        self.assertIn('fixture virtual filesystem', turn['error'])

    async def test_after_snapshot_failure_preserves_completed_run_but_disables_exact_restore(self):
        before = {'root_digest': 'b' * 64, 'entries': [], 'file_count': 0, 'total_size': 0}
        scope = await self.manager.resolve()
        with patch('xgent_app.conversation_branching.capture_manifest',
                   side_effect=[before, BranchingError('fixture changed file')]):
            async with self.manager.operation(scope, execution=True):
                await self.db.record_global_message(1, 1, 'user_text', 'user', '正常回答')
                await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '完成')
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT * FROM conversation_turns ORDER BY started_at DESC LIMIT 1')
        turn = dict(await cursor.fetchone())
        await cursor.close()
        self.assertEqual('completed', turn['status'])
        self.assertIsNotNone(turn['before_snapshot_id'])
        self.assertIsNone(turn['after_snapshot_id'])
        self.assertFalse(turn['snapshot_complete'])
        self.assertIn('fixture changed file', turn['error'])

    async def test_empty_cancelled_admission_skips_the_second_snapshot(self):
        before = {'root_digest': 'c' * 64, 'entries': [], 'file_count': 0, 'total_size': 0}
        scope = await self.manager.resolve()
        with patch('xgent_app.conversation_branching.capture_manifest', return_value=before) as capture:
            async with self.manager.operation(scope, execution=True):
                pass
        self.assertEqual(1, capture.call_count)
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT * FROM conversation_turns ORDER BY started_at DESC LIMIT 1')
        turn = dict(await cursor.fetchone())
        await cursor.close()
        self.assertEqual('empty', turn['status'])
        self.assertIsNone(turn['after_snapshot_id'])
        self.assertFalse(turn['snapshot_complete'])

    async def test_exact_branch_isolated_apply_and_safe_withdraw(self):
        user, answer = await self.make_edit_turn()
        preview = await self.service.branch_preview('global_memory', answer, 'assistant')
        self.assertTrue(preview['exact_files'])
        branch = await self.service.create_branch(
            'global_memory', answer, 'assistant', request_id='branch-request-0001')
        workspace = Path((await self.db.get_session(branch['conversation_id']))['workspace_path'])
        self.assertEqual('after\n', (workspace / 'note.txt').read_text(encoding='utf-8'))
        (workspace / 'note.txt').write_text('branch\n', encoding='utf-8')
        self.assertEqual('after\n', (self.root / 'note.txt').read_text(encoding='utf-8'))
        apply_preview = await self.service.apply_preview(branch['conversation_id'])
        self.assertTrue(apply_preview['can_apply'])
        applied = await self.service.apply(branch['conversation_id'], {
            'confirm': True, 'base_snapshot_id': apply_preview['base_snapshot_id'],
            'source_digest': apply_preview['source_digest'], 'branch_digest': apply_preview['branch_digest'],
            'request_id': 'apply-request-0001',
        })
        self.assertEqual(1, applied['applied'])
        self.assertEqual('branch\n', (self.root / 'note.txt').read_text(encoding='utf-8'))
        self.assertEqual('branch\n', (workspace / 'note.txt').read_text(encoding='utf-8'))
        (workspace / 'note.txt').write_text('branch second\n', encoding='utf-8')
        again = await self.service.apply_preview(branch['conversation_id'])
        self.assertTrue(again['can_apply'])
        await self.service.apply(branch['conversation_id'], {
            'confirm': True, 'base_snapshot_id': again['base_snapshot_id'],
            'source_digest': again['source_digest'], 'branch_digest': again['branch_digest'],
            'request_id': 'apply-request-0002',
        })
        self.assertEqual('branch second\n', (self.root / 'note.txt').read_text(encoding='utf-8'))
        # Applying a branch is an external change after the source turn and is
        # detected by the digest guard instead of being silently overwritten.
        blocked = await self.service.withdraw_preview('global_memory', user)
        self.assertFalse(blocked['can_withdraw'])

    async def test_read_only_turn_withdraws_and_legacy_branch_warns(self):
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            user = await self.db.record_global_message(1, 1, 'user_text', 'user', '只读取')
            await record_operation(self.db, 'read', 'read_only')
            await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '读取完成')
        preview = await self.service.withdraw_preview('global_memory', user)
        self.assertTrue(preview['can_withdraw'])
        result = await self.service.withdraw('global_memory', user, {
            'confirm': True, 'turn_id': preview['turn_id'], 'current_digest': preview['current_digest'],
            'request_id': 'withdraw-request-0001',
        })
        self.assertEqual(1, result['withdrawn_turns'])
        conn = await self.db._get_conn()
        row = await (await conn.execute('SELECT COUNT(*) n FROM global_messages WHERE withdrawn=0')).fetchone()
        self.assertEqual(0, row['n'])

        async with self.db._transaction() as tx:
            cursor = await tx.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,1,'user_text','user','旧消息',1,'global_memory')")
            legacy_id = cursor.lastrowid
        legacy = await self.service.branch_preview('global_memory', legacy_id, 'user')
        self.assertFalse(legacy['exact_files'])
        self.assertIn('仅上下文', legacy['warning'])

    async def test_unknown_operation_blocks_withdrawal(self):
        scope = await self.manager.resolve()
        async with self.manager.operation(scope, execution=True):
            user = await self.db.record_global_message(1, 1, 'user_text', 'user', '运行命令')
            await record_operation(self.db, 'run', 'unknown')
            await self.db.record_global_message(1, 0, 'ai_reply', 'assistant', '完成')
        preview = await self.service.withdraw_preview('global_memory', user)
        self.assertFalse(preview['can_withdraw'])
        self.assertIn('外部或未知', preview['reason'])

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

    async def test_apply_conflict_never_partially_overwrites_source(self):
        _user, answer = await self.make_edit_turn()
        branch = await self.service.create_branch('global_memory', answer, 'assistant', request_id='conflict-branch-01')
        workspace = Path((await self.db.get_session(branch['conversation_id']))['workspace_path'])
        (workspace / 'note.txt').write_text('branch change\n', encoding='utf-8')
        (self.root / 'note.txt').write_text('source change\n', encoding='utf-8')
        preview = await self.service.apply_preview(branch['conversation_id'])
        self.assertFalse(preview['can_apply'])
        self.assertEqual(['note.txt'], [item['path'] for item in preview['conflicts']])
        with self.assertRaisesRegex(Exception, '冲突'):
            await self.service.apply(branch['conversation_id'], {
                'confirm': True, 'base_snapshot_id': preview['base_snapshot_id'],
                'source_digest': preview['source_digest'], 'branch_digest': preview['branch_digest'],
                'request_id': 'conflict-apply-01',
            })
        self.assertEqual('source change\n', (self.root / 'note.txt').read_text(encoding='utf-8'))

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


if __name__ == '__main__':
    unittest.main()
