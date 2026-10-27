"""Real OS maintenance locks, isolated from installed instances and services."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xgent_app.data_lock import DataLease


class DataLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xgent-lease-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'
        self.root.mkdir(mode=0o700)

    def probe(self, exclusive):
        script = (
            'import sys\n'
            'from xgent_app.data_lock import DataLease\n'
            'lease = DataLease(sys.argv[1], exclusive=sys.argv[2] == "exclusive")\n'
            'acquired = lease.acquire()\n'
            'lease.release()\n'
            'raise SystemExit(0 if acquired else 3)\n'
        )
        result = subprocess.run(
            [sys.executable, '-c', script, str(self.root), 'exclusive' if exclusive else 'shared'],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        )
        self.assertIn(result.returncode, (0, 3), result.stderr)
        return result.returncode == 0

    def test_lock_location_ignores_process_permissions_and_temp_settings(self):
        lease = DataLease(self.root)
        with (
            patch.dict(os.environ, {'TMPDIR': str(self.root), 'TEMP': str(self.root)}),
            patch('os.access', return_value=False),
            patch('tempfile.gettempdir', return_value=str(self.root)),
        ):
            other_process = DataLease(self.root)
        self.assertEqual(lease.path, other_process.path)
        self.assertEqual(lease.path.parent, self.root.parent)
        self.assertFalse(lease.path.is_relative_to(self.root))

    def test_shared_runtime_and_exclusive_maintenance_conflict_across_processes(self):
        runtime = DataLease(self.root)
        self.addCleanup(runtime.release)
        self.assertTrue(runtime.acquire())
        self.assertTrue(self.probe(False))
        self.assertFalse(self.probe(True))
        runtime.release()
        self.assertTrue(self.probe(True))
        maintenance = DataLease(self.root, exclusive=True)
        self.addCleanup(maintenance.release)
        self.assertTrue(maintenance.acquire())
        self.assertFalse(self.probe(False))
        self.assertFalse(self.probe(True))
        maintenance.release()
        self.assertTrue(self.probe(False))

    def test_replacing_data_directory_does_not_bypass_held_lock(self):
        maintenance = DataLease(self.root, exclusive=True)
        self.addCleanup(maintenance.release)
        self.assertTrue(maintenance.acquire())
        self.root.rename(self.root.parent / 'rollback')
        self.root.mkdir(mode=0o700)
        self.assertEqual(maintenance.path, DataLease(self.root).path)
        self.assertFalse(self.probe(False))
        self.assertFalse(self.probe(True))
        maintenance.release()
        self.assertTrue(self.probe(False))

    def test_permission_error_does_not_silently_switch_to_an_unlocked_file(self):
        lease = DataLease(self.root)
        with patch('xgent_app.data_lock.os.open', side_effect=PermissionError('read-only parent')):
            with self.assertRaises(PermissionError):
                lease.acquire()
        self.assertIsNone(lease.fd)


if __name__ == '__main__':
    unittest.main()
