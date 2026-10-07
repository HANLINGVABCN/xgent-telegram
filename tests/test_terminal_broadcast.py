"""Terminal output broadcast and retention, with POSIX integration where available."""
import os
import time
import unittest
from xgent_app.web_terminal import TerminalSession, TerminalManager

class TerminalBroadcastTests(unittest.TestCase):
    def test_two_cursors_receive_same_output_without_consuming_each_other(self):
        manager=TerminalManager();session=TerminalSession('fixture',-1,-1,80,24)
        manager._sessions[session.id]=session
        manager._append(session,b'one');manager._append(session,b'two')
        first=manager.read_frames(session.id,0,0);second=manager.read_frames(session.id,0,0)
        self.assertEqual(first['frames'],second['frames']);self.assertEqual(2,len(first['frames']))
        self.assertEqual([],manager.read_frames(session.id,2,0)['frames'])
        self.assertFalse(first['gap'])

    def test_buffer_is_bounded_and_stale_cursor_reports_gap(self):
        manager=TerminalManager();session=TerminalSession('fixture',-1,-1,80,24);manager._sessions[session.id]=session
        for _ in range(50):manager._append(session,b'x'*65536)
        self.assertLessEqual(session.buffer_bytes,2*1024*1024)
        batch=manager.read_frames(session.id,0,0);self.assertTrue(batch['gap']);self.assertLessEqual(len(batch['frames']),16)
        self.assertFalse(manager.read_frames(session.id,session.sequence,0)['gap'])
        session.closed=True;self.assertTrue(manager.read_frames(session.id,session.sequence,0)['closed'])

    @unittest.skipUnless(os.name=='posix','PTY lifecycle requires POSIX')
    def test_real_pty_reconnect_and_close_reap_reader(self):
        manager=TerminalManager();session=manager.open()
        try:
            manager.write(session.id,b'printf "BROADCAST_TEST\\n"\n')
            deadline=time.time()+4;cursor=0;received=b''
            while time.time()<deadline:
                batch=manager.read_frames(session.id,cursor,.1)
                for seq,data in batch['frames']:cursor=seq;received+=data
                if b'BROADCAST_TEST' in received:break
            self.assertIn(b'BROADCAST_TEST',received)
            self.assertTrue(manager.read_frames(session.id,0,0)['frames'])
        finally:manager.close(session.id)
        self.assertFalse(session.reader.is_alive())
        with self.assertRaises(ChildProcessError):os.waitpid(session.pid,os.WNOHANG)
