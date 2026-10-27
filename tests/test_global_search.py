"""Global search uses disposable databases; never reads an installed instance."""
import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from xgent_app.conversation_search import SearchError, _options, _read, visible_text


class GlobalSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'search.db'
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE chat_sessions(id TEXT PRIMARY KEY, name TEXT, archived INTEGER DEFAULT 0,
                deleting INTEGER DEFAULT 0, last_active REAL);
            CREATE TABLE global_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,
                msg_type TEXT,role TEXT,content TEXT,metadata TEXT,timestamp REAL);
            INSERT INTO chat_sessions VALUES('a','服务器部署',0,0,100);
            INSERT INTO chat_sessions VALUES('b','中文归档测试',1,0,99);
            INSERT INTO chat_sessions VALUES('c','已删除服务器',0,1,98);
        ''')

    def add(self, text, cid='a', kind='user_text', role='user', meta=None, timestamp=100):
        return self.conn.execute('INSERT INTO global_messages(session_id,msg_type,role,content,metadata,timestamp) VALUES(?,?,?,?,?,?)',
            (cid, kind, role, text, json.dumps(meta) if meta is not None else None, timestamp)).lastrowid

    def search(self, q, **kwargs):
        return _read(self.path, _options({'q': q, **kwargs}), threading.Event())

    def test_title_content_archives_exact_anchors_and_no_mutation(self):
        first = self.add('正文里的服务器关键词，名称不需要相同。')
        second = self.add('服务器历史 AI 回复', cid='b', kind='ai_reply', role='assistant')
        self.add('服务器不可见', cid='c')
        before = self.conn.total_changes
        result = self.search('服务器')['items']
        self.assertEqual([r['kind'] for r in result], ['title', 'message', 'message'])
        self.assertEqual({r['message_key'] for r in result if r['kind'] == 'message'}, {f'history:{first}', f'history:{second}'})
        self.assertTrue(next(r for r in result if r['conversation_id'] == 'b')['archived'])
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT archived FROM chat_sessions WHERE id="b"').fetchone()[0], 1)

    def test_unicode_casefold_literal_wildcards_and_sql_are_not_operators(self):
        self.add('中文 STRASSE Café 100%_ready [brackets] " OR 1=1 --')
        for query in ('中文', 'straße', 'CAFÉ', '100%_ready', '[brackets]', '" OR 1=1 --'):
            with self.subTest(query=query):
                item = self.search(query)['items'][0]
                self.assertIn(item['match'], item['snippet'])
        self.assertFalse(self.search('not%_a%_wildcard')['items'])
        self.assertEqual(self.search('straße')['items'][0]['match'], 'STRASSE')
        self.add('Unicode Straße')
        self.assertEqual(self.search('STRASSE')['items'][0]['match'], 'Straße')

    def test_filters_distinguish_tools_from_ai_and_archives(self):
        self.add('匹配用户')
        self.add('匹配 AI', kind='ai_reply', role='assistant')
        self.add('匹配工具', kind='agent_result', role='assistant')
        self.add('匹配归档', cid='b')
        for kind in ('assistant', 'tool'):
            result = self.search('匹配', kind=kind)['items']
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['role'], kind)
        self.assertEqual(len(self.search('匹配', archive='active')['items']), 3)
        self.assertEqual(len(self.search('匹配', archive='archived')['items']), 1)
        self.assertFalse(self.search('匹配', kind='title')['items'])

    def test_pagination_same_timestamps_and_new_messages_do_not_repeat(self):
        ids = [self.add('分页匹配', timestamp=50) for _ in range(73)]
        result = self.search('分页匹配', limit=7)
        self.add('分页匹配', timestamp=50)
        found = [r['message_key'] for r in result['items']]
        while result['next_cursor']:
            result = self.search('分页匹配', limit=7, cursor=result['next_cursor'])
            found.extend(r['message_key'] for r in result['items'])
        self.assertEqual(found, [f'history:{i}' for i in reversed(ids)])

    def test_cursor_belongs_to_query_and_filters_and_rejects_malformed(self):
        for _ in range(3): self.add('匹配')
        cursor = self.search('匹配', limit=1)['next_cursor']
        for data in ({'q': '其他'}, {'q': '匹配', 'archive': 'archived'}, {'q': '匹配', 'kind': 'tool'}):
            with self.assertRaises(SearchError): _options({**data, 'cursor': cursor})
        for bad in ('?', 'e30=', 'x' * 5000, base64.urlsafe_b64encode(json.dumps([1, '', 0, [1, float('nan'), 'a']]).encode()).decode()):
            with self.assertRaises(SearchError): _options({'q': '匹配', 'cursor': bad})
        for bad in ({'q': 'x' * 201}, {'q': 'x', 'archive': 'bad'}, {'q': 'x', 'limit': 'oops'}):
            with self.assertRaises(SearchError): _options(bad)

    def test_hidden_metadata_config_payload_and_html_attributes_are_not_searchable(self):
        self.add('配置密钥 SECRET', kind='system_op', role='system', meta={'ui_audit': True})
        self.add('SECRET', kind='user_file', meta={'attachment_purpose': 'configuration'})
        self.add('按钮 SECRET', kind='button_click')
        self.add('仅正文安全', kind='ai_reply', role='assistant', meta={'api_key': 'SECRET', 'display': {'content': '<b title="SECRET">安全回复</b><script>SECRET</script>', 'parse_mode': 'HTML'}})
        self.add('data:image/png;base64,SECRET', kind='user_photo')
        self.assertFalse(self.search('SECRET')['items'])
        item = self.search('安全回复')['items'][0]
        self.assertEqual(item['snippet'], '安全回复')

    def test_file_captions_and_names_and_long_middle_snippet(self):
        self.add('模型附件索引', kind='user_file', meta={'web_file_message': {'text': '这是上传说明'},
            'attachments': [{'filename': '中文报告.zip', 'path': '/uploads/report.zip', 'data': 'SECRET'}]})
        self.assertTrue(self.search('中文报告.zip')['items'])
        self.assertTrue(self.search('这是上传说明')['items'])
        self.assertFalse(self.search('SECRET')['items'])
        self.add('开头' * 5000 + '精确命中' + '结尾' * 5000)
        item = self.search('精确命中')['items'][0]
        self.assertIn('精确命中', item['snippet'])
        self.assertLess(len(item['snippet']), 250)
        self.assertTrue(item['snippet'].startswith('…') and item['snippet'].endswith('…'))

    def test_user_html_remains_literal_and_ai_markup_is_plain_text(self):
        self.assertEqual(visible_text('user_text', 'user', '<script>这是用户原文</script>', None), '<script>这是用户原文</script>')
        self.assertEqual(visible_text('ai_reply', 'assistant', '<b>可搜索</b> &amp; <code>命令</code>', None), '可搜索 & 命令')

    def test_ten_thousand_messages_no_schema_change_or_sqlite_fts_requirement(self):
        self.conn.execute('BEGIN')
        self.conn.executemany("INSERT INTO global_messages(session_id,msg_type,role,content,timestamp) VALUES('a','user_text','user',?,?)",
            ((f'历史消息 {i}：查找位置', i) for i in range(10000)))
        self.conn.commit()
        schema = list(self.conn.execute('SELECT name FROM sqlite_master'))
        start = time.monotonic()
        item = self.search('历史消息 1234：')['items'][0]
        self.assertEqual(item['message_key'], 'history:1235')
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(list(self.conn.execute('SELECT name FROM sqlite_master')), schema)


if __name__ == '__main__':
    unittest.main()
