"""Best-effort process memory maintenance for Linux/glibc runtimes."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import os
import platform
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

LARGE_COMMAND_SECONDS = 60.0
LARGE_COMMAND_OUTPUT_BYTES = 10 * 1024 * 1024
TRIM_COOLDOWN_SECONDS = 60.0
MAINTENANCE_INTERVAL_SECONDS = 60 * 60.0
MEMORY_LIMIT_POLL_SECONDS = 1.0
MEMORY_LIMIT_PERCENT = 95

_trim_lock = threading.Lock()
_last_trim_at = float("-inf")


def _is_linux_glibc() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    libc_name = (platform.libc_ver()[0] or "").lower()
    if libc_name == "glibc":
        return True
    try:
        return (os.confstr("CS_GNU_LIBC_VERSION") or "").lower().startswith("glibc ")
    except (AttributeError, OSError, ValueError):
        return False


def _load_malloc_trim() -> Optional[Callable[[int], int]]:
    if not _is_linux_glibc():
        return None
    try:
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    except (AttributeError, OSError):
        return None


def trim_process_memory(*, force: bool = False) -> bool:
    """Run GC and return free glibc arenas to the OS when supported.

    Calls are serialized and normally limited to one successful attempt per
    minute. Every failure is contained because maintenance must never affect
    command execution or service availability.
    """
    global _last_trim_at

    trim = _load_malloc_trim()
    if trim is None:
        return False

    try:
        with _trim_lock:
            now = time.monotonic()
            if not force and now - _last_trim_at < TRIM_COOLDOWN_SECONDS:
                return False
            gc.collect()
            trim(0)
            _last_trim_at = time.monotonic()
            return True
    except Exception:  # pragma: no cover - defensive boundary around libc
        logger.debug("malloc_trim memory maintenance failed", exc_info=True)
        return False


async def trim_process_memory_async(*, force: bool = False) -> bool:
    """Run memory maintenance without blocking the asyncio event loop."""
    try:
        return await asyncio.to_thread(trim_process_memory, force=force)
    except Exception:
        logger.debug("async memory maintenance failed", exc_info=True)
        return False


def is_large_command(elapsed_seconds: float, output_bytes: int) -> bool:
    return (
        elapsed_seconds >= LARGE_COMMAND_SECONDS
        or output_bytes >= LARGE_COMMAND_OUTPUT_BYTES
    )


async def trim_after_large_command(elapsed_seconds: float, output_bytes: int) -> bool:
    """Trim after a command at either configured size boundary."""
    if not is_large_command(elapsed_seconds, output_bytes):
        return False
    return await trim_process_memory_async()


def _read_proc_memory_mb() -> Optional[tuple[int, int]]:
    """Return (MemAvailable, current RSS) in MiB on Linux."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        available_kib = None
        with open("/proc/meminfo", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    available_kib = int(line.split()[1])
                    break
        rss_kib = None
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    rss_kib = int(line.split()[1])
                    break
        if available_kib is None or rss_kib is None:
            return None
        # Keep sub-MiB values visible instead of silently converting them to 0.
        available_mb = max(1, (available_kib + 1023) // 1024)
        rss_mb = max(1, (rss_kib + 1023) // 1024)
        return available_mb, rss_mb
    except (OSError, ValueError, IndexError):
        return None


def current_dynamic_memory_limit_mb() -> Optional[tuple[int, int, int]]:
    """Return (limit, available, rss) for the live 95% process cap.

    MemAvailable already excludes memory currently held by this process.
    Adding RSS back reconstructs the memory pool available to XGent; otherwise
    comparing RSS with 95% of only MemAvailable would trip at about 49% of the
    original pool rather than at 95%.
    """
    sample = _read_proc_memory_mb()
    if sample is None:
        return None
    available_mb, rss_mb = sample
    usable_pool_mb = available_mb + rss_mb
    limit_mb = max(1, usable_pool_mb * MEMORY_LIMIT_PERCENT // 100)
    return limit_mb, available_mb, rss_mb


async def realtime_memory_limit_monitor(
    on_limit: Callable[[int, int, int], None],
    interval_seconds: float = MEMORY_LIMIT_POLL_SECONDS,
) -> None:
    """Recalculate the live 95% process limit after every polling interval."""
    while True:
        await asyncio.sleep(interval_seconds)
        sample = current_dynamic_memory_limit_mb()
        if sample is None:
            continue
        limit_mb, available_mb, rss_mb = sample
        if rss_mb >= limit_mb:
            on_limit(limit_mb, available_mb, rss_mb)
            return


async def periodic_memory_maintenance(
    interval_seconds: float = MAINTENANCE_INTERVAL_SECONDS,
) -> None:
    """Wait one interval, then perform best-effort maintenance forever."""
    while True:
        await asyncio.sleep(interval_seconds)
        await trim_process_memory_async()


def _reset_state_for_tests() -> None:
    global _last_trim_at
    _last_trim_at = float("-inf")
