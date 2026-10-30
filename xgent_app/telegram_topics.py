"""Private Telegram topic pages bind existing conversations, never move history."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import time

from .conversations import ConversationError

_incoming = ContextVar('telegram_input_route',default=None)
logger = logging.getLogger(__name__)


@contextmanager
def bind_input_route(bot_id,chat_id,thread_id):
    token=_incoming.set({'telegram_bot_id':bot_id,'telegram_chat_id':chat_id,'telegram_thread_id':thread_id})
    try:yield
    finally:_incoming.reset(token)


def input_route():
    return _incoming.get()


class TopicManager:
    def __init__(self,factory,bot,chat_id,*,enabled=False):
        self.factory,self.bot,self.chat_id=factory,bot,int(chat_id)
        self.bot_id=int(bot.id)
        self.enabled=bool(enabled and self.chat_id>0)
        self.lock=asyncio.Lock()

    async def rows(self):
        db=await self.factory();conn=await db._get_conn()
        cursor=await conn.execute('SELECT * FROM telegram_topics WHERE bot_id=? AND chat_id=?',
                                  (self.bot_id,self.chat_id))
        try:return [dict(row) for row in await cursor.fetchall()]
        finally:await cursor.close()

    async def route(self,cid):
        if not self.enabled:return {}
        row=next((row for row in await self.rows() if row['conversation_id']==cid and row['status']=='ready'),None)
        return {'telegram_bot_id':self.bot_id,'telegram_chat_id':self.chat_id,'telegram_thread_id':row['thread_id'] if row else None}

    async def _save(self,cid,status,title,thread_id=None):
        db=await self.factory()
        async with db._transaction() as conn:
            await conn.execute('INSERT INTO telegram_topics VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(bot_id,chat_id,conversation_id) DO UPDATE SET '
                'thread_id=excluded.thread_id,status=excluded.status,title=excluded.title,updated_at=excluded.updated_at',
                (self.bot_id,self.chat_id,cid,thread_id,status,title,time.time()))

    async def _call(self,method,**data):
        return await self.bot._post(method,{'chat_id':self.chat_id,**data})

    async def reconcile(self):
        if not self.enabled:return
        from telegram.error import BadRequest
        async with self.lock:
            db=await self.factory();sessions=await db.get_all_sessions()
            rows={row['conversation_id']:row for row in await self.rows()}
            ids={session['id'] for session in sessions}
            for cid,row in rows.items():
                if cid not in ids and row['status']!='orphaned':
                    await self._save(cid,'orphaned',row['title'],row['thread_id'])
            for session in sessions:
                cid=session['id'];row=rows.get(cid);title=(session.get('name') or '新对话')[:80]
                if session.get('deleting'):
                    if row:await self._save(cid,'orphaned',title,row['thread_id'])
                    continue
                if row and row['status'] in {'creating','uncertain','failed','orphaned'}:
                    continue  # Never automatically repeat an ambiguous create.
                if session.get('archived'):
                    if row and row['status']=='ready':
                        try:
                            await self._call('closeForumTopic',message_thread_id=row['thread_id'])
                            await self._save(cid,'closed',title,row['thread_id'])
                        except BadRequest as exc:
                            if 'already closed' in str(exc).lower():await self._save(cid,'closed',title,row['thread_id'])
                            else:logger.warning('关闭 Telegram 会话页面失败：%s',exc)
                        except Exception as exc:logger.warning('关闭 Telegram 会话页面未确认，将再次同步：%s',exc)
                    continue
                if row is None:
                    await self._save(cid,'creating',title)
                    try:
                        topic=await self._call('createForumTopic',name=title)
                    except BadRequest as exc:
                        await self._save(cid,'failed',title)
                        logger.warning('创建 Telegram 会话页面被拒绝：%s',exc)
                        continue
                    except Exception:
                        await self._save(cid,'uncertain',title)
                        logger.warning('Telegram 页面创建结果未确认，请绑定已有页面；不重复创建')
                        continue
                    await self._save(cid,'ready',title,int(topic['message_thread_id']))
                    continue
                if row['status']=='closed':
                    try:
                        await self._call('reopenForumTopic',message_thread_id=row['thread_id'])
                    except BadRequest as exc:
                        if 'already open' not in str(exc).lower():
                            logger.warning('恢复 Telegram 页面失败：%s',exc);continue
                    except Exception as exc:
                        logger.warning('恢复 Telegram 页面未确认，将再次同步：%s',exc);continue
                    await self._save(cid,'ready',title,row['thread_id'])
                if row['title']!=title:
                    try:
                        await self._call('editForumTopic',message_thread_id=row['thread_id'],name=title)
                        await self._save(cid,'ready',title,row['thread_id'])
                    except Exception as exc:logger.warning('同步 Telegram 页面名称失败：%s',exc)

    async def bind(self,cid,thread_id):
        if not self.enabled:raise ConversationError('请先在 BotFather 开启私聊话题。')
        if type(thread_id) is not int or thread_id<=0:raise ConversationError('页面 ID 必须是正整数。')
        db=await self.factory();session=await db.get_session(cid)
        if not session or session.get('deleting'):raise ConversationError('会话不存在。')
        async with self.lock:
            if any(row['thread_id']==thread_id and row['conversation_id']!=cid for row in await self.rows()):
                raise ConversationError('此页面已经绑定其他会话。')
            await self._save(cid,'closed' if session.get('archived') else 'ready',session.get('name') or '新对话',thread_id)
        return await self.route(cid)

    async def admit(self,thread_id,name=None):
        if not self.enabled:return None
        rows=await self.rows()
        row=next((row for row in rows if row['thread_id']==thread_id),None)
        db=await self.factory()
        if row:
            session=await db.get_session(row['conversation_id'])
            if not session or session.get('deleting') or session.get('archived') or row['status'] not in {'ready','closed'}:
                raise ConversationError('此页面原会话已归档或删除，请恢复会话或使用 /topic_bind 重新绑定。')
            return row['conversation_id']
        matches=[row for row in rows if row['status'] in {'creating','uncertain'} and name and row['title']==name]
        if len(matches)==1:
            await self.bind(matches[0]['conversation_id'],thread_id)
            return matches[0]['conversation_id']
        if any(row['status'] in {'creating','uncertain'} for row in rows):
            raise ConversationError('存在创建结果未确认的页面，请先用 /topics 查看，再用 /topic_bind 会话ID 页面ID 绑定。')
        async with self.lock:
            # Recheck after taking the lock; two incoming messages cannot create
            # two conversations for the same user-created topic.
            row=next((row for row in await self.rows() if row['thread_id']==thread_id),None)
            if row:return row['conversation_id']
            result=await db.manage_conversation('create',name=(name or '电报会话 '+str(thread_id))[:80],select=False)
            cid=result['conversation_id']
            await self._save(cid,'ready',(name or '电报会话 '+str(thread_id))[:80],thread_id)
            return cid

    async def incoming_event(self,cid,thread_id,message):
        db=await self.factory()
        edited=getattr(message,'forum_topic_edited',None)
        if edited is not None and getattr(edited,'name',None):
            name=str(edited.name)[:80]
            await db.manage_conversation('rename',cid,name,select=False)
            await self._save(cid,'ready',name,thread_id)
        elif getattr(message,'forum_topic_closed',None) is not None:
            row=next((row for row in await self.rows() if row['conversation_id']==cid),None)
            if row:await self._save(cid,'closed',row['title'],thread_id)
        elif getattr(message,'forum_topic_reopened',None) is not None:
            row=next((row for row in await self.rows() if row['conversation_id']==cid),None)
            if row:await self._save(cid,'ready',row['title'],thread_id)
