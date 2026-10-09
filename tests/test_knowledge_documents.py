import asyncio
import tempfile
import unittest
from pathlib import Path
from xgent_app.knowledge import KnowledgeDocuments, DocumentError, split_skill_text, compose_skill_text, document_operation


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        roots={name:self.root/name for name in ('public','private','legacy','memory')}
        for value in roots.values():value.mkdir()
        self.roots=roots
        ns={'SKILL_PUBLIC_DIR':roots['public'],'SKILL_PRIVATE_DIR':roots['private'],'SKILL_LEGACY_DIR':roots['legacy'],
            'MEMORY_DIR':roots['memory'],'SKILL_FILE_EXTENSIONS':{'.md','.markdown','.txt'}}
        ns['list_skill_files']=lambda:[prefix+p.relative_to(roots[k]).as_posix() for k,prefix in [('public',''),('legacy',''),('private','private/')] for p in roots[k].rglob('*') if p.is_file() and not p.name.startswith('.')]
        ns['list_memory_files']=lambda:[p.name for p in roots['memory'].glob('*.txt')]
        self.store=KnowledgeDocuments(ns)
    def tearDown(self):self.temp.cleanup()

    def test_new_documents_are_immediately_empty_and_unique(self):
        a=self.store.create('skills');b=self.store.create('skills');m=self.store.create('memories')
        self.assertTrue(a['path'].startswith('private/'));self.assertEqual('',a['content']);self.assertEqual('',m['content'])
        self.assertNotEqual(a['path'],b['path']);self.assertEqual(2,len(self.store.list('skills')))
        self.assertTrue((self.roots['private']/a['filename']).is_file())

    def test_summary_blocks_and_full_source_are_lossless_on_read(self):
        tick=chr(96);source=tick*3+'!\r\n简介 **加粗**\r\n'+tick*3+'\r\n\r\n# 正文\r\n'+tick*3+'python\r\nprint(1)\r\n'+tick*3+'\r\n'
        a=self.store.create('skills');target=self.roots['private']/a['filename'];target.write_bytes(source.encode())
        item=self.store.read('skills',a['path']);self.assertEqual(source,item['content']);self.assertEqual('简介 **加粗**',item['summary'])
        self.assertIn('print(1)',item['body']);self.assertNotIn('简介',item['body'])
        saved=self.store.write('skills',a['path'],item['revision'],'全文可直接改\n')
        self.assertEqual('全文可直接改\n',target.read_text(encoding='utf8'));self.assertEqual('',saved['summary'])

    def test_summary_can_contain_fences_and_body_can_be_empty(self):
        summary='例子\n'+chr(96)*3+'python\nx=1\n'+chr(96)*3
        doc=compose_skill_text(summary,'');parsed=split_skill_text(doc)
        self.assertEqual(summary,parsed['summary']);self.assertEqual('',parsed['body'])
        self.assertEqual('',compose_skill_text('',''))

    def test_editor_revisions_prevent_lost_updates(self):
        a=self.store.create('skills');b=self.store.write('skills',a['path'],a['revision'],'new')
        for operation in [lambda:self.store.write('skills',a['path'],a['revision'],'old'),lambda:self.store.rename('skills',a['path'],a['revision'],'oops.md'),lambda:self.store.stage_delete('skills',a['path'],a['revision'])]:
            with self.assertRaises(DocumentError) as caught:operation()
            self.assertEqual(409,caught.exception.status)
        self.assertEqual('new',self.store.read('skills',b['path'])['content'])

    def test_rename_changes_filename_not_content_and_never_overwrites(self):
        a=self.store.create('skills');a=self.store.write('skills',a['path'],a['revision'],'保留全文')
        changed=self.store.rename('skills',a['path'],a['revision'],'改名')
        self.assertEqual('private/改名.md',changed['path']);self.assertEqual('保留全文',changed['content'])
        other=self.store.create('skills')
        with self.assertRaises(DocumentError):self.store.rename('skills',other['path'],other['revision'],'改名.md')
        self.assertEqual('保留全文',self.store.read('skills',changed['path'])['content'])

    def test_memory_edit_delete_and_rollback(self):
        item=self.store.create('memories');item=self.store.write('memories',item['path'],item['revision'],'共享的手工记忆')
        ticket=self.store.stage_delete('memories',item['path'],item['revision']);self.assertEqual([],self.store.list('memories'))
        self.store.finish_delete(ticket,rollback=True);self.assertEqual(item['content'],self.store.read('memories',item['path'])['content'])
        ticket=self.store.stage_delete('memories',item['path'],item['revision']);self.store.finish_delete(ticket)
        self.assertFalse(list(self.roots['memory'].iterdir()))

    def test_traversal_hidden_files_reserved_names_and_extensions_are_rejected(self):
        item=self.store.create('skills')
        for bad in ['../bad.md','private/../../bad.md','/absolute.md','C:/bad.md','private//新技能.md','./新技能.md',chr(92)+'bad.md']:
            with self.subTest(path=bad),self.assertRaises(DocumentError):self.store.read('skills',bad)
        for bad in ['../bad.md','a/b.md',chr(92)+'bad.md','CON.md','x.exe','.secret.md']:
            with self.subTest(name=bad),self.assertRaises(DocumentError):self.store.rename('skills',item['path'],item['revision'],bad)
        self.assertEqual(1,len(self.store.list('skills')))

    def test_symlink_targets_outside_roots_are_never_read_or_modified(self):
        outside=self.root/'outside.md';outside.write_text('do not touch',encoding='utf8')
        link=self.roots['private']/'link.md'
        try:link.symlink_to(outside)
        except OSError:self.skipTest('symlink unavailable')
        with self.assertRaises(DocumentError):self.store.read('skills','private/link.md')
        self.assertEqual('do not touch',outside.read_text())

    def test_same_logical_public_and_legacy_file_is_readable_but_not_destructively_ambiguous(self):
        for key in ('public','legacy'):(self.roots[key]/'same.md').write_text(key,encoding='utf8')
        doc=self.store.read('skills','same.md');self.assertEqual('public',doc['content'])
        with self.assertRaises(DocumentError):self.store.stage_delete('skills','same.md',doc['revision'])
        self.assertTrue((self.roots['legacy']/'same.md').exists())

    def test_concurrent_writers_are_serialized_and_one_stale_edit_is_rejected(self):
        item=self.store.create('memories')
        async def check():
            async def write(text):
                async with document_operation(self.root/'state.db'):
                    async with document_operation(self.root/'state.db'):
                        try:return await asyncio.to_thread(self.store.write,'memories',item['path'],item['revision'],text)
                        except DocumentError:return None
            return await asyncio.gather(write('one'),write('two'))
        result=asyncio.run(check());self.assertEqual(1,sum(r is not None for r in result))
