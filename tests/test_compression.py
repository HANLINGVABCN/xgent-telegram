import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from xgent_app.compression import (
    ARCHIVE_MARKER, ATTACHMENTS_NAME, COMPRESSION_MARKER, DEFAULT_COMPRESSION_PROMPT, INSTRUCTION_NAME, MEMORY_NAME,
    SUMMARY_NAME, CompressionError, CompressionReply, archive_files, attachment_index,
    save_conversation_export, verify_export, with_archive_reference,
)


def record(text, kind='user_text', metadata=None, row_id=1):
    return {'id': row_id, 'role': 'assistant' if kind == 'ai_reply' else 'user',
            'msg_type': kind, 'content': text, 'timestamp': row_id, 'metadata': metadata}


class CompressionPromptTests(unittest.TestCase):
    def test_shipped_prompt_matches_the_bootstrap_default(self):
        path = Path(__file__).resolve().parents[1] / 'prompts' / 'compression.txt'
        self.assertEqual(DEFAULT_COMPRESSION_PROMPT, path.read_text(encoding='utf-8').strip())


class ConversationExportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / 'storage with spaces'
        self.root.mkdir()

    def export(self, records=None, rounds=None):
        return save_conversation_export(self.root, {'records': records or [], 'compressions': rounds or []},
                                        system_prompt='CURRENT-SYSTEM', compression_prompt='FROZEN-INSTRUCTION\n',
                                        access_logs=[{'content': 'ACCESS-LOG-TAIL'}])

    def test_identical_zip_and_readable_texts_with_no_extra_files(self):
        long_text = 'full history\n' * 15000 + 'HISTORY-END  '
        bundle = self.export([record(long_text)])
        expected = ['\u62e6\u622a\u8bb0\u5f55.txt', '\u63d0\u793a\u8bcd.txt', INSTRUCTION_NAME,
                    f'1a{MEMORY_NAME}.txt', f'1b{ATTACHMENTS_NAME}.txt']
        with zipfile.ZipFile(bundle['archive_path']) as archive:
            self.assertEqual(expected, archive.namelist())
            for name in expected:
                self.assertEqual(archive.read(name), (Path(bundle['text_dir']) / name).read_bytes())
            self.assertIn(long_text, archive.read(expected[3]).decode())
            self.assertEqual(b'CURRENT-SYSTEM', archive.read(expected[1]))
            self.assertEqual(b'FROZEN-INSTRUCTION\n', archive.read(INSTRUCTION_NAME))
            self.assertIn(b'ACCESS-LOG-TAIL', archive.read(expected[0]))
        verify_export(bundle)
        self.assertEqual(set(expected) | {Path(bundle['archive_path']).name},
                         {path.name for path in Path(bundle['text_dir']).iterdir()})

    def test_sequence_order_and_failed_segments_have_no_fake_results(self):
        rounds = [{'sequence': number, 'version': 2, 'status': 'completed',
                   'summary': f'SUMMARY-{number}', 'source_records': [record(f'SOURCE-{number}')]}
                  for number in range(1, 12)]
        rounds[4].update(status='failed', summary='')
        files = archive_files(rounds, [record('CURRENT')], system_prompt='SYSTEM',
                              compression_prompt='INSTRUCTION', access_logs=[], storage_root=self.root)
        expected = [name for number in range(1, 12) for name in (
            f'{number}a{MEMORY_NAME}.txt', f'{number}b{ATTACHMENTS_NAME}.txt',
            *([f'{number}c{SUMMARY_NAME}.txt'] if number != 5 else []),
        )] + [f'12a{MEMORY_NAME}.txt', f'12b{ATTACHMENTS_NAME}.txt']
        self.assertEqual(expected, list(files)[3:])
        self.assertFalse(any(name.endswith('.json') for name in files))

    def test_paths_are_ordered_and_relative_absolute_duplicates_collapse(self):
        first = self.root / 'generated_media' / 'day' / 'first.png'
        second = self.root / 'uploads' / 'day' / 'second.txt'
        metadata = {'attachments': [
            {'path': 'day/second.txt', 'kind': 'text', 'order': 1, 'sha256': 'b' * 64},
            {'path': 'day/first.png', 'storage': 'generated_media', 'kind': 'image',
             'order': 0, 'sha256': 'a' * 64, 'width': 100, 'height': 200},
        ], 'display_media': [{'path': str(first)}, {'path': str(second)}]}
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('opened original')):
            index = attachment_index([record('notice', 'ai_reply', json.dumps(metadata))], self.root)
        self.assertEqual([str(first), str(second)], [item['path'] for item in index])
        self.assertEqual([1, 2], [item['order'] for item in index])
        self.assertEqual(['a' * 64, 'b' * 64], [item['sha256'] for item in index])
        self.assertEqual(100, index[0]['width'])

    def test_legacy_path_inventory_never_reads_images_or_quoted_examples(self):
        path = self.root / 'generated_media' / '2026-09-13' / '123456_abcde123_assistant_image.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'not inspected by the exporter')
        notice = ('\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a\u672c\u56fe\u7247\u5df2\u81ea\u52a8\u5b58\u5165 '
                  f'{path}\uff0c\u9700\u8981\u65f6\u8bf7read\u4ee5\u8fd4\u56de\u4e0a\u4e0b\u6587\u3011')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('opened original')):
            index = attachment_index([record('generated\n\n' + notice, 'ai_reply')], self.root)
            self.assertEqual(str(path), index[0]['path'])
            self.assertEqual('image/png', index[0]['mime_type'])
            self.assertEqual([], attachment_index([record('```\n' + notice + '\n```', 'ai_reply')], self.root))
            self.assertEqual([], attachment_index([
                record(notice, 'ai_reply', {'generated_media_processed': True}),
                record(str(path)),
            ], self.root))
        missing = attachment_index([record('old upload without an index', 'user_photo')], self.root)
        self.assertTrue(missing[0]['error'])

    def test_inline_media_is_not_embedded_and_originals_are_not_copied(self):
        raw = base64.b64encode(b'ORIGINAL-IMAGE-CONTENT').decode()
        path = self.root / 'image.png'
        path.write_bytes(b'ORIGINAL-IMAGE-CONTENT')
        bundle = self.export([record('before data:image/png;base64,' + raw + ' after', 'ai_reply', {
            'attachments': [{'path': str(path), 'mime_type': 'image/png', 'sha256': 'a' * 64}],
        })])
        with zipfile.ZipFile(bundle['archive_path']) as archive:
            contents = b'\n'.join(archive.read(name) for name in archive.namelist())
            self.assertNotIn(raw.encode(), contents)
            self.assertNotIn('image.png', archive.namelist())
            self.assertIn('after', archive.read(f'1a{MEMORY_NAME}.txt').decode())

    def test_archive_validation_rejects_text_zip_and_filename_tampering(self):
        for damage in ('text', 'zip', 'missing', 'hash', 'filename'):
            with self.subTest(damage=damage):
                bundle = self.export([record('original')])
                if damage == 'text':
                    Path(bundle['memory_path']).write_text('changed', encoding='utf-8')
                elif damage == 'zip':
                    with zipfile.ZipFile(bundle['archive_path'], 'a') as archive:
                        archive.writestr('extra.txt', 'changed')
                elif damage == 'missing':
                    Path(bundle['attachments_path']).unlink()
                elif damage == 'hash':
                    bundle['file_hashes'][INSTRUCTION_NAME] = '0' * 64
                else:
                    bundle['file_hashes']['../outside.txt'] = '0' * 64
                with self.assertRaises((CompressionError, OSError)):
                    verify_export(bundle)

    def test_reference_contains_paths_once_and_never_pins_a_summary(self):
        latest = {**self.export(), 'sequence': 1, 'summary': 'DO-NOT-PIN'}
        history = [dict(role='user', content='OLD-SEED', **{COMPRESSION_MARKER: True}),
                   dict(role='user', content='OLD-PATH', **{ARCHIVE_MARKER: True}),
                   {'role': 'user', 'content': 'QUESTION'}]
        result = with_archive_reference(with_archive_reference(history, latest), latest)
        self.assertEqual(2, len(result))
        self.assertEqual('QUESTION', result[-1]['content'])
        self.assertNotIn('DO-NOT-PIN', json.dumps(result))
        self.assertIn(latest['memory_path'], result[0]['content'])
        self.assertEqual([history[-1]], with_archive_reference(result, None))


class CompressionReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_reply_is_persisted_once_before_handoff(self):
        persist = AsyncMock()
        reply = CompressionReply(persist)
        self.assertEqual(('summary', []), await reply.prepare('summary'))
        await reply.prepare('summary')
        persist.assert_awaited_once_with('summary', [], False)
        self.assertTrue(reply.recorded and reply.completed)

    async def test_partial_and_stopped_replies_never_count_as_complete(self):
        for stopped in (False, True):
            reply = CompressionReply(AsyncMock())
            await reply.prepare('partial', partial=True, stopped=stopped)
            self.assertTrue(reply.recorded and reply.partial)
            self.assertFalse(reply.completed)
        empty = CompressionReply(AsyncMock())
        await empty.prepare('', stopped=True)
        self.assertFalse(empty.recorded or empty.completed)

    async def test_empty_media_or_failed_persistence_cannot_be_success(self):
        for text in ('', '   ', 'data:image/png;base64,AAAA'):
            reply = CompressionReply(AsyncMock())
            with self.assertRaises(CompressionError):
                await reply.prepare(text)
            self.assertFalse(reply.recorded or reply.completed)
            reply.persist.assert_not_awaited()
        reply = CompressionReply(AsyncMock(side_effect=OSError('disk')))
        with self.assertRaises(OSError):
            await reply.prepare('summary')
        self.assertFalse(reply.recorded or reply.completed)


if __name__ == '__main__':
    unittest.main()
