import asyncio
from dataclasses import asdict

from telegram.error import NetworkError
from tests.attachment_request_probe import Harness
from tests.telegram_chat_probe import TelegramCapture
from xgent_app.conversations import bind_conversation
from xgent_app.fanout import OP_SEND, Op
from xgent_app.interaction import interaction
from xgent_app.telegram_topics import TopicManager, bind_input_route
from xgent_app.web_bridge import deliver_op_to_bot


class TopicsBot:
    id=7001
    def __init__(self):self.calls=[];self.serial=100;self.fail=False
    async def _post(self,method,data=None,**kwargs):
        self.calls.append((method,data))
        if method=='createForumTopic':
            if self.fail:self.fail=False;raise NetworkError('creation result unknown')
            self.serial+=1;return {'message_thread_id':self.serial,'name':data['name']}
        return True


async def exercise(bot, root):
    async with Harness(bot,root) as h:
        manager=bot.get_conversations();native=TopicsBot()
        topics=TopicManager(bot.BotMemoryDB.get_instance,native,31,enabled=True);manager.topics=topics
        await topics.reconcile()
        a=(await manager.state())['telegram_conversation_id']
        route_a=await topics.route(a)
        assert route_a['telegram_thread_id']==101
        with interaction('web','chat'):
            frozen=await manager.resolve(a)
            b=(await manager.manage('create',name='会话 B',selector='shared'))['conversation_id']
            assert (await topics.route(b))['telegram_thread_id']!=frozen.telegram_thread_id
            await manager.manage('rename',a,'会话 A',selector='shared')
            assert any(method=='editForumTopic' and data['name']=='会话 A' for method,data in native.calls)
            await manager.manage('archive',a,selector='shared')
            assert (await topics.route(a))['telegram_thread_id'] is None
            await manager.manage('restore',a,selector='shared')
            assert (await topics.route(a))['telegram_thread_id']==frozen.telegram_thread_id
        capture=TelegramCapture()
        with bind_conversation(frozen):
            op=Op(OP_SEND,logical_id=123,chat_id=31,payload={'text':'会话 A 回复','_conversation_context':asdict(frozen)})
            await deliver_op_to_bot(capture,op,None)
        assert next(iter(capture.messages.values()))['message_thread_id']==101
        incoming=await topics.admit(900,'用户创建的页面')
        with interaction('telegram','chat'),bind_input_route(native.id,31,900):
            scope=await manager.resolve(incoming,selector='telegram')
            assert scope.telegram_thread_id==900 and scope.conversation_id==incoming
        await topics.admit(900)
        assert len([row for row in await topics.rows() if row['thread_id']==900])==1
        native.fail=True
        with interaction('web','management'):
            uncertain=(await manager.manage('create',name='未确认',selector='shared'))['conversation_id']
        count=sum(method=='createForumTopic' and data['name']=='未确认' for method,data in native.calls)
        await topics.reconcile()
        assert sum(method=='createForumTopic' and data['name']=='未确认' for method,data in native.calls)==count==1
        assert next(row for row in await topics.rows() if row['conversation_id']==uncertain)['status']=='uncertain'
        await topics.bind(uncertain,901)
        assert (await topics.route(uncertain))['telegram_thread_id']==901
        foreign=TopicsBot();foreign.id=7002
        other=TopicManager(bot.BotMemoryDB.get_instance,foreign,31,enabled=True)
        assert (await other.route(a))['telegram_thread_id'] is None
        manager.topics=None
        return {'existing':True,'frozen_route':True,'rename_archive_restore':True,'unknown_binds':True,
                'create_once':True,'foreign_bot_ignored':True,'schema':await h.db.get_config('conversation_schema_version')}


async def migration(bot, root):
    from pathlib import Path
    async with Harness(bot,root) as h:
        async with h.db._transaction() as conn:
            await conn.execute("UPDATE config SET value='8' WHERE key='conversation_schema_version'")
            await conn.execute('DROP TABLE telegram_topics')
        h.db._config_cache.clear()
        await h.db.backup_legacy_conversations()
        await h.db.migrate_conversations()
        conn=await h.db._get_conn()
        row=await (await conn.execute("SELECT name FROM sqlite_master WHERE name='telegram_topics'")).fetchone()
        return {'version':await h.db.get_config('conversation_schema_version'),
                'backup':any(Path(root).glob('memory.db.pre-conversations-v9.sqlite3')),'table':bool(row)}


async def parallel_pages(bot, root):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock,patch
    from xgent_app.conversations import current_scope,conversation_operation
    from xgent_app.tg_html_adapter import install_telegram_html_adapter
    async with Harness(bot,root) as h:
        manager=bot.get_conversations();api=TopicsBot()
        topics=TopicManager(bot.BotMemoryDB.get_instance,api,31,enabled=True);manager.topics=topics
        await topics.reconcile();a=(await manager.state())['telegram_conversation_id']
        with interaction('web','management'):
            b=(await manager.manage('create',name='B',selector='shared'))['conversation_id']
        routes={a:(await topics.route(a))['telegram_thread_id'],b:(await topics.route(b))['telegram_thread_id']}
        native=TelegramCapture();install_telegram_html_adapter(native)
        entered,release=asyncio.Event(),asyncio.Event();seen=[]
        async def handler(update,context):
            async with conversation_operation(execution=True):
                scope=current_scope();seen.append((update.message.text,scope.conversation_id,scope.telegram_thread_id))
                if update.message.text=='A':entered.set();await release.wait()
                await context.bot.send_message(chat_id=31,text=update.message.text+'回复')
        def update(label,cid):
            message=SimpleNamespace(text=label,message_thread_id=routes[cid])
            return SimpleNamespace(message=message,effective_message=message,callback_query=None,
                                   effective_chat=SimpleNamespace(id=31),effective_user=SimpleNamespace(id=1))
        context=SimpleNamespace(bot=native)
        with patch.object(bot,'authorize_before_conversation',AsyncMock(return_value=True)):
            waiting=asyncio.create_task(bot.telegram_entry(handler)(update('A',a),context))
            await entered.wait()
            await asyncio.wait_for(bot.telegram_entry(handler)(update('B',b),context),5)
            assert not waiting.done()
            release.set();await waiting
        values={message['text']:message['message_thread_id'] for message in native.messages.values()}
        assert values['A回复']==routes[a] and values['B回复']==routes[b],values
        assert set(seen)=={('A',a,routes[a]),('B',b,routes[b])},seen
        assert (await manager.state())['telegram_conversation_id']==b
        manager.topics=None
        return {'parallel':True,'routes_frozen':True,'selected_b':True}
