"""Safe file-backed editing of shared skills and manual memories.

Logical paths never grant arbitrary filesystem access. All UI mutations require a
content revision and cooperate through a cross-process lock independent of turns.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath

from xgent_app.conversations import ExecutionFileLock

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_document_owner = contextvars.ContextVar('knowledge_document_owner', default=None)


class DocumentError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@contextlib.asynccontextmanager
async def document_operation(database_path):
    key = str(Path(database_path).resolve())
    owner = (key, asyncio.current_task())
    if _document_owner.get() == owner:
        yield
        return
    lock = ExecutionFileLock(key + '.documents')
    deadline = asyncio.get_running_loop().time() + 10
    while not lock.acquire():
        if asyncio.get_running_loop().time() >= deadline:
            raise DocumentError('其他操作正在修改文档，请稍后重试。', 409)
        await asyncio.sleep(.03)
    token = _document_owner.set(owner)
    try:
        yield
    finally:
        _document_owner.reset(token)
        lock.release()


def split_skill_text(text):
    """Extract complete ! fences, respecting other fenced examples in the file."""
    lines = text.splitlines(keepends=True)
    spans, summaries = [], []
    start, fence, summary = None, 0, False
    for index, line in enumerate(lines):
        stripped = line.strip().lstrip('\ufeff')
        if fence:
            if re.fullmatch(r'[\x60]{' + str(fence) + r',}\s*', stripped):
                if summary:
                    spans.append((start, index + 1))
                    summaries.append(''.join(lines[start + 1:index]).strip('\r\n'))
                start, fence, summary = None, 0, False
            continue
        match = re.fullmatch(r'([\x60]{3,})([^\x60]*)', stripped)
        if match:
            start, fence = index, len(match[1])
            summary = (match[2].strip().split() or [''])[0] == '!'
    excluded = {i for left, right in spans for i in range(left, right)}
    body = ''.join(line for i, line in enumerate(lines) if i not in excluded)
    return {'summary': '\n\n'.join(summaries), 'body': body.strip('\r\n')}


def compose_skill_text(summary, body):
    if not isinstance(summary, str) or not isinstance(body, str):
        raise DocumentError('技能简介和正文必须是文本。')
    if not summary:
        return body
    fence = chr(96) * max(3, max((len(m[0]) + 1 for m in re.finditer(r'[\x60]+', summary)), default=3))
    return fence + '!\n' + summary + '\n' + fence + ('\n\n' + body if body else '\n')


def _encoded(content):
    if not isinstance(content, str):
        raise DocumentError('文档内容必须是文本。')
    try:
        raw = content.encode('utf-8')
    except UnicodeEncodeError:
        raise DocumentError('文档包含无效的 Unicode 字符。') from None
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentError('文档不能超过 2 MiB；请使用文件工具处理更大的文件。', 413)
    return raw


def _revision(raw):
    return hashlib.sha256(raw).hexdigest()


def _decode(raw):
    for encoding in ('utf-8', 'gbk'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise DocumentError('文件不是有效的 UTF-8 或 GBK 文本文档。')


def _leaf(value, extension):
    if not isinstance(value, str):
        raise DocumentError('请输入文件名。')
    name = value.strip()
    if not name or chr(92) in name or name.startswith('.') or name.endswith(('.', ' ')) or re.search(r'[\x00-\x1f<>:"/\|?*]', name):
        raise DocumentError('文件名不能为空，也不能包含路径或特殊字符。')
    if name.split('.')[0].rstrip(' .').upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        raise DocumentError('此文件名是系统保留名称。')
    if not Path(name).suffix:
        name += extension
    if len(name) > 120 or len(name.encode('utf-8')) > 240:
        raise DocumentError('文件名过长。')
    return name


class KnowledgeDocuments:
    def __init__(self, namespace):
        self.ns = namespace

    def _roots(self, kind, logical):
        if kind == 'memories':
            return [(Path(self.ns['MEMORY_DIR']).resolve(), logical)]
        if kind != 'skills':
            raise DocumentError('未知文档类型。')
        if logical.startswith('private/'):
            return [(Path(self.ns['SKILL_PRIVATE_DIR']).resolve(), logical[8:])]
        return [(Path(self.ns[name]).resolve(), logical) for name in ('SKILL_PUBLIC_DIR','SKILL_LEGACY_DIR')]

    @staticmethod
    def _inside(root, candidate):
        root = root.resolve()
        if not candidate.is_absolute() or not candidate.resolve().is_relative_to(root) or candidate == root:
            raise DocumentError('文件路径越界，操作已拒绝。')
        current = candidate
        while current != root:
            if current.is_symlink():
                raise DocumentError('不允许通过符号链接读写文档。')
            current = current.parent
        return candidate

    def resolve(self, kind, logical, *, writable=False):
        if not isinstance(logical, str) or not logical or chr(92) in logical:
            raise DocumentError('无效的文件路径。')
        parts = PurePosixPath(logical).parts
        if str(PurePosixPath(logical)) != logical:
            raise DocumentError('请使用规范的文件相对路径。')
        if logical.startswith('/') or any(part in ('.','..') or part.startswith('.') or ':' in part for part in parts):
            raise DocumentError('无效的文件路径。')
        if kind == 'memories' and len(parts) != 1:
            raise DocumentError('记忆必须位于记忆目录中。')
        extensions = {'.txt'} if kind == 'memories' else set(self.ns.get('SKILL_FILE_EXTENSIONS', {'.md','.markdown','.txt'}))
        if Path(logical).suffix.lower() not in extensions:
            raise DocumentError('不支持此文档扩展名。')
        found = []
        for root, relative in self._roots(kind, logical):
            candidate = self._inside(root, root / relative)
            if candidate.is_file() and candidate not in [p for _, p in found]:
                found.append((root, candidate))
        if not found:
            raise DocumentError('文件已不存在，请刷新列表。', 404)
        if writable and len(found) > 1:
            raise DocumentError('公共目录和旧版技能目录存在同路径文件，请先整理重名文件，避免修改错误的对象。', 409)
        return found[0]

    def read(self, kind, logical, *, full=True):
        root, file = self.resolve(kind, logical)
        info = file.stat()
        if info.st_size > MAX_DOCUMENT_BYTES:
            raise DocumentError('文件超过 2 MiB，请使用文件工具管理。', 413)
        try:
            with file.open('rb') as stream:
                raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        except FileNotFoundError:
            raise DocumentError('文件已不存在，请刷新列表。', 404) from None
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise DocumentError('文件超过 2 MiB，请使用文件工具管理。', 413)
        content = _decode(raw)
        parts = split_skill_text(content) if kind == 'skills' else {'summary':'', 'body':content}
        result = {'path':logical,'filename':file.name,'name':file.stem,'revision':_revision(raw),
                  'updated_at':info.st_mtime,'size':len(raw),
                  'source':'private' if logical.startswith('private/') else 'public' if kind=='skills' else 'memory',
                  'summary':' '.join(parts['summary'].split())[:240], 'preview':' '.join(parts['body'].split())[:240]}
        if full:
            result.update(content=content, **parts)
        return result

    def list(self, kind):
        names = self.ns['list_skill_files' if kind=='skills' else 'list_memory_files']()
        items = []
        for logical in sorted(set(names), key=str.casefold):
            try:
                items.append(self.read(kind, logical, full=False))
            except DocumentError as exc:
                if exc.status == 413:
                    items.append({'path':logical,'filename':Path(logical).name,'name':Path(logical).stem,
                                  'source':'private' if logical.startswith('private/') else 'public' if kind=='skills' else 'memory',
                                  'summary':str(exc),'preview':'','readonly':True,'revision':None})
        return items

    def create(self, kind):
        if kind not in {'skills','memories'}:
            raise DocumentError('未知文档类型。')
        root = Path(self.ns['SKILL_PRIVATE_DIR'] if kind=='skills' else self.ns['MEMORY_DIR']).resolve()
        root.mkdir(parents=True, exist_ok=True)
        stem, extension = ('新技能','.md') if kind=='skills' else ('新记忆','.txt')
        for number in range(1,10001):
            name = stem + (f' {number}' if number > 1 else '') + extension
            candidate = self._inside(root, root / name)
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            os.close(fd)
            return self.read(kind, ('private/' if kind=='skills' else '') + name)
        raise DocumentError('同名空白文档过多，请先整理列表。', 409)

    def _checked(self, kind, logical, revision):
        root, file = self.resolve(kind, logical, writable=True)
        with file.open('rb') as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise DocumentError('文件超过 2 MiB，请使用文件工具管理。', 413)
        if not isinstance(revision,str) or revision != _revision(raw):
            raise DocumentError('文件已被其他操作修改，请先重新读取；你的输入尚未覆盖服务器文件。', 409)
        return root, file

    def write(self, kind, logical, revision, content):
        raw = _encoded(content)
        root, file = self._checked(kind, logical, revision)
        temporary = self._inside(root, file.with_name('.xgent-edit-' + uuid.uuid4().hex + '.tmp'))
        try:
            with temporary.open('xb') as stream:
                os.chmod(temporary, stat.S_IMODE(file.stat().st_mode))
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            self._checked(kind, logical, revision)
            self._inside(root, file)
            os.replace(temporary, file)
        finally:
            if temporary.exists():
                self._inside(root, temporary).unlink()
        return self.read(kind, logical)

    def rename_target(self, kind, logical, revision, name):
        root, old = self._checked(kind, logical, revision)
        filename = _leaf(name, old.suffix)
        if Path(filename).suffix.lower() not in ({'.txt'} if kind=='memories' else set(self.ns.get('SKILL_FILE_EXTENSIONS',{'.md','.markdown','.txt'}))):
            raise DocumentError('文件扩展名不受支持。')
        new_logical = str(PurePosixPath(logical).with_name(filename))
        target = self._inside(root, old.with_name(filename))
        for other_root, relative in self._roots(kind,new_logical):
            other = self._inside(other_root, other_root / relative)
            if other.exists() and not other.samefile(old):
                raise DocumentError('同名文件已存在，未覆盖任何文件。', 409)
        return root, old, target, new_logical

    def rename(self, kind, logical, revision, name):
        root, old, target, new_logical = self.rename_target(kind,logical,revision,name)
        if old.name != target.name:
            temporary = self._inside(root, old.with_name('.xgent-rename-' + uuid.uuid4().hex + '.tmp'))
            os.rename(old, temporary)
            try:
                if target.exists():
                    raise DocumentError('目标文件刚被创建，未覆盖任何文件。',409)
                os.rename(temporary, target)
            except BaseException:
                if not old.exists():
                    os.rename(temporary, self._inside(root,old))
                raise
        return self.read(kind,new_logical)

    def stage_delete(self, kind, logical, revision):
        root, file = self._checked(kind,logical,revision)
        temporary = self._inside(root,file.with_name('.xgent-delete-' + uuid.uuid4().hex + '.tmp'))
        os.rename(file, temporary)
        return root, file, temporary

    def finish_delete(self, ticket, *, rollback=False):
        root, original, temporary = ticket
        self._inside(root,original); self._inside(root,temporary)
        if rollback:
            if original.exists():
                raise DocumentError('原路径已出现新文件，不能覆盖；请人工恢复临时文档。',409)
            os.rename(temporary,original)
        else:
            temporary.unlink()


async def bump_knowledge_revision(conn):
    cursor = await conn.execute("SELECT value FROM config WHERE key='knowledge_revision'")
    row = await cursor.fetchone()
    revision = int(json.loads(row['value'])) + 1 if row else 1
    await conn.execute("INSERT INTO config(key,value) VALUES('knowledge_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(revision),))
    return revision


async def update_skill_references(db, old, new=None, *, keep_old=False):
    values = {}
    async with db._transaction() as conn:
        for key in ('disabled_skills','hidden_skills'):
            cursor = await conn.execute('SELECT value FROM config WHERE key=?',(key,))
            row = await cursor.fetchone()
            raw = json.loads(row['value']) if row else []
            items = {item for item in raw if isinstance(item,str)} if isinstance(raw,list) else set()
            if new is not None:
                items.discard(new)
                if old in items:
                    items.add(new)
            if not keep_old:
                items.discard(old)
            values[key] = sorted(items)
            await conn.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(values[key])))
        await bump_knowledge_revision(conn)
    for key,value in values.items():
        db._config_cache[key] = value
    return values


async def read_knowledge_state(db):
    # The legacy DB shares one connection. Serialize with writers so a poll cannot
    # cache an uncommitted revision together with only half of a state update.
    async with db._write_lock:
        conn = await db._get_conn()
        cursor = await conn.execute("SELECT key,value FROM config WHERE key IN ('knowledge_revision','disabled_skills','hidden_skills')")
        rows = {row['key']:json.loads(row['value']) for row in await cursor.fetchall()}
        await cursor.close()
    return {'revision':int(rows.get('knowledge_revision') or 0),
            **{key:rows.get(key,[]) if isinstance(rows.get(key,[]),list) else [] for key in ('disabled_skills','hidden_skills')}}
