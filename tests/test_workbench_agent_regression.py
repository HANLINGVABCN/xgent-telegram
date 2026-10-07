"""Opening the Web workbench must not poison subsequent Agent command parsing."""
import unittest
from tests.test_external_sync import SectionsProbeMixin


class WorkbenchAgentRegressionTests(SectionsProbeMixin, unittest.TestCase):
    def test_open_workbench_then_parse_execute_and_record_commands(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from pathlib import Path
from xgent_app.workbench import Workbench
from xgent_app.agent_dispatch import dispatch_standard_protocol
from xgent_app.agent_presenter import build_standard_operation_presentation
from xgent_app.agent_history import persist_standard_operation_result

async def main():
    await ns['UserDataManager'].init()
    db = await ns['BotMemoryDB'].get_instance()
    await db.create_session('global_memory')
    # Mirror installations predating the fix: the threshold was absent from _data.
    ns['UserDataManager']._data.pop('smart_match_threshold', None)
    before = dict(ns['UserDataManager']._data)
    initial_threshold = ns['UserDataManager'].get('smart_match_threshold', 90)
    service = Workbench(ns)
    for _ in range(3):
        await service.bootstrap({})
    nulls = [key for key in ns['WEB_EDITABLE_SETTINGS']
             if key in ns['UserDataManager']._data and ns['UserDataManager']._data[key] is None
             and (key not in before or before[key] is not None)]
    response = '\n'.join('```run-x\n<<BEGIN_TEST%04d\necho WORKBENCH_COMMAND_%d\n<<END_TEST%04d\n```' % (i, i, i)
                         for i in range(1, 4))
    blocks = ns['AgentExecutor'].extract_protocol_blocks(response)
    outputs = []
    for block in blocks:
        operation = await dispatch_standard_protocol(
            block, executor=ns['AgentExecutor'], provider_api_format='openai',
            stop_event_factory=asyncio.Event, logger=ns['logger'])
        presentation = build_standard_operation_presentation(operation)
        await persist_standard_operation_result(
            recorder=ns['GlobalRecorder'], message_type=ns['MessageType'].AGENT_RESULT,
            database=db, conversation_id='global_memory', chat_id=1,
            operation=operation, presentation=presentation)
        outputs.append({'success': operation['success'], 'output': operation['output'].strip(),
                        'archive': Path(operation['output_path']).is_file()})
    print(json.dumps({'nulls': nulls, 'initial': initial_threshold,
        'after': ns['UserDataManager'].get('smart_match_threshold', 90), 'results': outputs,
        'history': len(await db.get_display_history(50))}))
    await db.close()
asyncio.run(main())
''')
        self.assertEqual([], result['nulls'])
        self.assertEqual(90, result['initial'])
        self.assertEqual(90, result['after'])
        self.assertEqual(3, len(result['results']))
        for i, item in enumerate(result['results'], 1):
            self.assertTrue(item['success'])
            self.assertTrue(item['archive'])
            self.assertEqual(f'WORKBENCH_COMMAND_{i}', item['output'])
        self.assertGreaterEqual(result['history'], 3)

    def test_threshold_null_invalid_zero_and_saved_value_survive_bootstrap(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from unittest.mock import patch
from xgent_app.workbench import Workbench

async def main():
    await ns['UserDataManager'].init()
    db = await ns['BotMemoryDB'].get_instance()
    service = Workbench(ns)
    response = '```run-x\n<<BEGIN_TEST1234\necho ok\n<<END_TEST1234\n```'
    invalid = []
    for value in [None, '', 'broken', [], {}, float('nan'), float('inf'), True]:
        ns['UserDataManager'].set('smart_match_threshold', value)
        with patch.object(ns['ProtocolParser'], 'extract_protocol_blocks', wraps=ns['ProtocolParser'].extract_protocol_blocks) as parse:
            blocks = ns['AgentExecutor'].extract_protocol_blocks(response)
            invalid.append([len(blocks), parse.call_args.kwargs['smart_match_threshold']])
    saved = []
    for value in (0, 81):
        await db.set_config('smart_match_threshold', value)
        await ns['UserDataManager']._load_from_db()
        loaded = ns['UserDataManager'].get('smart_match_threshold')
        settings = await service.bootstrap({})
        with patch.object(ns['ProtocolParser'], 'extract_protocol_blocks', wraps=ns['ProtocolParser'].extract_protocol_blocks) as parse:
            ns['AgentExecutor'].extract_protocol_blocks(response)
            saved.append([loaded, settings['settings']['values']['smart_match_threshold'], parse.call_args.kwargs['smart_match_threshold']])
    await db.set_config('smart_match_threshold', None)
    await ns['UserDataManager']._load_from_db()
    settings = await service.bootstrap({})
    print(json.dumps({'invalid': invalid, 'saved': saved,
        'null_default': ns['UserDataManager'].get('smart_match_threshold'),
        'null_display': settings['settings']['values']['smart_match_threshold'],
        'ordinary_bash': ns['AgentExecutor'].extract_protocol_blocks('```bash\necho not-a-tool\n```')}))
    await db.close()
asyncio.run(main())
''')
        self.assertEqual([[1, .9]] * 8, result['invalid'])
        self.assertEqual([[0, 0, 0.0], [81, 81, .81]], result['saved'])
        self.assertEqual(90, result['null_default'])
        self.assertEqual(90, result['null_display'])
        self.assertEqual([], result['ordinary_bash'])

    def test_falsy_saved_settings_are_not_treated_as_missing(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from xgent_app.workbench import Workbench
async def main():
    await ns['UserDataManager'].init()
    db = await ns['BotMemoryDB'].get_instance()
    for key, value in {'smart_match_threshold': 0, 'stream_mode': False,
                       'hide_protocol_blocks': False, 'stats_auto_merge': False,
                       'disabled_skills': [], 'model_price_table': {}}.items():
        await db.set_config(key, value)
    result = await Workbench(ns).bootstrap({})
    print(json.dumps(result['settings']['values']))
    await db.close()
asyncio.run(main())
''')
        for key in ('stream_mode', 'hide_protocol_blocks', 'stats_auto_merge'):
            self.assertIs(False, result[key])
        self.assertEqual(0, result['smart_match_threshold'])
        self.assertEqual([], result['disabled_skills'])
        self.assertEqual({}, result['model_price_table'])
