"""run-x 生命周期与有界输出回归；只加载被测定义，不启动 bot。"""
import ast
import asyncio
import codecs
import contextlib
from datetime import datetime
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from xgent_app.shell_output import format_shell_context_output
from xgent_app.error_reporting import error_text

ROOT = Path(__file__).resolve().parents[1]


def load_run_namespace(directory):
    ns = dict(asyncio=asyncio, codecs=codecs, contextlib=contextlib, datetime=datetime,
              logging=logging, logger=logging.getLogger('run-test'), os=os,
              subprocess=subprocess, threading=threading, time=time, uuid=uuid,
              Any=Any, Dict=Dict, Optional=Optional, COMMAND_OUTPUT_DIR=directory,
              error_text=error_text, redact_sensitive_text=lambda s: s,
              to_display_path=lambda p: p, format_shell_context_output=format_shell_context_output,
              AgentCommandBlacklist=SimpleNamespace(check=lambda c: (False, '')),
              memory_maintenance=SimpleNamespace(trim_after_large_command=AsyncMock()),
              diagnose_signal_death=lambda *a: '', BLACKLIST_BLOCKED_NOTICE='blocked')
    path = ROOT / 'xgent_app/sections/shell_triggers.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    wanted = {'RunOutputCapture', 'terminate_async_process'}
    nodes = [n for n in tree.body if getattr(n, 'name', '') in wanted or
             (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and
              t.id.startswith('RUN_OUTPUT_') for t in n.targets))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), ns)
    path = ROOT / 'xgent_app/sections/agent.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AgentExecutor')
    method = next(n for n in cls.body if getattr(n, 'name', '') == 'run_command')
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), ns)
    agent = type('Agent', (), dict(run_command=ns['run_command'], WORK_DIR=directory,
                 get_timeout=staticmethod(lambda: 10), _current_chat_id=42,
                 _current_conversation_id='test-conversation'))
    ns['Agent'] = agent
    return ns


class RunCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xgent-run-test-')
        self.ns = load_run_namespace(self.temp.name)
        self.agent = self.ns['Agent']
        self.children = []
        self.spawned = asyncio.Event()
        self.forwarded = None

    async def asyncTearDown(self):
        for process in self.children:
            if process.returncode is None:
                process.kill()
            await process.wait()
        self.temp.cleanup()

    async def invoke(self, code, stop=None):
        async def create(*args, **kwargs):
            self.forwarded = kwargs
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-c', code, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=os.name != 'nt')
            self.children.append(process)
            self.spawned.set()
            return process
        with patch.object(asyncio, 'create_subprocess_shell', create):
            return await self.agent.run_command('safe-python-probe', stop)

    async def assert_no_new_tasks(self, before):
        await asyncio.sleep(0)
        self.assertEqual(set(), asyncio.all_tasks() - before)
        self.assertTrue(all(p.returncode is not None for p in self.children))

    async def test_small_output_stderr_and_context_unchanged(self):
        before = asyncio.all_tasks()
        result = await self.invoke('import sys; sys.stdout.buffer.write(b"hello\\n"); sys.stderr.buffer.write(b"error\\n")')
        self.assertTrue(result['success'])
        self.ns['memory_maintenance'].trim_after_large_command.assert_awaited_once_with(
            result['elapsed_seconds'], result['output_bytes'])
        self.assertEqual('hello\n\n--- stderr ---\nerror\n', result['output'])
        saved = Path(result['output_path'])
        self.assertEqual(saved.stat().st_size, result['output_bytes'])
        self.assertIn('hello', saved.read_text(encoding='utf-8'))
        self.assertIn('error', saved.read_text(encoding='utf-8'))
        self.assertEqual(self.temp.name, self.forwarded['cwd'])
        self.assertEqual('42', self.forwarded['env']['XGENT_CHAT_ID'])
        self.assertEqual('test-conversation', self.forwarded['env']['XGENT_CONVERSATION_ID'])
        await self.assert_no_new_tasks(before)

    async def test_native_shell_wiring(self):
        before = asyncio.all_tasks()
        result = await self.agent.run_command('echo native-shell-ok')
        self.assertTrue(result['success'], result)
        self.assertIn('native-shell-ok', result['output'])
        self.assertTrue(Path(result['output_path']).is_file())
        await self.assert_no_new_tasks(before)

    async def test_native_shell_timeout(self):
        import shlex
        before = asyncio.all_tasks()
        args = [sys.executable, '-c', 'import time; time.sleep(60)']
        command = subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)
        self.agent.get_timeout = staticmethod(lambda: 0.2)
        result = await self.agent.run_command(command)
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['success'])
        await self.assert_no_new_tasks(before)

    async def test_no_output_and_nonzero_exit(self):
        result = await self.invoke('raise SystemExit(7)')
        self.assertFalse(result['success'])
        self.assertEqual(7, result['return_code'])
        self.assertEqual('(无输出)', result['output'])
        self.assertTrue(Path(result['output_path']).exists())

    async def test_large_output_is_bounded_but_archive_is_complete(self):
        size = 2 * 1024 * 1024
        result = await self.invoke(f'import sys; sys.stdout.write("BEGIN" + "x" * {size} + "END")')
        self.assertTrue(result['success'])
        self.assertLess(len(result['output']), self.ns['RUN_OUTPUT_PREVIEW_CHARS'] + 100)
        self.assertTrue(result['output'].startswith('BEGIN'))
        self.assertTrue(result['output'].endswith('END'))
        self.assertIn('中间输出已省略', result['output'])
        archive = Path(result['output_path']).read_text(encoding='utf-8')
        self.assertIn('BEGIN' + 'x' * size + 'END', archive)

    async def test_archive_has_hard_limit_and_visible_warning(self):
        self.ns['RUN_OUTPUT_ARCHIVE_BYTES'] = 1024
        result = await self.invoke('import sys; sys.stdout.write("x" * 131072)')
        self.assertTrue(result['success'])
        saved = Path(result['output_path'])
        self.assertLessEqual(saved.stat().st_size, 1024)
        self.assertIn('存档不完整', result['output'])
        self.assertTrue(result['archive_truncated'])
        self.assertIn('后续输出已省略', saved.read_text(encoding='utf-8'))

    async def test_disk_error_keeps_bounded_result_without_failing_command(self):
        with patch.object(os, 'makedirs', side_effect=OSError('disk full')):
            result = await self.invoke('print("still visible")')
        self.assertTrue(result['success'])
        self.assertIsNone(result['output_path'])
        self.assertEqual(0, result['output_bytes'])
        self.assertIn('still visible', result['output'])
        self.assertIn('存档失败', result['output'])

    async def test_user_stop_cleans_child_and_waiters(self):
        before = asyncio.all_tasks()
        stop = asyncio.Event()
        task = asyncio.create_task(self.invoke('import time; print("ready", flush=True); time.sleep(60)', stop))
        await self.spawned.wait()
        await asyncio.sleep(0.1)
        stop.set()
        result = await asyncio.wait_for(task, 8)
        self.assertTrue(result['stopped'])
        self.assertFalse(result['success'])
        self.assertIn('手动停止', result['output'])
        await self.assert_no_new_tasks(before)

    async def test_timeout_cleans_child_and_waiters(self):
        before = asyncio.all_tasks()
        self.agent.get_timeout = staticmethod(lambda: 0.1)
        result = await self.invoke('import time; time.sleep(60)')
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['success'])
        await self.assert_no_new_tasks(before)

    async def test_task_cancellation_cleans_child_and_waiters(self):
        before = asyncio.all_tasks()
        task = asyncio.create_task(self.invoke('import time; time.sleep(60)', asyncio.Event()))
        await self.spawned.wait()
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        await self.assert_no_new_tasks(before)
        self.assertTrue(list(Path(self.temp.name).rglob('*.txt')))

    async def test_cancellation_during_cleanup_does_not_cancel_cleanup(self):
        before = asyncio.all_tasks()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        original = self.ns['terminate_async_process']
        async def delayed_cleanup(process):
            cleaning.set()
            await release.wait()
            await original(process)
        self.ns['terminate_async_process'] = delayed_cleanup
        task = asyncio.create_task(self.invoke('import time; time.sleep(60)'))
        await self.spawned.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        await self.assert_no_new_tasks(before)

    async def test_repeated_cancellation_during_spawn_still_reaps_child(self):
        before = asyncio.all_tasks()
        ready = asyncio.Event()
        release = asyncio.Event()
        async def delayed_create(*args, **kwargs):
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-c', 'import time; time.sleep(60)',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != 'nt')
            self.children.append(process)
            ready.set()
            await release.wait()
            return process
        with patch.object(asyncio, 'create_subprocess_shell', delayed_create):
            task = asyncio.create_task(self.agent.run_command('safe-python-probe'))
            await ready.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 8)
        await self.assert_no_new_tasks(before)

    async def test_spawn_failure_returns_error_without_leaked_waiters(self):
        before = asyncio.all_tasks()
        with patch.object(asyncio, 'create_subprocess_shell', side_effect=OSError('cannot spawn')):
            result = await self.agent.run_command('safe-python-probe')
        self.assertFalse(result['success'])
        self.assertIn('cannot spawn', result['output'])
        self.ns['memory_maintenance'].trim_after_large_command.assert_awaited_once_with(
            result['elapsed_seconds'], result['output_bytes'])
        await self.assert_no_new_tasks(before)

    async def test_pipe_error_terminates_running_child(self):
        before = asyncio.all_tasks()
        with patch.object(self.ns['RunOutputCapture'], 'read_stream', new=AsyncMock(side_effect=OSError('pipe failed'))):
            result = await self.invoke('import time; time.sleep(60)')
        self.assertFalse(result['success'])
        self.assertIn('pipe failed', result['output'])
        await self.assert_no_new_tasks(before)

    async def test_incremental_utf8_and_preview_boundary(self):
        self.ns['RUN_OUTPUT_PREVIEW_CHARS'] = 16
        capture = self.ns['RunOutputCapture']('utf8')
        raw = ('开头' + '中' * 20 + '结尾').encode('utf-8')
        class Reader:
            def __init__(self): self.chunks = iter([raw[i:i+2] for i in range(0, len(raw), 2)] + [b''])
            async def read(self, n): return next(self.chunks)
        await capture.read_stream(Reader(), 'stdout')
        await asyncio.to_thread(capture.finish)
        self.assertNotIn('\ufffd', capture.output())
        self.assertTrue(capture.output().startswith('开头'))
        self.assertTrue(capture.output().endswith('结尾'))
        self.assertIn(raw.decode('utf-8'), Path(capture.path).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
