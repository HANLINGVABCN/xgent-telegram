"""Shared runtime lease / exclusive cold-maintenance lock, outside data trees.

The stable parent path prevents renaming a data directory from invalidating the
lock inode. OS locks are released on process death and work across CLI/server.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class DataLease:
    def __init__(self, root, *, exclusive=False):
        root = Path(root).resolve()
        key = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:20]
        # Runtime and maintenance must always lock the same inode, including
        # when run as different users or with different temporary directories.
        # Never choose a fallback based on this process's write permissions.
        self.path = root.parent / (".xgent-data-" + key + ".lock")
        self.exclusive = exclusive
        self.fd = None
        self.overlapped = None

    def acquire(self):
        if self.fd is not None:
            return True
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.set_inheritable(fd, False)
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                class OVERLAPPED(ctypes.Structure):
                    _fields_ = [
                        ("Internal", ctypes.c_size_t),
                        ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD),
                        ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE),
                    ]

                overlap = OVERLAPPED()
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                lock = kernel.LockFileEx
                lock.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                ]
                lock.restype = wintypes.BOOL
                if not lock(
                    msvcrt.get_osfhandle(fd),
                    1 | (2 if self.exclusive else 0),
                    0,
                    1,
                    0,
                    ctypes.byref(overlap),
                ):
                    raise OSError(ctypes.get_last_error(), "data directory is in use")
                self.overlapped = overlap
            else:
                import fcntl

                fcntl.flock(
                    fd,
                    (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
                    | fcntl.LOCK_NB,
                )
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            self.overlapped = None

    def __enter__(self):
        if not self.acquire():
            raise ValueError(
                "数据目录仍被运行中的服务或 CLI 占用，请先停止后重试；未修改数据。"
            )
        return self

    def __exit__(self, *args):
        self.release()
