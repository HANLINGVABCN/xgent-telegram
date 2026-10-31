"""Bounded file scanning in a cancellable OS process (no application imports)."""
from __future__ import annotations

import asyncio
from collections import deque
import fnmatch
import json
from pathlib import Path
import re
import sys

CHUNK = 65536
MAX_LINE = 16 * 1024 * 1024
SKIP = {'.git', 'venv', '.venv', 'node_modules', '__pycache__', 'xgent_storage', 'backups', 'logs'}
EXCLUDED = {'.db', '.sqlite', '.sqlite3', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.zip', '.gz', '.tar', '.lock'}


def lines(path):
    with open(path, 'rb') as handle:
        pending = bytearray()
        while chunk := handle.read(CHUNK):
            pending.extend(chunk)
            while (index := pending.find(b'\n')) >= 0:
                if index > MAX_LINE:
                    raise ValueError('单行超过16MB，扫描未完成；请缩小读取范围。')
                line = bytes(pending[:index]).rstrip(b'\r')
                del pending[:index+1]
                yield line.decode('utf-8', 'replace')
            if len(pending) > MAX_LINE:
                raise ValueError('单行超过16MB，扫描未完成；请缩小读取范围。')
        if pending:
            yield pending.decode('utf-8', 'replace').rstrip('\r')


def scan(request):
    path = Path(request['path'])
    if request['kind'] == 'read':
        if not request.get('range') and path.stat().st_size > 512*1024:
            raise ValueError('全文超过512KB，请指定行范围读取。')
        rng = request.get('range') or ''
        match = re.fullmatch(r'(\d+):\+(\d+)', rng)
        if match:
            start = max(1, int(match[1])); end = start+max(1, int(match[2]))-1
        elif rng:
            a, _, b = rng.partition('-'); start = max(1, int(a)); end = int(b) if b else (start if '-' not in rng else sys.maxsize)
        else:
            start, end = 1, sys.maxsize
        selected, total, size = [], 0, 0
        for total, line in enumerate(lines(path), 1):
            if start <= total <= end:
                size += len(line.encode('utf-8'))
                if size > MAX_LINE:
                    raise ValueError('读取片段超过16MB，请缩小行范围。')
                selected.append(line)
        # Preserve the original text-reader's trailing empty-line convention.
        with path.open('rb') as handle:
            if path.stat().st_size:
                handle.seek(-1, 2)
                if handle.read(1) == b'\n':
                    total += 1
                    if start <= total <= end:
                        selected.append('')
        return {'lines': selected, 'total': max(1, total), 'start': start, 'end': min(end, max(1,total))}
    if request['context_n'] > 50 or request['max_hits'] > 1000:
        raise ValueError('单次检索最多1000个命中、上下文最多50行，请缩小范围。')
    flags = re.IGNORECASE if request['ignore_case'] else 0
    pattern = re.compile(request['pattern'] if request['regex'] else re.escape(request['pattern']), flags)
    glob = request.get('glob')
    files = [path] if path.is_file() else path.rglob(glob or '*')
    results, truncated, visited = [], False, 0
    for target in files:
        if not target.is_file() or target.is_symlink():
            continue
        relative = target.relative_to(path).parts if path.is_dir() else ()
        explicit_log = bool(glob and '.log' in glob.lower()) or path.is_file()
        if relative and (any(part in SKIP and not (explicit_log and part in {'logs', 'xgent_storage'}) for part in relative[:-1]) or
                         (target.suffix.lower() in EXCLUDED) or
                         (not explicit_log and '.log' in target.name.lower())):
            continue
        visited += 1
        if visited > 20000:
            raise ValueError('文件超过20000个，请缩小检索范围。')
        try:
            with target.open('rb') as handle:
                if b'\0' in handle.read(4096):
                    continue
            before = deque(maxlen=request['context_n'])
            pending_hits = []
            for number, line in enumerate(lines(target), 1):
                for hit in pending_hits:
                    if len(hit['after']) < request['context_n']:
                        hit['after'].append((number, line[:200]))
                pending_hits = [hit for hit in pending_hits if len(hit['after']) < request['context_n']]
                if pattern.search(line):
                    if len(results) >= request['max_hits']:
                        truncated = True
                        break
                    hit = {'path': str(target), 'line': number, 'match': line[:500],
                           'before': list(before), 'after': []}
                    results.append(hit); pending_hits.append(hit)
                before.append((number, line[:200]))
            if truncated:
                break
        except OSError:
            continue
    return {'results': results, 'truncated': truncated}


async def run(request, stop_event=None, timeout=900):
    process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    communicate = asyncio.create_task(process.communicate(json.dumps(request).encode()))
    stopped = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    try:
        done, _ = await asyncio.wait({communicate, *([stopped] if stopped else [])}, timeout=timeout,
                                    return_when=asyncio.FIRST_COMPLETED)
        if communicate not in done:
            raise ValueError('读取已停止。' if stopped in done else '读取超时，请缩小范围。')
        output, _ = communicate.result()
        result = json.loads(output)
        if result.get('error'):
            raise ValueError(result['error'])
        return result
    finally:
        if stopped is not None:
            stopped.cancel()
        if process.returncode is None:
            process.kill()
        await process.wait()
        if not communicate.done():
            await communicate


if __name__ == '__main__':
    try:
        result = scan(json.load(sys.stdin))
    except (OSError, ValueError) as exc:
        result = {'error': str(exc)}
    try:
        status = Path('/proc/self/status').read_text(encoding='ascii')
        match = re.search(r'^VmHWM:\s+(\d+)', status, re.M)
        if match:
            result['peak_rss_mb'] = int(match[1]) / 1024
    except OSError:
        pass
    print(json.dumps(result, ensure_ascii=True))
