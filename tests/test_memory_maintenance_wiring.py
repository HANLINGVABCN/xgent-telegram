import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MemoryMaintenanceWiringTests(unittest.TestCase):
    def test_agent_trims_success_and_exception_results(self):
        source = (ROOT / "xgent_app/sections/agent.py").read_text(encoding="utf-8")
        self.assertEqual(2, source.count("memory_maintenance.trim_after_large_command"))
        self.assertIn("elapsed_seconds, saved['bytes']", source)

    def test_trigger_uses_complete_output_file_size(self):
        source = (ROOT / "xgent_app/sections/shell_triggers.py").read_text(encoding="utf-8")
        self.assertIn("os.path.getsize(output_path)", source)
        self.assertIn("memory_maintenance.trim_after_large_command(", source)
        self.assertIn("elapsed_seconds, output_bytes", source)
        self.assertIn("await terminate_async_process(process)", source)

    def test_runtime_starts_and_cleanly_stops_periodic_task(self):
        source = (ROOT / "xgent_app/sections/runtime.py").read_text(encoding="utf-8")
        self.assertIn("start_memory_maintenance()", source)
        self.assertIn("memory_maintenance.realtime_memory_limit_monitor", source)
        self.assertIn('os.getenv("PM2_MAX_MEMORY_RESTART", "").strip()', source)
        self.assertIn("request_app_stop(75)", source)
        self.assertIn("await stop_memory_maintenance()", source)
        self.assertIn("await task", source)


if __name__ == "__main__":
    unittest.main()
