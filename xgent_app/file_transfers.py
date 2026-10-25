"""Short, resumable HTTP file requests independent of Bot and proxy limits."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .file_ingress import fingerprint

CHUNK_BYTES = 4 * 1024 * 1024  # Transport frame size, never a whole-file limit.
IDENTIFIER = re.compile(r'[0-9a-f]{64}')


class TransferError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class TransferLease:
    """The transfer store, not the asynchronous conversation, owns staged bytes."""
    def cleanup(self):
        pass


class FileTransfers:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        self.locks = {}
        self.active = set()
        self.terminal = {}
        self.workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='file-finalize')

    def lock(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise TransferError('无效上传标识', 400)
        with self.guard:
            return self.locks.setdefault(identifier, threading.RLock())

    def directory(self, identifier):
        directory = self.root / identifier
        if directory.is_symlink() or directory.resolve() != directory:
            raise TransferError('上传目录无效', 400)
        return directory

    def load(self, identifier):
        try:
            path = self.directory(identifier) / 'state.json'
            if path.is_symlink():
                raise TransferError('上传状态路径无效', 400)
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise TransferError('上传会话不存在，请重新建立传输', 404) from None

    def save(self, manifest):
        directory = self.directory(manifest['id'])
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.state-', delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, directory / 'state.json')
        finally:
            temporary.unlink(missing_ok=True)

    def file(self, identifier, index):
        path = self.directory(identifier) / f'{index}.part'
        if path.is_symlink():
            raise TransferError('上传暂存文件无效', 400)
        return path

    def view(self, manifest):
        result = {'ok': True, 'transfer_id': manifest['id'], 'state': manifest['state'],
                  'chunk_bytes': CHUNK_BYTES, 'offsets': []}
        for index in range(len(manifest['files'])):
            path = self.file(manifest['id'], index)
            result['offsets'].append(path.stat().st_size if path.exists() else 0)
        if manifest['state'] == 'accepted':
            result.update(manifest['result'])
        if manifest.get('error'):
            result['error'] = manifest['error']
        return result

    def begin(self, data):
        key, cid = data.get('idempotency_key'), data.get('conversation_id')
        files, mode = data.get('files'), data.get('mode')
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or not isinstance(cid, str) or not cid:
            raise TransferError('缺少上传标识或目标会话', 400)
        if mode not in {'attachment', 'server_upload'} or not isinstance(files, list) or not files:
            raise TransferError('文件清单或上传模式无效', 400)
        normalized = []
        for file in files:
            if (not isinstance(file, dict) or not isinstance(file.get('filename'), str)
                    or not file['filename'] or type(file.get('size')) is not int or file['size'] < 0):
                raise TransferError('文件名称或大小无效', 400)
            normalized.append({'filename': file['filename'], 'size': file['size']})
        text, modes, captions = data.get('text', ''), data.get('modes', []), data.get('captions', [])
        if (not isinstance(text, str) or not isinstance(modes, list) or not isinstance(captions, list)
                or modes and (len(modes) != len(files) or any(m not in {'attachment', 'server_upload'} for m in modes))
                or any(not isinstance(c, str) for c in captions)):
            raise TransferError('文件文字或模式无效', 400)
        spec = {'files': normalized, 'mode': mode, 'text': text, 'modes': modes,
                'captions': captions, 'conversation_id': cid, 'idempotency_key': key}
        signature = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        identifier = hashlib.sha256((cid + '\0' + key).encode()).hexdigest()
        with self.lock(identifier):
            directory = self.directory(identifier)
            if directory.exists():
                manifest = self.load(identifier)
                if manifest['signature'] != signature:
                    raise TransferError('相同上传标识已用于其他文件')
            else:
                directory.mkdir(mode=0o700)
                manifest = {**spec, 'id': identifier, 'signature': signature, 'state': 'receiving', 'created_at': time.time()}
                for index in range(len(files)):
                    self.file(identifier, index).touch(mode=0o600)
                self.save(manifest)
            return self.status(identifier)

    def status(self, identifier):
        with self.lock(identifier):
            manifest = self.terminal.get(identifier) or self.load(identifier)
            if manifest['state'] == 'processing' and identifier not in self.active:
                # A restart never automatically repeats an AI request. A client
                # may explicitly finish again; the conversation's key deduplicates it.
                manifest['state'] = 'receiving'
                self.save(manifest)
            return self.view(manifest)

    def append(self, identifier, index, offset, stream, length):
        with self.lock(identifier):
            manifest = self.load(identifier)
            if manifest['state'] not in {'receiving', 'failed'}:
                raise TransferError('文件已提交，请查询接收状态')
            if not 0 <= index < len(manifest['files']) or offset < 0 or not 0 < length <= CHUNK_BYTES:
                raise TransferError('上传分块参数无效', 400)
            path = self.file(identifier, index)
            size = path.stat().st_size
            if offset > size or offset + length > manifest['files'][index]['size']:
                raise TransferError('上传偏移不匹配，请查询已接收位置')
            replay = offset < size
            if replay and offset + length > size:
                raise TransferError('重复分块与已接收位置不匹配')
            with open(path, 'r+b') as handle:
                handle.seek(offset)
                remaining = length
                try:
                    while remaining:
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise TransferError('分块传输中断，可从已接收位置继续')
                        remaining -= len(chunk)
                        if replay:
                            if handle.read(len(chunk)) != chunk:
                                raise TransferError('重复分块内容不一致，请重新选择文件')
                        else:
                            handle.write(chunk)
                    handle.flush()
                except BaseException:
                    if not replay:
                        handle.truncate(size)
                    raise
            return {'ok': True, 'offset': path.stat().st_size}

    def complete(self, identifier, submit):
        with self.lock(identifier):
            manifest = self.terminal.get(identifier) or self.load(identifier)
            if manifest['state'] == 'accepted' or identifier in self.active:
                return self.view(manifest)
            for index, file in enumerate(manifest['files']):
                if self.file(identifier, index).stat().st_size != file['size']:
                    raise TransferError('仍有未传完的文件，请继续传输')
            manifest.update(state='processing', error=None)
            self.save(manifest)
            self.terminal.pop(identifier, None)
            self.active.add(identifier)
            try:
                self.workers.submit(self.finalize, identifier, submit)
            except BaseException:
                self.active.discard(identifier)
                raise
            return self.view(manifest)

    def finalize(self, identifier, submit):
        try:
            manifest = self.load(identifier)
            files = []
            for index, file in enumerate(manifest['files']):
                path = self.file(identifier, index)
                size, digest = fingerprint(path)
                if size != file['size']:
                    raise TransferError('暂存文件大小校验失败')
                files.append({**file, 'path': str(path), 'sha256': digest})
            data = {'files': files, 'text': manifest['text'], 'idempotency_key': manifest['idempotency_key'],
                    'modes': json.dumps(manifest['modes']), 'captions': json.dumps(manifest['captions']),
                    '_temporary': TransferLease()}
            receipt = submit(data, manifest['mode'], manifest['conversation_id'])
            if isinstance(receipt, Future):
                def finished(future):
                    try:
                        self.finish(identifier, future.result())
                    except Exception as exc:
                        self.finish(identifier, {'ok': False, 'error': str(exc)})
                receipt.add_done_callback(finished)
            else:
                self.finish(identifier, receipt if isinstance(receipt, dict) else {'ok': True})
        except Exception as exc:
            error = ('服务器磁盘空间不足，文件尚未确认接收；已传分块保留，请释放空间后重试。'
                     if isinstance(exc, OSError) and exc.errno == 28 else str(exc))
            self.finish(identifier, {'ok': False, 'error': error})

    def finish(self, identifier, result):
        with self.lock(identifier):
            manifest = self.load(identifier)
            manifest.update(state='accepted' if result.get('ok') else 'failed', result=result,
                            error=None if result.get('ok') else result.get('error', '文件接收失败'))
            stored = False
            try:
                self.save(manifest)
                stored = True
            except OSError:
                # A full disk must not leave the client polling forever. Keep
                # the known receipt in memory; a restart still uses the same
                # database idempotency key to recover the accepted operation.
                self.terminal[identifier] = manifest
            finally:
                self.active.discard(identifier)
            if result.get('ok') and stored:
                # All originals have been adopted before a successful receipt.
                for index in range(len(manifest['files'])):
                    try:
                        self.file(identifier, index).unlink(missing_ok=True)
                    except OSError:
                        pass  # A valid receipt is not undone by staging cleanup.

    def close(self):
        self.workers.shutdown(wait=False, cancel_futures=True)
