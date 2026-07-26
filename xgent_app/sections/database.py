# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

import inspect

class BotMemoryDB:
    """Bot的永久记忆系统 - 异步SQLite + 连接池"""
    
    _instance = None
    _lock = None  # 延迟创建，避免事件循环问题
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None
        self._connection_lock = asyncio.Lock()
        self._initialized = False
        # 内存缓存
        self._config_cache: Dict[str, Any] = {}
        self._providers_cache: Optional[Dict] = None
        self._cache_dirty = False
    
    @classmethod
    async def get_instance(cls) -> 'BotMemoryDB':
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        async with cls._lock:
            if cls._instance is None:
                cls._instance = BotMemoryDB(BotConfig.DB_FILE)
                await cls._instance._init_db()
            return cls._instance
    
    async def _get_conn(self) -> aiosqlite.Connection:
        # 热路径直接复用连接，避免每次真实 SQL 前额外执行 SELECT 1。
        connection = self._connection
        if connection is not None:
            return connection

        async with self._connection_lock:
            connection = self._connection
            if connection is not None:
                return connection

            connection = await aiosqlite.connect(
                self.db_path,
                isolation_level=None  # 自动提交
            )
            try:
                connection.row_factory = aiosqlite.Row
                # WAL 提升读写并发；busy_timeout 避免短暂写锁直接报错。
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute("PRAGMA synchronous=NORMAL")
                await connection.execute("PRAGMA cache_size=10000")
                await connection.execute("PRAGMA busy_timeout=5000")
            except Exception:
                await connection.close()
                raise

            self._connection = connection
            return connection
    
    async def _init_db(self):
        """初始化数据库表结构"""
        if self._initialized:
            return
            
        conn = await self._get_conn()
        
        # 全局消息记录表（用于全知模式）- 记录所有操作
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS global_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                msg_type TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                session_id TEXT,
                metadata TEXT
            )
        ''')
        
        # 内部兼容索引表（当前单一全局记忆模式下仅保留一条固定记录）
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                name TEXT,
                model TEXT,
                last_active REAL,
                created_at REAL
            )
        ''')
        
        # 内部兼容镜像表（只镜像纯 user/assistant 对话）
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
            )
        ''')
        
        # 配置表
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
        # Provider表
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS providers (
                name TEXT PRIMARY KEY,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                models TEXT DEFAULT '[]',
                api_format TEXT DEFAULT 'openai'
            )
        ''')
        
        # 自动迁移：给旧表加 api_format 列
        try:
            await conn.execute('ALTER TABLE providers ADD COLUMN api_format TEXT DEFAULT "openai"')
        except Exception:
            pass  # 列已存在
        
        # 未授权用户记录表
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS unauthorized_access_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                action_type TEXT NOT NULL,
                content TEXT NOT NULL,
                bot_reply TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS trigger_tasks (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                command TEXT NOT NULL,
                summary TEXT,
                schedule_type TEXT NOT NULL,
                schedule_expr TEXT,
                timezone TEXT NOT NULL,
                next_run_at REAL,
                condition_expr TEXT,
                repeat INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                origin_user_text TEXT,
                origin_assistant_text TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_started_at REAL,
                last_finished_at REAL,
                fire_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                recovery_count INTEGER NOT NULL DEFAULT 0,
                last_result_hash TEXT,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                backoff_seconds REAL NOT NULL DEFAULT 0,
                backoff_until REAL,
                last_error TEXT
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS trigger_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                scheduled_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                status TEXT NOT NULL,
                trigger_reason TEXT,
                matched_conditions TEXT,
                exit_code INTEGER,
                output TEXT,
                output_path TEXT,
                error TEXT,
                notice_started_at REAL,
                notice_sent_at REAL,
                delivery_started_at REAL,
                delivered_at REAL,
                created_at REAL NOT NULL,
                UNIQUE(task_id, scheduled_at),
                FOREIGN KEY (task_id) REFERENCES trigger_tasks(id)
            )
        ''')

        try:
            await conn.execute('ALTER TABLE trigger_tasks ADD COLUMN summary TEXT')
        except Exception:
            pass
        for migration in (
            'ALTER TABLE trigger_tasks ADD COLUMN last_result_hash TEXT',
            'ALTER TABLE trigger_tasks ADD COLUMN duplicate_count INTEGER NOT NULL DEFAULT 0',
            'ALTER TABLE trigger_tasks ADD COLUMN backoff_seconds REAL NOT NULL DEFAULT 0',
            'ALTER TABLE trigger_tasks ADD COLUMN backoff_until REAL',
        ):
            try:
                await conn.execute(migration)
            except Exception:
                pass
        for migration in (
            'ALTER TABLE trigger_runs ADD COLUMN notice_started_at REAL',
            'ALTER TABLE trigger_runs ADD COLUMN notice_sent_at REAL',
            'ALTER TABLE trigger_runs ADD COLUMN delivery_started_at REAL',
        ):
            try:
                await conn.execute(migration)
            except Exception:
                pass
        
        # 索引优化
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_global_timestamp ON global_messages(timestamp)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_global_session ON global_messages(session_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_global_type ON global_messages(msg_type)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_messages_time ON chat_messages(timestamp)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_unauthorized_access_timestamp ON unauthorized_access_logs(timestamp)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_trigger_tasks_status ON trigger_tasks(status)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_trigger_tasks_next_run ON trigger_tasks(next_run_at)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_trigger_runs_delivery ON trigger_runs(status, delivered_at)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_trigger_runs_task ON trigger_runs(task_id, created_at)')
        
        self._initialized = True
        logger.info("📚 系统记忆数据库初始化完成")
    
    # --- 全局消息记录（仅全局模式使用）---
    async def record_global_message(self, chat_id: int, user_id: int, msg_type: str,
                                     role: str, content: str, session_id: Optional[str] = None,
                                     metadata: Optional[Dict[str, Any]] = None):
        """记录一条全局消息"""
        conn = await self._get_conn()
        await conn.execute('''
            INSERT INTO global_messages (chat_id, user_id, msg_type, role, content, timestamp, session_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (chat_id, user_id, msg_type, role, content, time.time(), session_id, 
              json.dumps(metadata) if metadata else None))
    
    async def get_global_messages(self, limit: int = 50,
                                   include_types: Optional[List[str]] = None) -> List[Dict]:
        """获取全局消息"""
        conn = await self._get_conn()
        
        if include_types:
            placeholders = ','.join('?' * len(include_types))
            query = f'''
                SELECT msg_type, role, content, timestamp, session_id, metadata 
                FROM global_messages
                WHERE msg_type IN ({placeholders})
                ORDER BY timestamp DESC LIMIT ?
            '''
            cursor = await conn.execute(query, (*include_types, limit))
        else:
            cursor = await conn.execute('''
                SELECT msg_type, role, content, timestamp, session_id, metadata 
                FROM global_messages
                ORDER BY timestamp DESC LIMIT ?
            ''', (limit,))
        
        rows = await cursor.fetchall()
        messages = [dict(row) for row in reversed(rows)]
        return [
            msg for msg in messages
            if not is_redundant_agent_command_record(msg.get('msg_type'), msg.get('content'))
        ]
    
    async def get_conversation_messages(self, limit: int = 50) -> List[Dict]:
        """获取所有消息（用于AI上下文）- 包含对话和系统操作"""
        conn = await self._get_conn()
        cursor = await conn.execute('''
            SELECT role, content, timestamp, msg_type FROM global_messages
            ORDER BY timestamp DESC LIMIT ?
        ''', (limit,))
        rows = await cursor.fetchall()
        
        # 转换格式，系统操作转为 user 角色以便 AI 理解
        result = []
        for row in reversed(rows):
            msg = dict(row)
            msg_type = msg.get('msg_type')
            if is_redundant_agent_command_record(msg_type, msg.get('content')):
                continue
            
            # 系统操作以 system 角色注入（OpenAI 格式原样支持；Gemini/Claude 在各自构建器里降级为 user），
            # 避免系统旁白伪装成用户消息、破坏对话轮换结构
            if msg_type == MessageType.SYSTEM_OP:
                result.append({
                    'role': 'system',
                    'content': f"[系统操作] {msg['content']}"
                })
            elif msg_type == MessageType.BUTTON_CLICK:
                result.append({
                    'role': 'user', 
                    'content': f"[操作] {msg['content']}"
                })
            elif msg_type == MessageType.AGENT_CMD:
                result.append({
                    'role': 'user',
                    'content': f"[Agent执行] {msg['content']}"
                })
            elif msg_type == MessageType.AGENT_RESULT:
                result.append({
                    'role': 'user',
                    'content': f"[系统结果] {msg['content']}"
                })
            elif msg_type == MessageType.MEDIA_REPLY:
                result.append({
                    'role': 'user',
                    'content': f"[外部媒体模块回复] {msg['content']}"
                })
            else:
                result.append({
                    'role': msg['role'],
                    'content': msg['content']
                })
        
        return result
    
    async def get_last_user_message_time(self) -> Optional[float]:
        """获取用户最后一次发消息的时间"""
        conn = await self._get_conn()
        cursor = await conn.execute('''
            SELECT MAX(timestamp) as last_time FROM global_messages
            WHERE role = 'user'
        ''')
        row = await cursor.fetchone()
        return row['last_time'] if row and row['last_time'] else None
    
    # --- 会话管理 ---
    async def create_session(self, session_id: str, model: Optional[str] = None) -> str:
        """创建新会话"""
        conn = await self._get_conn()
        now = time.time()
        await conn.execute('''
            INSERT OR REPLACE INTO chat_sessions (id, name, model, last_active, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (session_id, None, model, now, now))
        return session_id
    
    async def get_session(self, session_id: str) -> Optional[Dict]:
        """获取会话信息"""
        conn = await self._get_conn()
        cursor = await conn.execute('SELECT * FROM chat_sessions WHERE id = ?', (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    
    _ALLOWED_SESSION_COLUMNS = {'name', 'model', 'last_active', 'created_at'}

    async def update_session(self, session_id: str, **kwargs):
        """更新会话信息"""
        conn = await self._get_conn()
        for key, value in kwargs.items():
            if key not in self._ALLOWED_SESSION_COLUMNS:
                logger.warning(f"尝试更新非法列名: {key}，已拒绝")
                continue
            await conn.execute(f'UPDATE chat_sessions SET {key} = ? WHERE id = ?', (value, session_id))
    
    async def get_all_sessions(self) -> List[Dict]:
        """获取所有会话"""
        conn = await self._get_conn()
        cursor = await conn.execute('SELECT * FROM chat_sessions ORDER BY last_active DESC')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
    async def delete_session(self, session_id: str, delete_global_messages: bool = False) -> int:
        """删除会话及其消息，可选同时删除关联的全局记忆"""
        conn = await self._get_conn()
        deleted_global_messages = 0
        if delete_global_messages:
            cursor = await conn.execute(
                'SELECT COUNT(*) AS count FROM global_messages WHERE session_id = ?',
                (session_id,)
            )
            row = await cursor.fetchone()
            deleted_global_messages = int(row['count']) if row and row['count'] is not None else 0
            await conn.execute('DELETE FROM global_messages WHERE session_id = ?', (session_id,))
        await conn.execute('DELETE FROM chat_messages WHERE session_id = ?', (session_id,))
        await conn.execute('DELETE FROM chat_sessions WHERE id = ?', (session_id,))
        return deleted_global_messages

    async def clear_all_conversation_memory(self) -> Dict[str, int]:
        """清空对话相关记忆，保留 providers、config、prompts 等配置"""
        conn = await self._get_conn()

        async def _count(table_name: str) -> int:
            cursor = await conn.execute(f'SELECT COUNT(*) AS count FROM {table_name}')
            row = await cursor.fetchone()
            return int(row['count']) if row and row['count'] is not None else 0

        counts = {
            'global_messages': await _count('global_messages'),
            'chat_messages': await _count('chat_messages'),
            'chat_sessions': await _count('chat_sessions'),
        }

        await conn.execute('DELETE FROM global_messages')
        await conn.execute('DELETE FROM chat_messages')
        await conn.execute('DELETE FROM chat_sessions')
        return counts
    
    # --- 内部兼容镜像消息 ---
    async def add_chat_message(self, session_id: str, role: str, content: str):
        """添加消息到内部兼容镜像"""
        conn = await self._get_conn()
        await conn.execute('''
            INSERT INTO chat_messages (session_id, role, content, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (session_id, role, content, time.time()))
        await conn.execute('UPDATE chat_sessions SET last_active = ? WHERE id = ?', 
                          (time.time(), session_id))
    
    async def get_chat_messages(self, session_id: str, limit: Optional[int] = None) -> List[Dict]:
        """获取内部兼容镜像消息"""
        conn = await self._get_conn()
        if limit:
            cursor = await conn.execute('''
                SELECT role, content FROM chat_messages 
                WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?
            ''', (session_id, limit))
            rows = await cursor.fetchall()
            return [dict(row) for row in reversed(rows)]
        else:
            cursor = await conn.execute('''
                SELECT role, content FROM chat_messages 
                WHERE session_id = ? ORDER BY timestamp ASC
            ''', (session_id,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def remove_last_chat_message(self, session_id: str):
        """移除最后一条消息"""
        conn = await self._get_conn()
        await conn.execute('''
            DELETE FROM chat_messages WHERE id = (
                SELECT id FROM chat_messages WHERE session_id = ? 
                ORDER BY timestamp DESC LIMIT 1
            )
        ''', (session_id,))
    
    # --- 配置管理（带缓存）---
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置"""
        if key in self._config_cache:
            return self._config_cache[key]
        
        conn = await self._get_conn()
        cursor = await conn.execute('SELECT value FROM config WHERE key = ?', (key,))
        row = await cursor.fetchone()
        if row:
            try:
                value = json.loads(row['value'])
            except (json.JSONDecodeError, TypeError):
                value = row['value']
            self._config_cache[key] = value
            return value
        return default
    
    async def set_config(self, key: str, value: Any):
        """设置配置"""
        self._config_cache[key] = value
        conn = await self._get_conn()
        json_value = json.dumps(value)
        await conn.execute('''
            INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)
        ''', (key, json_value))
    
    # --- Provider管理（带缓存）---
    async def get_providers(self) -> Dict[str, Dict]:
        """获取所有Provider"""
        if self._providers_cache is not None:
            return self._providers_cache
        
        conn = await self._get_conn()
        cursor = await conn.execute('SELECT * FROM providers')
        result = {}
        async for row in cursor:
            result[row['name']] = {
                'base_url': row['base_url'],
                'api_key': row['api_key'],
                'models': json.loads(row['models']),
                'api_format': row['api_format'] if 'api_format' in row.keys() else 'openai'
            }
        self._providers_cache = result
        return result
    
    async def save_provider(self, name: str, base_url: str, api_key: str,
                            models: Optional[List[str]] = None, api_format: str = 'openai'):
        """保存Provider"""
        if api_format not in VALID_PROVIDER_API_FORMATS:
            raise ValueError(f"无效的 api_format: {api_format}，支持的格式: {', '.join(sorted(VALID_PROVIDER_API_FORMATS))}")
        conn = await self._get_conn()
        await conn.execute('''
            INSERT OR REPLACE INTO providers (name, base_url, api_key, models, api_format)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, base_url, api_key, json.dumps(models or []), api_format))
        # 清除缓存
        self._providers_cache = None
    
    async def import_providers(self, providers: Dict[str, Dict[str, Any]], replace: bool = False):
        """在单个事务中批量导入 Provider；可合并或先清空后覆盖。"""
        conn = await self._get_conn()
        await conn.execute('BEGIN IMMEDIATE')
        try:
            if replace:
                await conn.execute('DELETE FROM providers')
            for name, provider in providers.items():
                await conn.execute('''
                    INSERT OR REPLACE INTO providers (name, base_url, api_key, models, api_format)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    name,
                    provider['base_url'],
                    provider['api_key'],
                    json.dumps(provider.get('models', []), ensure_ascii=False),
                    provider.get('api_format', 'openai')
                ))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        self._providers_cache = None

    async def rename_provider(self, old_name: str, new_name: str):
        """原子重命名 Provider，并同步关联的默认模型提供商配置。"""
        if old_name == new_name:
            return
        conn = await self._get_conn()
        await conn.execute('BEGIN IMMEDIATE')
        try:
            cursor = await conn.execute('SELECT 1 FROM providers WHERE name = ?', (old_name,))
            if not await cursor.fetchone():
                raise ValueError(f'提供商不存在：{old_name}')
            cursor = await conn.execute('SELECT 1 FROM providers WHERE name = ?', (new_name,))
            if await cursor.fetchone():
                raise ValueError(f'提供商名称已存在：{new_name}')

            await conn.execute('UPDATE providers SET name = ? WHERE name = ?', (new_name, old_name))
            old_json = json.dumps(old_name)
            new_json = json.dumps(new_name)
            await conn.execute(
                "UPDATE config SET value = ? WHERE key = 'active_provider' AND value IN (?, ?)",
                (new_json, old_json, old_name)
            )
            await conn.execute(
                "UPDATE config SET value = ? WHERE key = 'default_media_provider' AND value IN (?, ?)",
                (new_json, old_json, old_name)
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

        self._providers_cache = None
        if self._config_cache.get('active_provider') == old_name:
            self._config_cache['active_provider'] = new_name
        if self._config_cache.get('default_media_provider') == old_name:
            self._config_cache['default_media_provider'] = new_name

    async def delete_provider(self, name: str):
        """删除Provider"""
        conn = await self._get_conn()
        await conn.execute('DELETE FROM providers WHERE name = ?', (name,))
        self._providers_cache = None
    
    async def update_provider_models(self, name: str, models: List[str]):
        """更新Provider的模型列表"""
        conn = await self._get_conn()
        await conn.execute('UPDATE providers SET models = ? WHERE name = ?', 
                          (json.dumps(models), name))
        self._providers_cache = None
    
    # --- 未授权用户记录 ---
    async def record_unauthorized_access(self, user_id: int, username: Optional[str], full_name: Optional[str],
                               action_type: str, content: str, bot_reply: str):
        """记录未授权用户入侵"""
        conn = await self._get_conn()
        await conn.execute('''
            INSERT INTO unauthorized_access_logs (user_id, username, full_name, action_type, content, bot_reply, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, username, full_name, action_type, content, bot_reply, time.time()))
    
    async def get_unauthorized_access_logs(self, limit: int = 500) -> List[Dict]:
        """获取未授权用户记录"""
        conn = await self._get_conn()
        cursor = await conn.execute('''
            SELECT user_id, username, full_name, action_type, content, bot_reply, timestamp
            FROM unauthorized_access_logs ORDER BY timestamp DESC LIMIT ?
        ''', (limit,))
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    # --- 持久化后台触发任务 ---
    async def create_trigger_task(self, task: Dict[str, Any]):
        conn = await self._get_conn()
        await conn.execute('''
            INSERT INTO trigger_tasks (
                id, chat_id, conversation_id, command, summary, schedule_type, schedule_expr,
                timezone, next_run_at, condition_expr, repeat, status,
                origin_user_text, origin_assistant_text, created_at, updated_at,
                last_started_at, last_finished_at, fire_count, failure_count,
                recovery_count, last_result_hash, duplicate_count, backoff_seconds,
                backoff_until, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task['id'], task['chat_id'], task['conversation_id'], task['command'],
            task.get('summary'), task['schedule_type'], task.get('schedule_expr'), task['timezone'],
            task.get('next_run_at'), task.get('condition_expr'), int(bool(task.get('repeat'))),
            task['status'], task.get('origin_user_text'), task.get('origin_assistant_text'),
            task['created_at'], task['updated_at'], task.get('last_started_at'),
            task.get('last_finished_at'), int(task.get('fire_count', 0)),
            int(task.get('failure_count', 0)), int(task.get('recovery_count', 0)),
            task.get('last_result_hash'), int(task.get('duplicate_count', 0)),
            float(task.get('backoff_seconds', 0) or 0), task.get('backoff_until'),
            task.get('last_error'),
        ))

    async def get_trigger_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute('SELECT * FROM trigger_tasks WHERE id = ?', (task_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_trigger_tasks(self, active_only: bool = True) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        if active_only:
            cursor = await conn.execute('''
                SELECT * FROM trigger_tasks
                WHERE status NOT IN ('completed', 'cancelled', 'failed')
                ORDER BY created_at ASC
            ''')
        else:
            cursor = await conn.execute('SELECT * FROM trigger_tasks ORDER BY created_at ASC')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_trigger_task(self, task_id: str, **fields: Any):
        allowed = {
            'schedule_expr', 'next_run_at', 'status', 'updated_at', 'last_started_at',
            'last_finished_at', 'fire_count', 'failure_count', 'recovery_count', 'last_error',
            'last_result_hash', 'duplicate_count', 'backoff_seconds', 'backoff_until',
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates.setdefault('updated_at', time.time())
        assignments = ', '.join(f'{key} = ?' for key in updates)
        conn = await self._get_conn()
        await conn.execute(
            f'UPDATE trigger_tasks SET {assignments} WHERE id = ?',
            (*updates.values(), task_id),
        )

    async def cancel_trigger_tasks(self, task_id: Optional[str] = None) -> int:
        conn = await self._get_conn()
        now = time.time()
        if task_id is None:
            cursor = await conn.execute('''
                UPDATE trigger_tasks
                SET status = 'cancelled', next_run_at = NULL, updated_at = ?
                WHERE status NOT IN ('completed', 'cancelled', 'failed')
            ''', (now,))
        else:
            cursor = await conn.execute('''
                UPDATE trigger_tasks
                SET status = 'cancelled', next_run_at = NULL, updated_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'cancelled', 'failed')
            ''', (now, task_id))
        if task_id is None:
            await conn.execute('''
                UPDATE trigger_runs
                SET delivered_at = COALESCE(delivered_at, ?)
                WHERE delivered_at IS NULL
                  AND task_id IN (
                      SELECT id FROM trigger_tasks WHERE status = 'cancelled'
                  )
            ''', (now,))
        else:
            await conn.execute('''
                UPDATE trigger_runs
                SET delivered_at = COALESCE(delivered_at, ?)
                WHERE task_id = ? AND delivered_at IS NULL
            ''', (now, task_id))
        return max(0, int(cursor.rowcount or 0))

    async def resume_legacy_auto_paused_repeat_tasks(self) -> int:
        conn = await self._get_conn()
        now = time.time()
        cursor = await conn.execute('''
            UPDATE trigger_tasks
            SET status = 'pending', next_run_at = ?, updated_at = ?, last_error = NULL,
                failure_count = MAX(failure_count - 1, 0),
                duplicate_count = 0, backoff_seconds = 0, backoff_until = NULL
            WHERE repeat = 1 AND status = 'failed'
              AND (
                  last_error LIKE 'repeat 任务在 %已自动暂停%'
                  OR last_error LIKE '%消息风暴%自动暂停%'
              )
        ''', (now, now))
        return max(0, int(cursor.rowcount or 0))

    async def create_trigger_run(self, task_id: str, scheduled_at: float,
                                 trigger_reason: str) -> Tuple[Dict[str, Any], bool]:
        conn = await self._get_conn()
        run_id = f"trun_{uuid.uuid4().hex[:10]}"
        now = time.time()
        cursor = await conn.execute('''
            INSERT OR IGNORE INTO trigger_runs (
                run_id, task_id, scheduled_at, started_at, status,
                trigger_reason, created_at
            ) VALUES (?, ?, ?, ?, 'running', ?, ?)
        ''', (run_id, task_id, scheduled_at, now, trigger_reason, now))
        created = bool(cursor.rowcount)
        row_cursor = await conn.execute(
            'SELECT * FROM trigger_runs WHERE task_id = ? AND scheduled_at = ?',
            (task_id, scheduled_at),
        )
        row = await row_cursor.fetchone()
        if row is None:
            raise RuntimeError(f'无法创建 trigger run: {task_id}')
        return dict(row), created

    async def finish_trigger_run(self, run_id: str, **fields: Any):
        allowed = {
            'finished_at', 'status', 'trigger_reason', 'matched_conditions',
            'exit_code', 'output', 'output_path', 'error', 'notice_started_at',
            'notice_sent_at', 'delivery_started_at', 'delivered_at',
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ', '.join(f'{key} = ?' for key in updates)
        conn = await self._get_conn()
        await conn.execute(
            f'UPDATE trigger_runs SET {assignments} WHERE run_id = ?',
            (*updates.values(), run_id),
        )

    async def claim_trigger_run_delivery(self, run_id: str, claimed_at: float) -> bool:
        """Atomically claim one finished trigger run for exactly-once delivery."""
        conn = await self._get_conn()
        cursor = await conn.execute('''
            UPDATE trigger_runs
            SET delivery_started_at = ?
            WHERE run_id = ?
              AND finished_at IS NOT NULL
              AND delivered_at IS NULL
              AND delivery_started_at IS NULL
        ''', (claimed_at, run_id))
        return bool(cursor.rowcount)

    async def get_trigger_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute('SELECT * FROM trigger_runs WHERE run_id = ?', (run_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_latest_delivered_trigger_run(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute('''
            SELECT * FROM trigger_runs
            WHERE task_id = ? AND status = 'condition_matched' AND delivered_at IS NOT NULL
              AND (error IS NULL OR error = '')
            ORDER BY delivered_at DESC
            LIMIT 1
        ''', (task_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def suppress_trigger_runs(self, run_ids: List[str], reason: str) -> int:
        if not run_ids:
            return 0
        conn = await self._get_conn()
        placeholders = ', '.join('?' for _ in run_ids)
        now = time.time()
        cursor = await conn.execute(
            f'''
            UPDATE trigger_runs
            SET delivered_at = COALESCE(delivered_at, ?),
                status = 'suppressed',
                trigger_reason = 'backlog_suppressed',
                error = CASE WHEN error IS NULL OR error = '' THEN ? ELSE error END
            WHERE run_id IN ({placeholders}) AND delivered_at IS NULL
            ''',
            (now, reason, *run_ids),
        )
        return max(0, int(cursor.rowcount or 0))

    async def list_undelivered_trigger_runs(self) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute('''
            SELECT r.*, t.chat_id, t.conversation_id, t.command, t.schedule_type,
                   t.schedule_expr, t.timezone, t.condition_expr, t.repeat,
                   t.origin_user_text, t.origin_assistant_text
            FROM trigger_runs r
            JOIN trigger_tasks t ON t.id = r.task_id
            WHERE r.finished_at IS NOT NULL AND r.delivered_at IS NULL
              AND r.status IN ('completed', 'condition_matched', 'condition_unmatched', 'failed', 'interrupted')
              AND t.status != 'cancelled'
            ORDER BY r.finished_at ASC
        ''')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def finalize_interrupted_trigger_deliveries(self) -> List[Dict[str, Any]]:
        conn = await self._get_conn()
        cursor = await conn.execute('''
            SELECT r.*
            FROM trigger_runs r
            JOIN trigger_tasks t ON t.id = r.task_id
            WHERE r.finished_at IS NOT NULL
              AND r.delivered_at IS NULL
              AND r.delivery_started_at IS NOT NULL
              AND t.status != 'cancelled'
            ORDER BY r.delivery_started_at ASC
        ''')
        rows = [dict(row) for row in await cursor.fetchall()]
        if not rows:
            return []
        run_ids = [row['run_id'] for row in rows]
        placeholders = ', '.join('?' for _ in run_ids)
        await conn.execute(
            f'''
            UPDATE trigger_runs
            SET delivered_at = ?
            WHERE run_id IN ({placeholders}) AND delivered_at IS NULL
            ''',
            (time.time(), *run_ids),
        )
        return rows

    async def interrupt_running_trigger_runs(self) -> int:
        conn = await self._get_conn()
        now = time.time()
        cursor = await conn.execute('''
            UPDATE trigger_runs
            SET status = 'interrupted', finished_at = ?,
                delivered_at = ?,
                error = COALESCE(error, 'Bot 进程退出，操作系统子进程不可恢复')
            WHERE status = 'running' AND finished_at IS NULL
        ''', (now, now))
        await conn.execute('''
            UPDATE trigger_tasks
            SET status = 'recovering', recovery_count = recovery_count + 1,
                updated_at = ?, last_error = 'Bot 重启后重新执行任务'
            WHERE status = 'running'
              AND EXISTS (
                  SELECT 1 FROM trigger_runs r
                  WHERE r.task_id = trigger_tasks.id
                    AND r.status = 'interrupted'
                    AND r.delivered_at = ?
              )
        ''', (now, now))
        return max(0, int(cursor.rowcount or 0))

    async def close(self):
        """关闭连接"""
        async with self._connection_lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                await connection.close()

# --- ☆ 用户数据管理（内存缓存 + 数据库同步）☆ ---
class UserDataManager:
    """管理用户数据，内存缓存优先"""
    
    _data: Dict[str, Any] = {}
    _db: Optional[BotMemoryDB] = None
    _initialized = False

    @classmethod
    def _require_db(cls) -> BotMemoryDB:
        db = cls._db
        if db is None:
            raise RuntimeError("UserDataManager 未初始化数据库")
        return db
    
    @classmethod
    async def init(cls):
        if cls._initialized:
            return
        cls._db = await BotMemoryDB.get_instance()
        await cls._load_from_db()
        cls._initialized = True
    
    @classmethod
    async def _load_from_db(cls):
        """从数据库加载数据到内存"""
        cls._data = {
            'state': BotState.IDLE,
            'providers': await cls._require_db().get_providers(),
            'active_provider_key': await cls._require_db().get_config('active_provider'),
            'default_model': await cls._require_db().get_config('default_model'),
            'default_media_provider_key': await cls._require_db().get_config('default_media_provider'),
            'default_media_model': await cls._require_db().get_config('default_media_model'),
            'current_chat_id': await cls._require_db().get_config('current_chat_id'),
            'assistant_prompt': await cls._require_db().get_config('assistant_prompt', PromptFileManager.get('assistant_prompt')),
            'global_prompt_addon': await cls._require_db().get_config('global_prompt_addon', PromptFileManager.get('global_prompt_addon')),
            'global_depth': await cls._require_db().get_config('global_depth', 30),
            'agent_mode': await cls._require_db().get_config('agent_mode', False),
            'agent_confirm': await cls._require_db().get_config('agent_confirm', False),
            'stream_mode': normalize_bool(await cls._require_db().get_config('stream_mode', True), True),
            'text_stitch_mode': normalize_text_stitch_mode(
                await cls._require_db().get_config('text_stitch_mode', DEFAULT_TEXT_STITCH_MODE)
            ),
            'stream_timeout': normalize_stream_timeout(await cls._require_db().get_config('stream_timeout', 0)),
            'agent_command_timeout': normalize_command_timeout(
                await cls._require_db().get_config('agent_command_timeout', DEFAULT_AGENT_COMMAND_TIMEOUT)
            ),
            'agent_max_iterations': normalize_agent_max_iterations(
                await cls._require_db().get_config('agent_max_iterations', DEFAULT_AGENT_MAX_ITERATIONS)
            ),
            'idle_message_interval': normalize_idle_message_interval(
                await cls._require_db().get_config('idle_message_interval', DEFAULT_IDLE_MESSAGE_INTERVAL)
            ),
            # 临时数据（不需要持久化）
            'temp_viewing_prov': None,
            'temp_list_type': None,
            'temp_page': 1,
            'temp_filter': None,
            'temp_saved_filter': None,
            'fetched_cache': [],
            'editing_provider': None,
            'temp_prov_name': None,
            'temp_prov_url': None,
            'temp_prov_format': None,
            'temp_model_target': None,
            'temp_back_callback': None,
            'prompt_buffer': '',
            'editing_prompt_key': '',
            'provider_import_mode': None,
        }
    
    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        return cls._data.get(key, default)
    
    @classmethod
    def set(cls, key: str, value: Any):
        cls._data[key] = value
    
    @classmethod
    async def save_config(cls, key: str, value: Any):
        """保存配置到数据库"""
        cls._data[key] = value
        await cls._require_db().set_config(key, value)
    
    @classmethod
    async def reload_providers(cls):
        """重新加载providers"""
        cls._data['providers'] = await cls._require_db().get_providers()

# --- ☆ API Key 轮询（多 Key 支持）☆ ---
# 每个 provider 维护一个轮询计数器，按逗号分隔的多个 key 依次轮询调用。
_api_key_counters: Dict[str, int] = {}
_API_KEY_WHITESPACE_RE = re.compile(r'\s+')

def parse_api_keys(api_key_field: str) -> List[str]:
    """解析逗号分隔的 API Key，并移除复制粘贴时混入的空白字符。"""
    keys: List[str] = []
    for raw_key in str(api_key_field or '').split(','):
        key = _API_KEY_WHITESPACE_RE.sub('', raw_key)
        if key:
            keys.append(key)
    return keys


def get_next_api_key(prov_name: str, api_key_field: str) -> str:
    """从可能包含多个逗号分隔 key 的字段中取出下一个 key（轮询）。

    支持填写多个 API Key，用英文逗号隔开，忽略所有空白字符。
    单个 Key 时直接返回；多个 Key 时按轮询方式依次取出。
    """
    keys = parse_api_keys(api_key_field)
    if not keys:
        return re.sub(r'\s+', '', str(api_key_field or ''))
    if len(keys) == 1:
        return keys[0]
    idx = _api_key_counters.get(prov_name, 0) % len(keys)
    _api_key_counters[prov_name] = (idx + 1) % len(keys)
    return keys[idx]

# --- ☆ OpenAI 客户端管理 ☆ ---
class PortalManager:
    _portals = {}

    @staticmethod
    def _schedule_close(client: Any) -> None:
        """在当前事件循环后台关闭被替换或移除的客户端。"""
        close_method = getattr(client, 'close', None)
        if not callable(close_method):
            return
        try:
            loop = asyncio.get_running_loop()
            close_result = close_method()
            if inspect.isawaitable(close_result):
                loop.create_task(close_result)
        except Exception:
            logger.debug("调度关闭 OpenAI 客户端失败", exc_info=True)

    @classmethod
    def get_portal(cls, provider_name: str, api_key: str, base_url: str) -> AsyncOpenAI:
        read_timeout = normalize_stream_timeout(UserDataManager.get('stream_timeout', 0))
        read_timeout_value = None if read_timeout <= 0 else read_timeout
        config_hash = f"{base_url}|{api_key}|read_timeout={read_timeout_value}"
        # 以 provider|api_key 作为缓存键，使多 Key 轮询时每个 Key 都有独立缓存，
        # 避免每次轮询都重建客户端。
        cache_key = f"{provider_name}|{api_key}"
        if cache_key in cls._portals:
            cached = cls._portals[cache_key]
            if cached['hash'] == config_hash:
                return cached['client']
            cls._portals.pop(cache_key, None)
            cls._schedule_close(cached.get('client'))

        import httpx
        client_timeout = httpx.Timeout(connect=20.0, read=read_timeout_value, write=60.0, pool=60.0)
        http_client = httpx.AsyncClient(
            timeout=client_timeout,
            headers=PROVIDER_HTTP_HEADERS,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=16,
                max_keepalive_connections=8,
                keepalive_expiry=30.0,
            ),
        )
        new_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=client_timeout,
            max_retries=2,
            default_headers=PROVIDER_HTTP_HEADERS,
            http_client=http_client,
        )
        cls._portals[cache_key] = {'client': new_client, 'hash': config_hash}
        return new_client

    @classmethod
    def remove_portal(cls, provider_name: str):
        """移除Provider的所有客户端（含多 Key 轮询缓存），释放资源"""
        prefix = f"{provider_name}|"
        keys_to_remove = [k for k in cls._portals if k.startswith(prefix) or k == provider_name]
        for key in keys_to_remove:
            entry = cls._portals.pop(key, None)
            if entry:
                client = entry.get('client')
                if client:
                    cls._schedule_close(client)
        _api_key_counters.pop(provider_name, None)

    @classmethod
    async def close_all(cls) -> None:
        """关闭所有缓存的 OpenAI SDK 客户端，供应用生命周期统一调用。"""
        entries = list(cls._portals.values())
        cls._portals.clear()
        _api_key_counters.clear()
        close_tasks = []
        for entry in entries:
            client = entry.get('client')
            if client is None:
                continue
            close_method = getattr(client, 'close', None)
            if callable(close_method):
                try:
                    close_result = close_method()
                    if inspect.isawaitable(close_result):
                        close_tasks.append(close_result)
                except Exception:
                    logger.debug("关闭 OpenAI 客户端失败", exc_info=True)
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

# --- ☆ Agent 命令执行器 ☆ ---
