import asyncio
import threading
import time
import unittest
from unittest import mock

from xgent_app import memory_maintenance as memory


class MemoryMaintenanceTests(unittest.TestCase):
    def setUp(self):
        memory._reset_state_for_tests()

    def test_large_command_boundaries(self):
        self.assertFalse(memory.is_large_command(59.999, 10 * 1024 * 1024 - 1))
        self.assertTrue(memory.is_large_command(60.0, 0))
        self.assertTrue(memory.is_large_command(0, 10 * 1024 * 1024))

    def test_supported_runtime_collects_then_trims(self):
        calls = []
        with mock.patch.object(memory, "_load_malloc_trim", return_value=lambda padding: calls.append(("trim", padding)) or 1),              mock.patch.object(memory.gc, "collect", side_effect=lambda: calls.append(("gc", None))):
            self.assertTrue(memory.trim_process_memory())
        self.assertEqual([("gc", None), ("trim", 0)], calls)

    def test_unsupported_runtime_is_noop(self):
        with mock.patch.object(memory, "_load_malloc_trim", return_value=None),              mock.patch.object(memory.gc, "collect") as collect:
            self.assertFalse(memory.trim_process_memory())
        collect.assert_not_called()

    def test_libc_exception_does_not_escape(self):
        def broken(_padding):
            raise RuntimeError("libc failure")

        with mock.patch.object(memory, "_load_malloc_trim", return_value=broken):
            self.assertFalse(memory.trim_process_memory())

    def test_cooldown_skips_second_call(self):
        trim = mock.Mock(return_value=1)
        with mock.patch.object(memory, "_load_malloc_trim", return_value=trim),              mock.patch.object(memory.time, "monotonic", side_effect=[100.0, 100.0, 120.0]):
            self.assertTrue(memory.trim_process_memory())
            self.assertFalse(memory.trim_process_memory())
        self.assertEqual(1, trim.call_count)

    def test_concurrent_forced_calls_are_serialized(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def trim(_padding):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return 1

        with mock.patch.object(memory, "_load_malloc_trim", return_value=trim):
            async def run():
                return await asyncio.gather(
                    memory.trim_process_memory_async(force=True),
                    memory.trim_process_memory_async(force=True),
                )
            self.assertEqual([True, True], asyncio.run(run()))
        self.assertEqual(1, maximum)

    def test_async_large_command_only_trims_at_threshold(self):
        async def run():
            with mock.patch.object(memory, "trim_process_memory_async", new=mock.AsyncMock(return_value=True)) as trim:
                self.assertFalse(await memory.trim_after_large_command(59.0, 100))
                self.assertTrue(await memory.trim_after_large_command(60.0, 100))
                trim.assert_awaited_once_with()
        asyncio.run(run())

    def test_dynamic_limit_uses_95_percent_of_available_plus_rss(self):
        with mock.patch.object(memory, "_read_proc_memory_mb", return_value=(3000, 1000)):
            self.assertEqual((3800, 3000, 1000), memory.current_dynamic_memory_limit_mb())

    def test_dynamic_limit_is_unavailable_when_proc_read_fails(self):
        with mock.patch.object(memory, "_read_proc_memory_mb", return_value=None):
            self.assertIsNone(memory.current_dynamic_memory_limit_mb())

    def test_proc_reader_is_safe_off_linux(self):
        with mock.patch.object(memory.sys, "platform", "win32"), mock.patch("builtins.open") as open_file:
            self.assertIsNone(memory._read_proc_memory_mb())
        open_file.assert_not_called()

    def test_realtime_monitor_waits_one_second_then_calls_at_limit(self):
        async def run():
            on_limit = mock.Mock()
            sleep = mock.AsyncMock()
            with mock.patch.object(memory.asyncio, "sleep", sleep), mock.patch.object(
                memory, "current_dynamic_memory_limit_mb", return_value=(3800, 2800, 3800)
            ):
                await memory.realtime_memory_limit_monitor(on_limit)
            sleep.assert_awaited_once_with(1.0)
            on_limit.assert_called_once_with(3800, 2800, 3800)
        asyncio.run(run())

    def test_realtime_monitor_does_not_call_below_limit(self):
        async def run():
            on_limit = mock.Mock()
            samples = iter(((3800, 3000, 1000), (3800, 0, 3800)))
            with mock.patch.object(memory.asyncio, "sleep", new=mock.AsyncMock()), mock.patch.object(
                memory, "current_dynamic_memory_limit_mb", side_effect=lambda: next(samples)
            ):
                await memory.realtime_memory_limit_monitor(on_limit)
            on_limit.assert_called_once_with(3800, 0, 3800)
        asyncio.run(run())

    def test_periodic_maintenance_waits_before_first_trim(self):
        async def run():
            trim = mock.AsyncMock()

            async def one_sleep(_seconds):
                self.assertEqual(3600.0, _seconds)
                raise asyncio.CancelledError

            with mock.patch.object(memory.asyncio, "sleep", side_effect=one_sleep),                  mock.patch.object(memory, "trim_process_memory_async", trim):
                with self.assertRaises(asyncio.CancelledError):
                    await memory.periodic_memory_maintenance()
                trim.assert_not_awaited()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
