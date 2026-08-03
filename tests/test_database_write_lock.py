"""数据库写入串行化的回归测试。

背景：全进程共用一条 aiosqlite 连接且是自动提交模式。显式事务期间的每个
await 都会让出控制权，别的协程此时发出的写会落进这个未关闭的事务里，跟着
一起提交或回滚。这个仓库确实存在并发写（后台触发器、消息记录、空闲任务），
所以这不是理论问题。
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_db_class():
    """只加载 database section 需要的最小依赖，避免拉起整个 bot。"""
    import contextlib
    import copy
    import json
    import logging
    import time
    import uuid
    from typing import Any, Dict, List, Optional, Tuple

    import aiosqlite

    namespace: Dict[str, Any] = {
        'aiosqlite': aiosqlite,
        'asyncio': asyncio,
        'contextlib': contextlib,
        'copy': copy,
        'json': json,
        'os': os,
        'time': time,
        'uuid': uuid,
        'logger': logging.getLogger('test'),
        'Any': Any, 'Dict': Dict, 'List': List,
        'Optional': Optional, 'Tuple': Tuple,
        'VALID_PROVIDER_API_FORMATS': {'openai'},
        # 只在读取路径用到的过滤器，这里不参与被测逻辑。
        'is_redundant_agent_command_record': lambda *a, **kw: False,
    }

    class _Cfg:
        DB_FILE = ':memory:'
    namespace['BotConfig'] = _Cfg

    source = (ROOT / 'xgent_app' / 'sections' / 'database.py').read_text(encoding='utf-8')
    # 只取 BotMemoryDB 类定义，后面的 UserDataManager 等依赖更多外部符号。
    start = source.index('class BotMemoryDB')
    end = source.index('# --- ☆ 用户数据管理')
    exec(compile(source[start:end], 'database.py', 'exec'), namespace)
    return namespace['BotMemoryDB']


BotMemoryDB = _load_db_class()


class WriteSerializationTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _run(self, coro):
        return asyncio.run(coro)

    def test_concurrent_write_survives_transaction_rollback(self):
        """事务回滚不能连带抹掉并发协程在此期间写入的数据。"""

        async def scenario():
            db = BotMemoryDB(self.db_path)
            await db._init_db()

            started = asyncio.Event()

            async def failing_import():
                # 走 _transaction()：中途抛错必然回滚自己的写。
                try:
                    async with db._transaction() as conn:
                        await conn.execute(
                            "INSERT OR REPLACE INTO providers (name, base_url, api_key, models, api_format)"
                            " VALUES ('doomed', 'u', 'k', '[]', 'openai')"
                        )
                        started.set()
                        # 让出控制权，给并发写一个挤进本事务的机会。
                        # 加锁前它会成功挤进来并被下面的回滚一起抹掉。
                        await asyncio.sleep(0.05)
                        raise RuntimeError('模拟导入中途失败')
                except RuntimeError:
                    pass

            async def concurrent_writer():
                await started.wait()
                # 事务仍开着时发出的独立写。加锁后这里会等事务结束，
                # 不会被卷进对方的回滚。
                await db.record_global_message(
                    chat_id=1, user_id=1, msg_type='USER',
                    role='user', content='must survive',
                )

            await asyncio.wait_for(
                asyncio.gather(failing_import(), concurrent_writer()),
                timeout=10,
            )

            messages = await db.get_global_messages(50)
            providers = await db.get_providers()
            await db.close()
            return messages, providers

        messages, providers = self._run(scenario())

        contents = [m['content'] for m in messages]
        self.assertIn('must survive', contents,
                      '并发写入被事务回滚连带抹掉了')
        self.assertNotIn('doomed', providers,
                         '失败的事务应该完全回滚')

    def test_get_providers_returns_copy_not_live_cache(self):
        """调用方就地删改返回值，不能污染内部缓存。"""

        async def scenario():
            db = BotMemoryDB(self.db_path)
            await db._init_db()
            await db.save_provider('p1', 'https://example.com', 'key', [], 'openai')

            first = await db.get_providers()
            del first['p1']                      # 模拟 callbacks.py 的先删后落库
            first_after = await db.get_providers()

            second = await db.get_providers()
            second['p1']['api_key'] = 'mutated'  # 嵌套字典也不能是共享引用
            second_after = await db.get_providers()

            await db.close()
            return first_after, second_after

        first_after, second_after = self._run(scenario())
        self.assertIn('p1', first_after, '删除副本的键污染了内部缓存')
        self.assertEqual('key', second_after['p1']['api_key'],
                         '修改副本的嵌套值污染了内部缓存')

    def test_set_config_does_not_cache_on_write_failure(self):
        """写库失败时不能留下与 DB 不一致的缓存。"""

        async def scenario():
            db = BotMemoryDB(self.db_path)
            await db._init_db()
            await db.set_config('good', 1)

            # 关掉底层连接，让后续写必然失败。
            conn = await db._get_conn()
            await conn.close()

            failed = False
            try:
                await db.set_config('bad', 999)
            except Exception:
                failed = True

            cached = db._config_cache.get('bad', '<absent>')
            return failed, cached

        failed, cached = self._run(scenario())
        self.assertTrue(failed, '预期写入失败，测试前提不成立')
        self.assertEqual('<absent>', cached,
                         '写库失败后仍把值写进了缓存')


if __name__ == '__main__':
    unittest.main()
