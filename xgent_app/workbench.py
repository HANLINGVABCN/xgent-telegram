"""Application-facing workbench service; HTTP delegates here, not a second agent engine."""
from __future__ import annotations
import contextlib
from xgent_app.knowledge import KnowledgeDocuments, DocumentError, document_operation, compose_skill_text, bump_knowledge_revision, update_skill_references, read_knowledge_state
import asyncio
import base64
from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from pathlib import Path
import time
from urllib.parse import urlsplit, parse_qsl
from zoneinfo import ZoneInfo
from .web_history import build_history_message
from .ui_history import ui_record, hide_audit_record

class WorkbenchError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

def _integer(value, default=50, maximum=100):
    try:
        if isinstance(value, bool): raise ValueError()
        return max(1, min(int(value), maximum))
    except (ValueError, TypeError): return default

def _pack(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode()

def _unpack(value):
    try:
        data = json.loads(base64.urlsafe_b64decode(value))
        if not isinstance(data, list) or len(data) != 4 or not isinstance(data[0], (int, float)): raise ValueError()
        if not math.isfinite(data[0]) or data[1] not in (0, 1) or not isinstance(data[2], str): raise ValueError()
        return data
    except Exception: raise WorkbenchError('历史游标无效，请重新加载') from None

from xgent_app.conversations import conversation_operation, current_scope, get_conversations, stamp_frame, ConversationError


class Workbench:
    def __init__(self, namespace):
        self.ns = namespace
        self._ready = False
        self._init_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._background = set()
        self._settings_lock = asyncio.Lock()

    async def db(self):
        db = await self.ns['BotMemoryDB'].get_instance()
        if not self._ready:
            async with self._init_lock:
                if not self._ready:
                    async with db._write() as conn:
                        await conn.execute('CREATE INDEX IF NOT EXISTS wb_history_order ON global_messages(timestamp, id)')
                        await conn.execute('CREATE INDEX IF NOT EXISTS wb_ui_order ON ui_messages(generation, timestamp, ui_message_id)')
                        await conn.execute('CREATE INDEX IF NOT EXISTS wb_runs_order ON trigger_runs(task_id, created_at)')
                        await conn.execute('CREATE TABLE IF NOT EXISTS workbench_requests (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT, created_at REAL NOT NULL)')
                    self._ready = True
        return db

    def background(self, coro):
        task = asyncio.create_task(coro)
        self._background.add(task)
        def done(t):
            self._background.discard(t)
            if not t.cancelled(): t.exception()
        task.add_done_callback(done)
        return task

    async def handle(self, method, resource, data):
        navigation = resource == 'conversations' or resource.startswith('conversations/') or resource in {'tasks','tasks/cancel','skills','memories'} or resource.startswith(('skills/','memories/'))
        cid = None if navigation else data.get('conversation_id')
        if method != 'GET' and resource in {'memory/clear','tasks/create'} and not cid:
            raise WorkbenchError('缺少目标会话，请刷新后重试。', 409)
        from xgent_app.interaction import interaction
        try:
            with interaction('web', 'management'):
                async with conversation_operation(cid, expected=method != 'GET' and not navigation, fresh=True, selector='shared'):
                    return await self._handle_scoped(method, resource, data)
        except ConversationError as exc:
            raise WorkbenchError(str(exc), exc.status) from exc

    async def _handle_scoped(self, method, resource, data):
        await self.db()
        if method == 'GET':
            operations = {'conversations': self.conversations, 'conversations/delete_info': self.conversation_delete_info, 'conversations/search': self.search_conversations, 'bootstrap': self.bootstrap, 'history': self.history, 'search': self.search,
                          'tasks': self.tasks, 'artifacts': self.artifacts, 'providers': self.providers,
                          'skills': self.skills, 'memories': self.memories, 'usage': self.usage, 'usage/records': self.usage_records,
                          'artifacts/preview': self.artifact_preview, 'settings': self.read_settings}
        else:
            operations = {**{f'{kind}/{action}': self.document_action for kind in ('skills','memories') for action in ('create','update','rename','delete')}, **{f'conversations/{action}': self.conversation_action for action in ('create','switch','rename','archive','restore','reset_context','delete')},
                          'tasks/create': self.create_task, 'tasks/cancel': self.cancel_tasks,
                          'providers/save': self.save_provider, 'providers/delete': self.delete_provider,
                          'providers/fetch': self.fetch_models, 'providers/select': self.select_model,
                          'providers/import': self.import_providers, 'providers/export': self.export_providers,
                          'skills/state': self.skill_state, 'settings': self.settings,
                          'password': self.password, 'memory/clear': self.clear_memory, 'settings/web': self.web_settings}
        operation = operations.get(resource)
        if operation is None: raise WorkbenchError('工作台接口不存在', 404)
        try:
            if resource.startswith('conversations/'):
                data = {**data, 'action': resource.split('/', 1)[1]}
            if resource.startswith(('skills/','memories/')) and resource != 'skills/state':
                kind, action = resource.split('/',1)
                data = {**data, 'kind':kind, 'action':action}
            return await operation(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Credentials supplied in invalid forms must be redacted before persistence.
            for key in ('password', 'repeat', 'api_key'):
                if isinstance(data.get(key), str) and (key != 'repeat' or resource == 'password'):
                    self.ns['register_runtime_secret'](str(data.get(key) or ''))
            failure = await self.ns['GlobalRecorder'].record_error(exc, source='workbench')
            status = exc.status if isinstance(exc, (WorkbenchError, DocumentError)) else (400 if isinstance(exc, (ValueError, TypeError)) else 500)
            raise WorkbenchError(failure, status) from None

    async def bootstrap(self, data):
        await self.ns['UserDataManager'].init()
        await self.refresh_providers([])
        self.ns['_ensure_web_command_map']()
        db = await self.db()
        missing = object()
        for key in self.ns.get('WEB_EDITABLE_SETTINGS', set()):
            value = await db.get_config_fresh(key, missing)
            # Missing rows (including virtual settings such as chat_model/skill_state)
            # must not create None entries that suppress runtime getter defaults.
            # Explicit 0/False/empty collections are real settings, not missing values.
            if value is not missing and value is not None:
                self.ns['UserDataManager'].set(key, value)
        settings = await self.ns['_web_read_settings']()
        visible_names = self.ns.get('visible_command_names', lambda names: names)(self.ns['_WEB_COMMAND_MAP'])
        commands = [{'cmd': '/' + name, 'desc': self.ns['command_description'](name)}
                    for name in sorted(visible_names, key=lambda n: (n != 'start', n))]
        return {'settings': settings, 'commands': commands,
                'conversations': await self.conversations({}),
                'capabilities': {'tasks': True, 'providers': True, 'skills': True, 'usage': True, 'artifacts': True, 'shared_memory': True},
                'timezone': self.ns['SelfTriggerManager'].DEFAULT_TIMEZONE,
                'version': self.ns.get('RUNTIME_CODE_VERSION', self.ns.get('CODE_VERSION', '')),
                'web': await self.read_settings({}),
                'media_model': {'provider': self.ns['get_model_target_provider_name']('media'),
                                'model': self.ns['UserDataManager'].get('default_media_model', '')}}

    async def history(self, data):
        db = await self.db()
        conn = await db._get_conn()
        generation = await db.get_attachment_generation()
        limit = _integer(data.get('limit'))
        forward = bool(data.get('after'))
        token = data.get('after') or data.get('before')
        boundary = _unpack(token) if token else None
        reset = {'messages': [], 'ui_generation': generation, 'ui_tombstones': [],
                 'conversation_id': current_scope().conversation_id, 'reset': True, 'next_cursor': None, 'first_cursor': None, 'last_cursor': None}
        if boundary and boundary[3] != generation:
            return reset
        query = str(data.get('q') or '').strip()[:200]
        anchor = str(data.get('anchor') or '')
        if anchor:
            kind, _, key = anchor.partition(':')
            sql = ('SELECT timestamp FROM ui_messages WHERE ui_message_id=? AND conversation_id=?'
                   if kind == 'ui' else 'SELECT timestamp FROM global_messages WHERE id=? AND session_id=?')
            cur = await conn.execute(sql, (key, current_scope().conversation_id))
            row = await cur.fetchone()
            await cur.close()
            if row is None: raise WorkbenchError('消息已不存在', 404)
            boundary = [row['timestamp'], 1 if kind == 'ui' else 0,
                        (key if kind == 'ui' else f'{int(key):020d}') + '\uffff', generation]
            forward = False
        found, tombstones = [], []
        scanned = 0
        direction = 'ASC' if forward else 'DESC'
        op = '>' if forward else '<'
        # Limit each indexed source before merging. No printf/UNION sort across the full history.
        while len(found) <= limit and scanned < 10000:
            batch = []
            for source in (0, 1):
                table = 'ui_messages' if source else 'global_messages'
                key_column = 'ui_message_id' if source else 'id'
                text_column = 'payload' if source else 'content'
                where, params = (['conversation_id=?'], [current_scope().conversation_id]) if source else (["session_id=?", "msg_type<>?"], [current_scope().conversation_id, str(self.ns['MessageType'].AGENT_CMD)])
                if boundary:
                    ts, boundary_source, key, _ = boundary
                    if source == boundary_source:
                        if not source:
                            suffix = key.endswith('\uffff')
                            key = int(key.rstrip('\uffff'))
                            if suffix and not forward: key += 1
                        where.append(f'(timestamp,{key_column}) {op} (?,?)')
                        params.extend((ts, key))
                    else:
                        inclusive = (source > boundary_source) if forward else (source < boundary_source)
                        where.append(f'timestamp {op}{"=" if inclusive else ""} ?')
                        params.append(ts)
                if query:
                    where.append(f'instr(lower({text_column}),lower(?))>0')
                    params.append(query)
                cur = await conn.execute(f'SELECT * FROM {table} WHERE {" AND ".join(where)} '
                                         f'ORDER BY timestamp {direction}, {key_column} {direction} LIMIT 128', params)
                for item in await cur.fetchall():
                    row = dict(item)
                    row['_source'] = source
                    row['_key'] = row['ui_message_id'] if source else f"{row['id']:020d}"
                    batch.append(row)
                await cur.close()
            batch.sort(key=lambda r: (r['timestamp'], r['_source'], r['_key']), reverse=not forward)
            batch = batch[:128]  # Do not advance past unread rows from either individually bounded source.
            if not batch: break
            for row in batch:
                scanned += 1
                boundary = [row['timestamp'], row['_source'], row['_key'], generation]
                if row['_source']:
                    message = ui_record(row, active_generation=generation)
                    if message.pop('deleted', False):
                        tombstones.append({k: message[k] for k in ('ui_message_id','revision','ui_generation')})
                        continue
                else:
                    if hide_audit_record(row) or self.ns['is_redundant_agent_command_record'](row['msg_type'], row['content']): continue
                    message = await asyncio.to_thread(build_history_message, row, self.ns['ArtifactManager'].ROOT_DIR,
                                                      str(Path(self.ns['AgentExecutor'].WORK_DIR) / 'workspace'))
                message['history_key'] = 'ui:' + row['_key'] if row['_source'] else 'history:' + str(row['id'])
                message['history_cursor'] = _pack(boundary)
                found.append(message)
                if len(found) > limit: break
            if len(batch) < 128: break
        more = len(found) > limit or scanned >= 10000
        found = found[:limit]
        cursor = found[-1]['history_cursor'] if found and more else (_pack(boundary) if more else None)
        if not forward: found.reverse()
        if await db.get_attachment_generation() != generation: return reset
        return {'messages': found, 'next_cursor': cursor,
                'first_cursor': found[0]['history_cursor'] if found else None,
                'last_cursor': found[-1]['history_cursor'] if found else None,
                'conversation_id': current_scope().conversation_id,
                'ui_generation': generation, 'ui_tombstones': tombstones, 'busy': self.ns['_web_is_busy'](),
                'running': (await get_conversations().state()).get('running')}

    async def search(self, data):
        if not str(data.get('q') or '').strip(): return {'messages': [], 'next_cursor': None}
        return await self.history({**data, 'limit': min(_integer(data.get('limit')), 50)})

    async def tasks(self, data):
        db = await self.db(); conn = await db._get_conn(); task_id = str(data.get('id') or '')
        if task_id:
            task = await db.get_trigger_task(task_id)
            if task is None: raise WorkbenchError('任务不存在', 404)
            owner = await db.get_session(task['conversation_id'])
            if owner is None or owner.get('deleting'): raise WorkbenchError('任务不存在', 404)
            task['conversation_name'] = owner['name']
            task['conversation_archived'] = bool(owner['archived'])
            before = data.get('before')
            boundary = json.loads(before) if before else [time.time()+1, '\uffff']
            if not isinstance(boundary,list) or len(boundary)!=2: raise WorkbenchError('运行记录游标无效')
            cur = await conn.execute('SELECT * FROM trigger_runs WHERE task_id=? AND (created_at,run_id)<(?,?) ORDER BY created_at DESC,run_id DESC LIMIT 51', (task_id, *boundary))
            runs = [dict(row) for row in await cur.fetchall()]; await cur.close()
            return {'task': task, 'runs': runs[:50], 'next_cursor': json.dumps([runs[49]['created_at'],runs[49]['run_id']]) if len(runs)>50 else None}
        status = str(data.get('status') or '')
        kind = str(data.get('kind') or '')
        search = str(data.get('q') or '').strip()[:200]
        try:
            offset = max(0, int(data.get('offset') or 0))
        except (ValueError, TypeError, OverflowError):
            raise WorkbenchError('任务分页位置无效') from None
        source = str(data.get('source_conversation_id') or '')
        clauses, params = ["conversation_id IN (SELECT id FROM chat_sessions WHERE deleting=0)"], []
        if source and source != 'all':
            clauses.append('conversation_id=?'); params.append(source)
        if kind == 'condition':
            clauses.append("COALESCE(condition_expr, '') != ''")
        elif kind in ('cron', 'once', 'immediate'):
            clauses.append('schedule_type=?'); params.append(kind)
        elif kind:
            raise WorkbenchError('触发方式无效')
        if search:
            # Treat SQL wildcard characters as literal search text.
            pattern = '%' + search.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            clauses.append("(summary LIKE ? ESCAPE '!' OR command LIKE ? ESCAPE '!' OR id LIKE ? ESCAPE '!')")
            params.extend([pattern] * 3)
        count_clauses, count_params = list(clauses), list(params)
        if status:
            clauses.append('status=?'); params.append(status)
        condition = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
        cur = await conn.execute(f'SELECT COUNT(*) FROM trigger_tasks {condition}', params)
        total = (await cur.fetchone())[0]; await cur.close()
        # A saved page can become empty after cross-channel cancellation/filter changes.
        offset = min(offset, max(0, (total - 1) // 50 * 50))
        cur = await conn.execute(f'SELECT * FROM trigger_tasks {condition} ORDER BY created_at DESC, id DESC LIMIT 51 OFFSET ?', (*params, offset))
        rows = [dict(r) for r in await cur.fetchall()]; await cur.close()
        cur = await conn.execute('SELECT status, COUNT(*) AS count FROM trigger_tasks WHERE ' + ' AND '.join(count_clauses) + ' GROUP BY status', count_params)
        counts = {r['status']: r['count'] for r in await cur.fetchall()}; await cur.close()
        sources = await db.get_all_sessions()
        owners = {item['id']: item for item in sources}
        for task in rows:
            owner = owners.get(task['conversation_id'], {})
            task['conversation_name'] = owner.get('name', task['conversation_id'])
            task['conversation_archived'] = bool(owner.get('archived'))
        return {'sources': sources, 'source_conversation_id': source, 'items': rows[:50], 'offset': offset, 'total': total, 'counts': counts,
                'next_offset': offset+50 if len(rows)>50 else None,
                'timezone': self.ns['SelfTriggerManager'].DEFAULT_TIMEZONE}

    async def create_task(self, data):
        request_id = str(data.get('request_id') or '')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request_id): raise WorkbenchError('缺少有效的请求标识')
        fields = {key: data[key] for key in ('command','task','after','at','cron','when','repeat','timezone') if key in data}
        if not isinstance(fields.get('command'), str) or not fields['command'].strip(): raise WorkbenchError('命令不能为空')
        if 'repeat' in fields and not isinstance(fields['repeat'],bool): raise WorkbenchError('重复监控必须是布尔值')
        if any(not isinstance(v,str) for k,v in fields.items() if k!='repeat'): raise WorkbenchError('任务参数必须是文本')
        fingerprint = hashlib.sha256(json.dumps({'conversation_id': current_scope().conversation_id, **fields}, sort_keys=True).encode()).hexdigest(); db = await self.db()
        async with self._mutation_lock:
            async with db._transaction() as conn:
                cur = await conn.execute('SELECT * FROM workbench_requests WHERE request_id=?', (request_id,))
                previous = await cur.fetchone(); await cur.close()
                if previous:
                    if previous['fingerprint'] != fingerprint: raise WorkbenchError('请求标识已用于其他任务', 409)
                    if previous['result']: return json.loads(previous['result'])
                    raise WorkbenchError('此请求可能已执行，请先检查任务列表，不要重复创建', 409)
                await conn.execute('INSERT INTO workbench_requests VALUES (?,?,NULL,?)', (request_id,fingerprint,time.time()))
            async def perform():
                try:
                    result = await self.ns['SelfTriggerManager'].register_from_fields(**fields,
                        chat_id=self.ns['BotConfig'].AUTHORIZED_USER_ID, conversation_id=current_scope().conversation_id)
                    response = {'ok': True, 'message': result}
                except Exception as exc:
                    failure = await self.ns['GlobalRecorder'].record_error(exc, source='task_create')
                    response = {'ok': False, 'error': failure}
                async with db._write() as conn:
                    await conn.execute('UPDATE workbench_requests SET result=? WHERE request_id=?', (json.dumps(response),request_id))
                return response
            return await asyncio.shield(self.background(perform()))

    async def cancel_tasks(self, data):
        if data.get('confirm') is not True: raise WorkbenchError('取消任务需要确认')
        ids = data.get('ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100: raise WorkbenchError('请选择 1–100 个任务')
        db = await self.db()
        for task_id in ids:
            task = await db.get_trigger_task(str(task_id))
            if task is None:
                raise WorkbenchError('任务不存在', 404)
            owner = await db.get_session(task['conversation_id'])
            if owner is None or owner.get('deleting'):
                raise WorkbenchError('任务来源已不存在', 404)
        results = [await self.ns['SelfTriggerManager'].cancel(str(task_id)) for task_id in ids]
        return {'ok': True, 'messages': results}

    async def providers(self, data):
        db = await self.db(); db._providers_cache = None
        providers = await db.get_providers()
        return {'items': [{'name':name, **{k:v for k,v in value.items() if k!='api_key'}, 'has_key': bool(value.get('api_key'))} for name,value in providers.items()],
                'formats': sorted(self.ns['VALID_PROVIDER_API_FORMATS'])}

    async def refresh_providers(self, names):
        for name in names: self.ns['PortalManager'].remove_portal(name)
        db = await self.db()
        db._providers_cache = None
        await self.ns['UserDataManager'].reload_providers()
        for target in ('chat', 'media'):
            meta = self.ns['get_model_target_meta'](target)
            for field in ('provider', 'model'):
                value = await db.get_config_fresh(meta[field + '_config_key'])
                self.ns['UserDataManager'].set(meta[field + '_state_key'], value)

    def validate_provider(self, data, old=None):
        name = str(data.get('name') or '').strip()
        if not name or len(name)>100 or any(c in name for c in '|\r\n'): raise WorkbenchError('提供商名称不能为空或含分隔符')
        base, error = self.ns['validate_provider_base_url'](str(data.get('base_url') or ''))
        parsed = urlsplit(base)
        if parsed.username or parsed.password or any(k.lower() in {'key','api_key','apikey','token','access_token'} for k,v in parse_qsl(parsed.query)):
            raise WorkbenchError('不要在接口地址中嵌入凭据，请使用独立的 API Key 字段')
        fmt = str(data.get('api_format') or 'openai')
        if fmt not in self.ns['VALID_PROVIDER_API_FORMATS']: raise WorkbenchError('不支持的接口格式')
        models = data.get('models', (old or {}).get('models', []))
        if not isinstance(models,list) or not all(isinstance(m,str) and 0<len(m)<512 for m in models): raise WorkbenchError('模型列表格式错误')
        key = (old or {}).get('api_key','')
        if data.get('clear_key') is True: key = ''
        elif 'api_key' in data:
            if not isinstance(data['api_key'],str): raise WorkbenchError('密钥必须是文本')
            key = data['api_key'].strip()
        return name, base, key, list(dict.fromkeys(models)), fmt

    async def save_provider(self, data):
        async with self._mutation_lock:
            db = await self.db(); db._providers_cache = None; providers = await db.get_providers()
            old_name = str(data.get('original_name') or data.get('name') or '')
            name, base, key, models, fmt = self.validate_provider(data, providers.get(old_name))
            self.ns['register_runtime_secret'](key)
            if data.get('create') and name in providers: raise WorkbenchError('提供商已存在，请编辑现有配置')
            if old_name != name and name in providers: raise WorkbenchError('提供商名称已存在')
            async with db._transaction() as conn:
                if old_name != name and old_name in providers:
                    await conn.execute('DELETE FROM providers WHERE name=?',(old_name,))
                    for target in ('active_provider','default_media_provider'):
                        await conn.execute('UPDATE config SET value=? WHERE key=? AND value IN (?,?)',
                                           (json.dumps(name),target,json.dumps(old_name),old_name))
                await conn.execute('INSERT OR REPLACE INTO providers(name,base_url,api_key,models,api_format) VALUES(?,?,?,?,?)',
                                   (name,base,key,json.dumps(models),fmt))
            db._providers_cache = None
            await self.refresh_providers([old_name,name])
            for target in ('chat', 'media'):
                if self.ns['get_model_target_provider_name'](target) == name and self.ns['get_model_target_name'](target) not in models:
                    await self.ns['save_model_target_selection'](target, None, None)
                    if target == 'chat': await self.ns['sync_chat_session_model'](None)
        return {'ok':True, **await self.providers({})}

    async def delete_provider(self, data):
        if data.get('confirm') is not True: raise WorkbenchError('删除提供商需要确认')
        async with self._mutation_lock:
            await self.refresh_providers([])
            db = await self.db()
            name = str(data.get('name') or '')
            if name not in await db.get_providers(): raise WorkbenchError('提供商不存在', 404)
            targets = [t for t in ('chat','media') if self.ns['get_model_target_provider_name'](t) == name]
            await db.delete_provider(name)
            for target in targets:
                await self.ns['save_model_target_selection'](target, None, None)
                if target == 'chat': await self.ns['sync_chat_session_model'](None)
            await self.refresh_providers([name])
        return {'ok': True}

    async def fetch_models(self,data):
        db=await self.db(); providers=await db.get_providers(); name=str(data.get('name') or ''); provider=providers.get(name)
        if provider is None: raise WorkbenchError('提供商不存在',404)
        models,error=await self.ns['ModelClient'].fetch_knowledge_detailed(name,provider['api_key'],provider['base_url'],provider.get('api_format','openai'))
        if error: raise WorkbenchError(self.ns['redact_sensitive_text'](error),502)
        return {'models':models}

    async def select_model(self,data):
        target=data.get('target','chat'); name=str(data.get('provider') or ''); model=str(data.get('model') or '')
        if target not in ('chat','media'): raise WorkbenchError('无效模型用途')
        db=await self.db(); provider=(await db.get_providers()).get(name)
        if not provider or model not in provider['models']: raise WorkbenchError('模型或提供商不存在')
        await self.ns['UserDataManager'].reload_providers()
        await self.ns['save_model_target_selection'](target,name,model)
        if target=='chat': await self.ns['sync_chat_session_model'](model)
        return {'ok':True}

    async def export_providers(self,data):
        secret=data.get('include_secrets') is True
        if secret and data.get('confirm') is not True: raise WorkbenchError('导出密钥需要明确确认')
        db=await self.db(); providers=await db.get_providers()
        if not secret:
            for value in providers.values(): value.pop('api_key',None)
        return {'format':'xgent-workbench-providers','providers':providers,'contains_secrets':secret}

    async def import_providers(self,data):
        if data.get('confirm') is not True: raise WorkbenchError('导入配置需要确认')
        payload=data.get('config',{}); incoming=payload.get('providers') if isinstance(payload,dict) else None
        if not isinstance(incoming,dict) or not 1<=len(incoming)<=200: raise WorkbenchError('无效的提供商配置')
        db=await self.db(); old=await db.get_providers(); result={}
        for name,value in incoming.items():
            if not isinstance(value,dict): raise WorkbenchError('无效的提供商记录')
            n,base,key,models,fmt=self.validate_provider({**value,'name':name},old.get(name))
            self.ns['register_runtime_secret'](key)
            result[n]={'base_url':base,'api_key':key,'models':models,'api_format':fmt}
        await db.import_providers(result,replace=False); await self.refresh_providers(list(result))
        return {'ok':True,'count':len(result)}

    def _documents(self):
        return KnowledgeDocuments(self.ns)

    async def _refresh_skill_state(self):
        db = await self.db()
        snapshot = await read_knowledge_state(db)
        for key in ('disabled_skills','hidden_skills'):
            self.ns['UserDataManager'].set(key, snapshot[key])

    def _skill_record(self, record):
        return {**record, 'state': self.ns['get_skill_state'](record['path'])}

    async def skills(self, data):
        await self._refresh_skill_state()
        selected = str(data.get('path') or '')
        store = self._documents()
        if selected:
            return self._skill_record(await asyncio.to_thread(store.read, 'skills', selected))
        return {'items':[self._skill_record(item) for item in await asyncio.to_thread(store.list, 'skills')]}

    async def memories(self, data):
        store = self._documents()
        selected = str(data.get('path') or '')
        if selected:
            return await asyncio.to_thread(store.read, 'memories', selected)
        return {'items':await asyncio.to_thread(store.list, 'memories'), 'shared':True}

    async def document_action(self, data):
        db = await self.db()
        kind, action = data['kind'], data['action']
        store = self._documents()
        logical, revision = data.get('path'), data.get('revision')
        warning = None
        async with document_operation(db.db_path):
            if action == 'create':
                item = await asyncio.to_thread(store.create, kind)
                try:
                    if kind == 'skills':
                        await self.ns['save_skill_state'](item['path'], 'disabled')
                except Exception:
                    ticket = await asyncio.to_thread(store.stage_delete, kind, item['path'], item['revision'])
                    await asyncio.to_thread(store.finish_delete, ticket)
                    raise
            elif action == 'update':
                content = (compose_skill_text(data.get('summary'), data.get('body'))
                           if kind == 'skills' and data.get('mode') == 'sections' else data.get('content'))
                item = await asyncio.to_thread(store.write, kind, logical, revision, content)
            elif action == 'rename':
                _, old_file, _, new_logical = await asyncio.to_thread(store.rename_target, kind, logical, revision, data.get('name'))
                if new_logical == logical:
                    item = await asyncio.to_thread(store.read, kind, logical)
                else:
                    if kind == 'skills':
                        # Prime the destination state before exposing its filename.
                        await update_skill_references(db, logical, new_logical, keep_old=True)
                        await self._refresh_skill_state()
                    renamed = None
                    try:
                        renamed = await asyncio.to_thread(store.rename, kind, logical, revision, data.get('name'))
                        if kind == 'skills':
                            await update_skill_references(db, logical)
                        item = renamed
                    except Exception:
                        rolled_back = renamed is None
                        if renamed is not None:
                            try:
                                await asyncio.to_thread(store.rename, kind, renamed['path'], renamed['revision'], old_file.name)
                                rolled_back = True
                            except Exception:
                                pass  # Never overwrite an externally-created file to roll back.
                        if kind == 'skills' and rolled_back:
                            with contextlib.suppress(Exception):
                                await update_skill_references(db, new_logical)
                        raise
            elif action == 'delete':
                if data.get('confirm') is not True:
                    raise WorkbenchError('请确认删除这份文档。')
                ticket = await asyncio.to_thread(store.stage_delete, kind, logical, revision)
                try:
                    if kind == 'skills':
                        await update_skill_references(db, logical)
                except Exception:
                    await asyncio.to_thread(store.finish_delete, ticket, rollback=True)
                    raise
                try:
                    await asyncio.to_thread(store.finish_delete, ticket)
                except OSError:
                    warning = '文档已从列表移除，但临时文件清理失败；请检查文件权限。'
                item = None
            else:
                raise WorkbenchError('未知文档操作。')
            async with db._transaction() as conn:
                version = await bump_knowledge_revision(conn)
            await self._refresh_skill_state()
        notify = self.ns.get('_sync_knowledge_state')
        if notify is not None:
            await notify(db, force=True)
        return {'ok':True, 'item':self._skill_record(item) if item is not None and kind=='skills' else item,
                'knowledge_revision':version, 'warning':warning}

    async def skill_state(self,data):
        logical=str(data.get('path') or ''); state=data.get('state')
        if logical not in self.ns['list_skill_files']() or state not in ('enabled','disabled','hidden'):
            raise WorkbenchError('技能或状态无效')
        await self.ns['save_skill_state'](logical,state)
        return {'ok':True}

    async def usage(self, data):
        start, end = await self.usage_range(data)
        db = await self.db(); conn = await db._get_conn()
        pricing = await self.usage_pricing(start, end)
        models, prices, merge, clusters, auto = pricing
        selected = str(data.get('model') or '')
        conditions, params = ['ts>=?', 'ts<=?'], [start,end]
        if selected: conditions.append('model=?'); params.append(selected)
        zone = ZoneInfo(self.ns['SelfTriggerManager'].DEFAULT_TIMEZONE)
        monthly = end-start > 366*86400
        await conn.create_function('wb_day', 1, lambda ts: datetime.fromtimestamp(ts,zone).strftime('%Y-%m' if monthly else '%Y-%m-%d'))
        sql = ('SELECT model,wb_day(ts) day,count(*) count,sum(input_tokens) input, '
               'sum(output_tokens) output,sum(cached_tokens) cached,sum(reasoning_tokens) reasoning, '
               'sum(total_tokens) total FROM token_usage_stats WHERE ' + ' AND '.join(conditions) + ' GROUP BY model,wb_day(ts)')
        cur = await conn.execute(sql, params)
        grouped = [dict(r) for r in await cur.fetchall()]; await cur.close()
        keys = ('count','input','output','cached','reasoning','total')
        grand = {k:0 for k in keys}
        per_model, days = {}, {}
        missing = set()
        for row in grouped:
            model = self.ns['_resolve_name'](row['model'],merge,clusters,auto)
            price = self.ns['_model_price'](model,prices) if model in prices or 'default' in prices else None
            pm = per_model.setdefault(model, {'model':model, **{k:0 for k in keys}, 'cost':0.0,'cache_saved':0.0,'price':price,'members':[]})
            if row['model'] not in pm['members']: pm['members'].append(row['model'])
            day = days.setdefault(row['day'], {'day':row['day'], **{k:0 for k in keys},'cost':0.0})
            for key in keys:
                value = int(row[key] or 0);pm[key]+=value;grand[key]+=value;day[key]+=value
            if price is None:
                missing.add(model);pm['cost']=pm['cache_saved']=day['cost']=None
            else:
                cost = self.ns['_compute_cost'](row,price)
                pm['cost'] += cost
                pm['cache_saved'] += int(row['cached'] or 0)/1e6*max(0,price['input']-price['cached'])
                if day['cost'] is not None: day['cost'] += cost
        grand['cost'] = None if missing else sum(m['cost'] for m in per_model.values())
        grand['known_cost'] = sum(m['cost'] or 0 for m in per_model.values())
        grand['cache_saved'] = None if missing else sum(m['cache_saved'] for m in per_model.values())
        grand['average_cost'] = None if grand['cost'] is None else grand['cost']/grand['count'] if grand['count'] else 0
        for model in per_model.values():
            model['share'] = model['total']/grand['total'] if grand['total'] else 0
            model['average_cost'] = None if model['cost'] is None else model['cost']/model['count'] if model['count'] else 0
        details = await self.usage_records({'start':start,'end':end,'model':selected}, pricing=pricing)
        records = [{**r,'input':r['input_tokens'],'output':r['output_tokens'],'cached':r['cached_tokens'],
                    'reasoning':r['reasoning_tokens'],'total':r['total_tokens']} for r in details['items']]
        return {'summary':grand,'models':models,'per_model':sorted(per_model.values(),key=lambda m:m['total'],reverse=True),
                'days':sorted(days.values(),key=lambda r:r['day']), 'bucket':'month' if monthly else 'day',
                'missing_prices':sorted(missing),'records':records,'records_limit':50,
                'next_records_cursor':details['next_cursor'],'start':start,'end':end,
                'auto_merge':auto, 'timezone':self.ns['SelfTriggerManager'].DEFAULT_TIMEZONE}

    async def usage_pricing(self, start, end):
        db = await self.db(); conn = await db._get_conn()
        cur = await conn.execute('SELECT DISTINCT model FROM token_usage_stats WHERE ts>=? AND ts<=?', (start,end))
        models = sorted(row['model'] for row in await cur.fetchall()); await cur.close()
        for key in ('model_price_table','model_merge_map','stats_auto_merge'):
            self.ns['UserDataManager'].set(key, await db.get_config_fresh(key,self.ns['UserDataManager'].get(key)))
        prices = self.ns['_get_price_table'](); merge = self.ns['_get_merge_map']()
        auto = self.ns['UserDataManager'].get('stats_auto_merge',True)
        return models, prices, merge, self.ns['_build_clusters'](models) if auto else {}, auto

    async def settings(self, data):
        key, value = str(data.get('key') or ''), data.get('value')
        if key == 'model_price_table':
            if not isinstance(value, dict): raise WorkbenchError('价格表必须是对象')
            for price in value.values():
                if not isinstance(price, dict): raise WorkbenchError('无效价格记录')
                for field in ('input', 'output', 'cached'):
                    number = float(price.get(field, 0))
                    if not math.isfinite(number) or number < 0: raise WorkbenchError('价格必须是非负有限数值')
        if key == 'model_merge_map' and (not isinstance(value, dict) or any(not isinstance(v, list) or not all(isinstance(x,str) for x in v) for v in value.values())):
            raise WorkbenchError('模型合并规则格式不正确')
        if key in {'global_depth','stream_timeout','agent_command_timeout','agent_max_iterations','idle_message_interval','smart_match_threshold'}:
            number = float(value)
            if not math.isfinite(number) or number < 0 or int(number) != number:
                raise WorkbenchError('请输入非负整数')
        return await self.ns['_web_write_setting'](key, value)

    async def read_settings(self, data):
        db = await self.db()
        return {key: await db.get_config_fresh(key, default) for key, default in
                [('web_enabled', False), ('terminal_enabled', False), ('web_port', self.ns['DEFAULT_WEB_PORT']), ('web_public_url', '')]}

    async def web_settings(self, data):
        if data.get('confirm') is not True: raise WorkbenchError('修改 Web 服务设置需要确认')
        current = await self.read_settings({})
        values = {key: data.get(key, value) for key, value in current.items()}
        for key in ('web_enabled', 'terminal_enabled'):
            if not isinstance(values[key], bool): raise WorkbenchError('开关必须是布尔值')
        values['web_port'] = self.ns['parse_web_port'](str(values['web_port']))
        values['web_public_url'] = self.ns['normalize_web_public_url'](values['web_public_url'])
        if values['web_public_url']:
            url = urlsplit(values['web_public_url'])
            if not url.hostname or url.username or url.password: raise WorkbenchError('请输入有效的 HTTPS 公开地址')
        db = await self.db()
        async with db._transaction() as conn:
            for key, value in values.items():
                await conn.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,json.dumps(value)))
        for key,value in values.items(): self.ns['UserDataManager'].set(key,value)
        async def apply():
            await asyncio.sleep(.5)
            await self.ns['apply_web_config_change']()
        self.background(apply())
        return {'ok': True, 'settings': values, 'reconnect': current['web_port'] != values['web_port'] or not values['web_enabled']}

    async def artifact_preview(self, data):
        match = re.fullmatch(r'(\d+):(\d+)', str(data.get('id') or ''))
        if not match: raise WorkbenchError('无效附件标识')
        db = await self.db()
        record = await db.get_display_message(int(match[1]))
        if not record: raise WorkbenchError('附件记录已不存在', 404)
        message = await asyncio.to_thread(build_history_message, record, self.ns['ArtifactManager'].ROOT_DIR,
                                          str(Path(self.ns['AgentExecutor'].WORK_DIR) / 'workspace'))
        items = message.get('media', [])
        index = int(match[2])
        if index >= len(items) or items[index].get('error'): raise WorkbenchError('附件不存在或不可读取',404)
        item = items[index]
        path = Path(item['path'])
        mime = item.get('mime_type','')
        text_types = {'.txt','.md','.json','.csv','.tsv','.log','.py','.js','.ts','.css','.html','.xml','.yaml','.yml','.toml','.ini','.sh','.sql','.svg'}
        if not mime.startswith('text/') and path.suffix.lower() not in text_types:
            raise WorkbenchError('此文件类型请下载或使用媒体预览', 415)
        offset = int(data.get('offset') or 0)
        if offset < 0: raise WorkbenchError('无效分页位置')
        def read():
            import codecs
            import os
            import stat
            fd = os.open(path, os.O_RDONLY | getattr(os,'O_NONBLOCK',0) | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_BINARY',0))
            with os.fdopen(fd,'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or offset > info.st_size: raise WorkbenchError('文件已变化，请重新打开')
                handle.seek(offset)
                raw = handle.read(65536)
                eof = offset + len(raw) >= info.st_size
                if b'\x00' in raw: raise WorkbenchError('检测到二进制内容，请下载查看',415)
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                text = decoder.decode(raw, final=eof)
                return {'text':text, 'offset':offset,'next_offset':offset+len(raw)-len(decoder.getstate()[0]),
                        'eof':eof,'size':info.st_size,'filename':item['filename'],'encoding':'UTF-8'}
        try: return await asyncio.to_thread(read)
        except OSError: raise WorkbenchError('附件不存在或不可读取',404) from None

    async def usage_records(self, data, *, pricing=None):
        start, end = await self.usage_range(data)
        db = await self.db(); conn = await db._get_conn()
        conditions = ['ts>=?', 'ts<=?']; params = [start,end]
        if data.get('model'): conditions.append('model=?'); params.append(str(data['model']))
        if data.get('before'):
            try:
                cursor = json.loads(data['before'])
                if (not isinstance(cursor,list) or len(cursor)!=2 or type(cursor[1]) is not int
                        or not math.isfinite(float(cursor[0]))): raise ValueError()
            except (ValueError,TypeError,OverflowError): raise WorkbenchError('无效用量游标') from None
            conditions.append('(ts,id)<(?,?)'); params.extend(cursor)
        cur = await conn.execute('SELECT * FROM token_usage_stats WHERE '+ ' AND '.join(conditions) + ' ORDER BY ts DESC,id DESC LIMIT 51', params)
        rows = [dict(r) for r in await cur.fetchall()]; await cur.close()
        _, prices, merge, clusters, auto = pricing or await self.usage_pricing(start,end)
        for row in rows:
            model = self.ns['_resolve_name'](row['model'],merge,clusters,auto)
            row['display_model'] = model
            rec = {'input':row['input_tokens'],'output':row['output_tokens'],'cached':row['cached_tokens']}
            row['cost'] = self.ns['_compute_cost'](rec,self.ns['_model_price'](model,prices)) if model in prices or 'default' in prices else None
        return {'items':rows[:50], 'next_cursor':json.dumps([rows[49]['ts'],rows[49]['id']]) if len(rows)>50 else None}

    async def usage_range(self, data):
        zone = ZoneInfo(self.ns['SelfTriggerManager'].DEFAULT_TIMEZONE)
        now = time.time(); period = str(data.get('range') or '')
        try:
            if period == 'all':
                db = await self.db(); conn = await db._get_conn()
                cur = await conn.execute('SELECT MIN(ts),MAX(ts) FROM token_usage_stats')
                first,last = await cur.fetchone(); await cur.close()
                return float(first if first is not None else now), max(now,float(last or now))
            if period in ('7','30','90'):
                return now-int(period)*86400,now
            if period == 'custom':
                start_day = datetime.strptime(str(data.get('start_date') or ''),'%Y-%m-%d').replace(tzinfo=zone)
                end_day = datetime.strptime(str(data.get('end_date') or ''),'%Y-%m-%d').replace(tzinfo=zone)
                start,end = start_day.timestamp(),(end_day+timedelta(days=1)).timestamp()-0.000001
            elif period:
                raise ValueError()
            else:
                end = float(data['end']) if data.get('end') not in (None,'') else now
                start = float(data['start']) if data.get('start') not in (None,'') else end-7*86400
            if not all(math.isfinite(x) for x in (start,end)) or start<0 or start>end:
                raise ValueError()
        except (ValueError,TypeError,OverflowError):
            raise WorkbenchError('请选择有效的起止日期，开始日期不能晚于结束日期') from None
        return start,end

    async def password(self,data):
        if data.get('confirm') is not True: raise WorkbenchError('修改密码会撤销已有登录，需要确认')
        await self.ns['persist_web_password'](data.get('password',''))
        async def apply():
            await asyncio.sleep(.5); await self.ns['apply_web_config_change']()
        self.background(apply()); return {'ok':True,'requires_login':True}

    async def clear_memory(self, data):
        if data.get('confirm') not in {'清空当前会话', '重置上下文'}:
            raise WorkbenchError('请输入“重置上下文”确认；历史保留，其他会话不受影响。')
        result = await self.ns['clear_current_conversation']()
        outbox = self.ns['get_web_outbox']()
        if outbox is not None:
            outbox.put(stamp_frame({'type': 'context_reset'}))
        return {'ok': True, 'result': result}

    async def artifacts(self,data):
        kind=data.get('kind','files'); q=str(data.get('q') or '').lower()[:200]
        if kind=='outputs':
            root=Path(self.ns['COMMAND_OUTPUT_DIR']).resolve(); after=str(data.get('after') or '')
            def scan():
                import heapq
                def candidates():
                    if not root.exists(): return
                    for directory in root.iterdir():
                        if directory.is_symlink() or not directory.is_dir(): continue
                        for path in directory.iterdir():
                            key=path.relative_to(root).as_posix()
                            if path.suffix=='.txt' and path.is_file() and not path.is_symlink() and (not after or key<after) and q in key.lower(): yield key,path
                chosen=heapq.nlargest(51,candidates(),key=lambda item:item[0]); rows=[]
                for key,path in chosen[:50]:
                    try:
                        info=path.stat(); rows.append({'id':key,'filename':path.name,'path':str(path),'kind':'output','size':info.st_size,'timestamp':info.st_mtime})
                    except OSError: continue
                return {'items':rows,'next_cursor':chosen[49][0] if len(chosen)>50 else None}
            return await asyncio.to_thread(scan)
        db=await self.db(); conn=await db._get_conn(); before=int(data.get('before') or 2**63-1)
        search_sql = " AND (instr(lower(content),?)>0 OR instr(lower(COALESCE(metadata,'')),?)>0 OR instr(lower(COALESCE(metadata,'')),?)>0)" if q else ''
        params = (current_scope().conversation_id,before,q,q,json.dumps(q,ensure_ascii=True)[1:-1]) if q else (current_scope().conversation_id,before,)
        cur=await conn.execute("SELECT * FROM global_messages WHERE session_id=? AND id<? AND (metadata LIKE '%attachments%' OR metadata LIKE '%display_media%' OR msg_type IN ('user_file','user_photo','media_reply'))" + search_sql + " ORDER BY id DESC LIMIT 51",params)
        rows=[dict(r) for r in await cur.fetchall()]; await cur.close(); items=[]
        for row in rows[:50]:
            message=await asyncio.to_thread(build_history_message,row,self.ns['ArtifactManager'].ROOT_DIR,str(Path(self.ns['AgentExecutor'].WORK_DIR)/'workspace'))
            for index,item in enumerate(message.get('media',[])):
                if q and q not in str(item.get('filename','')).lower(): continue
                if data.get('media_type') and item.get('kind') != data['media_type']: continue
                items.append({**item,'id':f"{row['id']}:{index}",'source_id':row['id'],'timestamp':row['timestamp']})
            if message.get('media_error'): items.append({'id':str(row['id']),'filename':'附件记录','error':message['media_error'],'source_id':row['id']})
        return {'items':items,'next_cursor':rows[49]['id'] if len(rows)>50 else None}


    async def conversations(self, data):
        db = await self.db()
        return {**await get_conversations().state(), 'items': await db.get_all_sessions()}

    async def conversation_action(self, data):
        if data['action'] != 'create' and not data.get('id'):
            raise WorkbenchError('缺少目标会话。', 409)
        action = data['action']
        result = {}
        if action in {'reset_context','delete'}:
            if data.get('confirm') is not True:
                raise WorkbenchError('请确认目标会话后再操作。')
            if action == 'delete':
                result = await self.ns['delete_conversation'](str(data['id']))
            else:
                await self.ns['reset_conversation_context'](str(data['id']))
        else:
            await get_conversations().manage(action, data.get('id'), data.get('name'))
        return {**await self.conversations({}), **result}

    async def conversation_delete_info(self, data):
        db = await self.db()
        return await db.conversation_delete_info(str(data.get('id') or ''))

    async def search_conversations(self, data):
        query = str(data.get('q') or '').strip()[:200]
        db = await self.db(); conn = await db._get_conn()
        # Search is a management read, never a cross-conversation model context.
        sql = """SELECT s.id,s.name,s.archived,s.last_active,
            (SELECT substr(m.content, max(1,instr(lower(m.content),lower(?))-40),180)
             FROM global_messages m WHERE m.session_id=s.id AND ?<>''
             AND m.msg_type IN ('user_text','ai_reply','user_file','user_photo','media_reply')
             AND instr(lower(m.content),lower(?))>0 ORDER BY m.timestamp DESC,m.id DESC LIMIT 1) snippet
            FROM chat_sessions s WHERE s.deleting=0 AND (?='' OR instr(lower(s.name),lower(?))>0 OR EXISTS
              (SELECT 1 FROM global_messages m WHERE m.session_id=s.id
               AND m.msg_type IN ('user_text','ai_reply','user_file','user_photo','media_reply')
               AND instr(lower(m.content),lower(?))>0))
            ORDER BY s.last_active DESC,s.id LIMIT 30"""
        cursor = await conn.execute(sql, (query,)*6)
        items = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        for item in items:
            item['snippet'] = ' '.join((item['snippet'] or '').split())
        return {'items':items}
