"""路径、崩溃日志、监督任务和 CI 检查的针对性回归。"""
import ast
import asyncio
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_definitions(relative_path, names, namespace):
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if getattr(n, 'name', '') in names or
             (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class StopWaitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stop = asyncio.Event()
        self.ns = load_definitions('xgent_app/sections/runtime.py', {'_sleep_or_stop'},
            {'asyncio': asyncio, 'get_app_stop_event': lambda: self.stop})

    async def test_repeated_timeouts_do_not_accumulate_tasks(self):
        before = asyncio.all_tasks()
        for _ in range(20):
            self.assertFalse(await self.ns['_sleep_or_stop'](0.001))
        self.assertEqual(before, asyncio.all_tasks())
        self.assertFalse(self.stop.is_set())

    async def test_stop_wakes_waiter_and_zero_delay_does_not_allocate_task(self):
        task = asyncio.create_task(self.ns['_sleep_or_stop'](30))
        await asyncio.sleep(0)
        self.stop.set()
        self.assertTrue(await asyncio.wait_for(task, 1))
        before = asyncio.all_tasks()
        self.assertTrue(await self.ns['_sleep_or_stop'](0))
        self.assertEqual(before, asyncio.all_tasks())

    async def test_cancelled_wait_does_not_leak_or_change_event(self):
        before = asyncio.all_tasks()
        task = asyncio.create_task(self.ns['_sleep_or_stop'](30))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(before, asyncio.all_tasks())
        self.assertFalse(self.stop.is_set())


class CrashLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xgent-crash-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.stderr = io.StringIO()
        self.ns = load_definitions('xgent_app/sections/main.py',
            {'_CRASH_LOG', '_write_crash', '_crash_asyncio_handler'},
            {'PROJECT_ROOT': str(self.root), '__file__': str(self.root / 'xgent_server.py'),
             '_os': os, '_sys': SimpleNamespace(stderr=self.stderr), '_time': time, '_traceback': traceback})

    def test_crash_log_is_inside_project(self):
        self.ns['_write_crash']('test crash')
        self.assertEqual(self.root / 'xgent_crash.log', Path(self.ns['_CRASH_LOG']))
        self.assertIn('test crash', (self.root / 'xgent_crash.log').read_text(encoding='utf-8'))
        self.assertFalse((self.root.parent / 'xgent_crash.log').exists())

    def test_unwritable_log_reports_failure_to_stderr(self):
        with patch('builtins.open', side_effect=PermissionError('read only')):
            self.ns['_write_crash']('test crash')
        self.assertIn('无法写入崩溃日志', self.stderr.getvalue())
        self.assertIn('read only', self.stderr.getvalue())

    def test_async_crash_keeps_traceback(self):
        try:
            raise ValueError('async failure')
        except ValueError as exc:
            self.ns['_crash_asyncio_handler'](None, {'message': 'background task', 'exception': exc})
        text = (self.root / 'xgent_crash.log').read_text(encoding='utf-8')
        self.assertIn('Traceback', text)
        self.assertIn('test_async_crash_keeps_traceback', text)
        self.assertIn('async failure', text)


class CICheckTests(unittest.TestCase):
    def test_compile_targets_and_shell_scripts_exist(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8'))
        commands = [s.get('run', '') for s in workflow['jobs']['test']['steps']]
        compile_cmd = next(c for c in commands if 'compileall' in c and 'skill-public/script' in c)
        self.assertIn('test -d skill-public/script', compile_cmd)
        self.assertTrue((ROOT / 'skill-public/script').is_dir())
        shell_cmd = next(c for c in commands if 'bash -n' in c)
        self.assertIn('bash -n "$script"', shell_cmd)
        self.assertIn('test -f "$script"', shell_cmd)
        targets = shell_cmd.split('for script in ', 1)[1].split('; do', 1)[0].split()
        self.assertGreater(len(targets), 1)
        for target in targets:
            self.assertTrue((ROOT / target).is_file(), target)

    def test_bash_loop_rejects_second_broken_or_missing_script(self):
        import shutil
        bash = shutil.which('bash')
        git_bash = Path('C:/Program Files/Git/bin/bash.exe')
        if os.name == 'nt' and git_bash.is_file():
            bash = str(git_bash)
        if not bash:
            self.skipTest('bash unavailable')
        workflow = yaml.safe_load((ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8'))
        command = next(s['run'] for s in workflow['jobs']['test']['steps'] if 'bash -n' in s.get('run', ''))
        targets = command.split('for script in ', 1)[1].split('; do', 1)[0]
        with tempfile.TemporaryDirectory(prefix='xgent-ci-test-') as temp:
            Path(temp, 'valid.sh').write_text('echo ok\n', encoding='utf-8')
            Path(temp, 'broken.sh').write_text('if then\n', encoding='utf-8')
            for other in ('broken.sh', 'missing.sh'):
                script = command.replace(targets, 'valid.sh ' + other)
                result = subprocess.run([bash, '-c', script], cwd=temp, capture_output=True, timeout=10)
                self.assertNotEqual(0, result.returncode, result.stdout)


if __name__ == '__main__':
    unittest.main()
