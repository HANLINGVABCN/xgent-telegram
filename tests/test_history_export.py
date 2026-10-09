"""Normal exports partition retained records; compression snapshots remain stable."""
import asyncio
import re
import unittest
import zipfile
from pathlib import Path

from tests import test_conversations as support
from xgent_app.compression import save_conversation_export, verify_export
from xgent_app.conversations import bind_conversation
from xgent_app.history_export import history_archive_files


class HistoryExportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ConversationTests.asyncSetUp
    asyncTearDown = support.ConversationTests.asyncTearDown
    write = support.ConversationTests.write

    async def compress(self, text):
        snapshot = await self.db.get_compression_snapshot()
        entry = await self.db.begin_compression(snapshot, {'archive_path':'a.zip','text_dir':'dir','instruction':'compress'},
                                               1, 'p', 'm', 'test', asyncio.Event())
        entry = await self.db.start_compression_attempt(entry['job_id'], 'p', 'm')
        return await self.db.commit_compression(entry, text, asyncio.Event())

    async def test_real_multiple_compressions_and_resets_export_exactly_once(self):
        with bind_conversation(await self.manager.resolve()):
            await self.write('stage zero original')
            await self.db.record_global_message(1,1,'system_op','system','management audit',metadata={'ui_audit':True})
            first = await self.compress('first summary')
            await self.write('stage zero second part')
            second = await self.compress('second summary')
            await self.write('stage zero tail')
            await self.db.clear_all_conversation_memory()
            await self.write('stage one original')
            await self.db.clear_all_conversation_memory()
            await self.write('current original')
            third = await self.compress('current summary')
            self.assertEqual([1,2,3], [first['sequence'],second['sequence'],third['sequence']])
            snapshot = await self.db.get_export_snapshot()
            bundle = save_conversation_export(self.temp.name, snapshot, system_prompt='system',
                                              compression_prompt='compress', access_logs=[])
            verify_export(bundle)
            with zipfile.ZipFile(bundle['archive_path']) as archive:
                names = archive.namelist()
                self.assertEqual(sorted(names), names)
                a_names = [name for name in names if re.search(r'\da全局记忆\.txt$', name)]
                text = '\n'.join(archive.read(name).decode('utf-8') for name in a_names)
                ids = re.findall(r'^--- record (\d+) ---$', text, flags=re.M)
                self.assertEqual(sorted(row['id'] for row in snapshot['records']), sorted(map(int,ids)))
                self.assertEqual(len(ids), len(set(ids)))
                self.assertIn('001-已清空-001c压缩结果.txt', names)
                self.assertIn('001-已清空-002c压缩结果.txt', names)
                self.assertIn('002-已清空-001a全局记忆.txt', names)
                self.assertNotIn('002-已清空-001c压缩结果.txt', names)
                self.assertIn('003-当前-001c压缩结果.txt', names)
                current = '\n'.join(archive.read(name).decode('utf-8') for name in a_names if '-当前-' in name)
                self.assertNotIn('stage zero', current)
                self.assertNotIn('stage one', current)
                self.assertIn('current original', current)
                self.assertIn('management audit', text)
                self.assertIn('清空时间', archive.read('上下文范围.txt').decode('utf-8'))
            self.assertEqual('003-当前-002a全局记忆.txt', Path(bundle['memory_path']).name)
            effective = await self.db.get_compression_snapshot()
            self.assertNotIn('stage zero', str(effective))
            self.assertNotIn('stage one', str(effective))
            frozen = save_conversation_export(self.temp.name, effective, system_prompt='system', compression_prompt='compress', access_logs=[])
            self.assertEqual('4a全局记忆.txt', Path(frozen['memory_path']).name)
            self.assertFalse(any('已清空' in name for name in frozen['file_hashes']))

    async def test_empty_stages_and_double_digit_sort(self):
        with bind_conversation(await self.manager.resolve()):
            for _ in range(11):
                await self.db.clear_all_conversation_memory()
            files, current = history_archive_files(await self.db.get_export_snapshot(), system_prompt='',
                                                  compression_prompt='', access_logs=[], storage_root=self.temp.name)
            names = list(files)
            self.assertEqual(sorted(names), names)
            self.assertEqual('012-当前-001a全局记忆.txt', current[0])
            self.assertLess(names.index('002-已清空-001a全局记忆.txt'), names.index('010-已清空-001a全局记忆.txt'))
            self.assertFalse(any('c压缩结果' in name for name in names))

    async def test_legacy_snapshot_is_retained_without_fabricated_boundary(self):
        snapshot = {'context_epoch':0,'records':[{'id':10,'timestamp':10,'role':'user','msg_type':'user_text','content':'current'}],
                    'compressions':[{'sequence':1,'status':'completed','summary':'saved summary',
                    'source_records':[{'id':1,'timestamp':1,'role':'user','msg_type':'user_text','content':'actual old snapshot'}]}]}
        files, current = history_archive_files(snapshot,system_prompt='',compression_prompt='',access_logs=[],storage_root=self.temp.name)
        self.assertIn(b'actual old snapshot',files['001-当前-001a全局记忆.txt'])
        self.assertIn(b'current',files[current[0]])
        self.assertIn('旧版压缩快照',files['上下文范围.txt'].decode('utf-8'))

    async def test_number_width_expands_consistently(self):
        snapshot={'context_epoch':1000,'records':[],'compressions':[{'context_epoch':0,'sequence':1,'status':'failed','source_records':[]}]}
        files,current=history_archive_files(snapshot,system_prompt='',compression_prompt='',access_logs=[],storage_root=self.temp.name)
        self.assertIn('0001-已清空-0001a全局记忆.txt',files)
        self.assertEqual('1001-当前-0001a全局记忆.txt',current[0])
        self.assertFalse(any('c压缩结果' in name for name in files))
