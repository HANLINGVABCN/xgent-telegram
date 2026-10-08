# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

@conversation_entry()
async def cmd_delete_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    
    await UserDataManager.init()
    
    db = await BotMemoryDB.get_instance()
    counts = await clear_current_conversation()
    publish_conversation_event(context, {'type': 'history_reset'})

    message = update.message or update.callback_query.message
    deleted_total = counts['global_messages']
    deleted_mirror = counts['chat_messages']

    if update.callback_query:
        await message.edit_text(
            "🧹 当前会话已经清空了；其他会话不受影响。\n"
            f"🌐 删除了 {deleted_total} 条会话记录\n"
            f"🪞 删除了 {deleted_mirror} 条内部镜像消息\n"
            f"📦 保留了会话名称与归档状态\n\n"
            "Provider 配置、提示词、.env 都还在，token 用量统计（/stats）也保留了。",
            reply_markup=get_main_menu()
        )
    else:
        await message.reply_text(
            "🧹 当前会话已经清空了；其他会话不受影响。\n"
            f"🌐 删除了 {deleted_total} 条会话记录\n"
            f"🪞 删除了 {deleted_mirror} 条内部镜像消息\n"
            f"📦 保留了会话名称与归档状态\n\n"
            "Provider 配置、提示词、.env 都还在，token 用量统计（/stats）也保留了。",
            reply_markup=get_main_menu()
        )

async def create_conversation_export(snapshot: Optional[Dict] = None) -> Tuple[Dict, Dict]:
    db = await BotMemoryDB.get_instance()
    snapshot = snapshot if snapshot is not None else await db.get_compression_snapshot()
    instruction = PromptFileManager.get_required('compression_prompt')
    system_prompt = build_conversation_system_prompt(bool(UserDataManager.get('agent_mode', False)))
    logs = await db.get_unauthorized_access_logs(1000)
    bundle = await asyncio.to_thread(
        save_conversation_export, ArtifactManager.ROOT_DIR, snapshot,
        system_prompt=system_prompt, compression_prompt=instruction, access_logs=logs,
    )
    return snapshot, bundle


async def deliver_conversation_export(context, chat_id: int, bundle: Dict, *, record_failure: bool = True) -> Optional[str]:
    try:
        with open(bundle['archive_path'], 'rb') as archive:
            await context.bot.send_document(chat_id=chat_id, document=archive,
                                            filename='系统记忆.zip', caption='导出完成。')
    except Exception as exc:
        logger.warning('Conversation export delivery failed: %s', exc)
        from xgent_app.error_reporting import error_text
        detail = error_text(f"归档已保存，但文件投递失败：{format_provider_exception(exc)}\n服务器文件路径：{bundle['archive_path']}")
        if not record_failure:
            return detail  # Compression records this after its frozen snapshot commit.
        return await GlobalRecorder.record_error(exc, chat_id, source='export_delivery', detail=detail)
    return None


def compression_retry_keyboard(entry: Dict) -> Optional[InlineKeyboardMarkup]:
    if entry.get('status') == 'completed':
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        '重试压缩', callback_data=f"retry_compress:{entry['job_id']}",
    )]])


async def cmd_compress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_context_compression(update, context)


async def build_compression_history(snapshot, instruction):
    """Freeze exactly this conversation, including native payloads, without addons."""
    from xgent_app.attachments import is_attachment_record
    from xgent_app.tool_context import restore_tool_context, deduplicate_attachment_parts
    from xgent_app.compression import metadata_of
    history = BotMemoryDB.conversation_records_to_messages(snapshot['records'])
    history = await asyncio.to_thread(restore_tool_context, history, snapshot['records'],
                                     ArtifactManager.UPLOAD_DIR)
    records = [r for r in snapshot['records'] if is_attachment_record(r)]
    parts, updates, errors = await asyncio.to_thread(
        prepare_attachment_context, records, ArtifactManager.UPLOAD_DIR,
        ArtifactManager.GENERATED_MEDIA_DIR,
    )
    for metadata in [metadata_of(r) for r in records] + [item[2] for item in updates]:
        for reference in metadata.get('attachments', []):
            if reference.get('kind') == 'invalid':
                errors.append(f"无法完整读取附件：{reference.get('name')}")
    if errors:
        raise CompressionError('压缩无法完整提供附件：\n' + '\n'.join(errors))
    parts = deduplicate_attachment_parts(history, parts)
    history = with_attachment_context(history, parts)
    # Unlike ordinary historical fallback, compression must never omit a binary.
    for message in history:
        if isinstance(message.get('content'), list):
            for part in message['content']:
                part.pop('degrade_if_unsupported', None)
    history.append({'role': 'user', 'content': instruction})
    return FrozenConversation(history, snapshot['generation'])


async def generate_compression_summary(provider, data, model, history, stop_event, usage_sink):
    """Buffer uncommitted output. No partial summary can become a chat message."""
    from xgent_app.compression import validate_summary
    args = (provider, get_next_api_key(provider, data['api_key']), data['base_url'],
            model, '', history)
    kwargs = dict(api_format=data.get('api_format', 'openai'), usage_sink=usage_sink,
                  trace_id=uuid.uuid4().hex, conversation_context=True)
    stop_task = asyncio.create_task(stop_event.wait())
    pending = None
    stream = None

    async def wait_result(awaitable, timeout):
        nonlocal pending
        if stop_event.is_set():
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise CompressionError('用户已停止压缩。')
        pending = asyncio.ensure_future(awaitable)
        done, _ = await asyncio.wait({pending, stop_task}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if stop_event.is_set():
            raise CompressionError('用户已停止压缩。')
        if pending not in done:
            raise CompressionError('压缩请求超时。')
        result = pending.result()
        pending = None
        return result

    try:
        if normalize_bool(UserDataManager.get('stream_mode', True), True):
            stream = ModelClient.think_and_reply_stream(*args, **kwargs).__aiter__()
            chunks = []
            while True:
                try:
                    chunk = await wait_result(stream.__anext__(), _stream_chunk_idle_timeout_seconds())
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
            text = ''.join(chunks)
        else:
            text, error = await wait_result(ModelClient.think_and_reply(*args, **kwargs),
                                            _nonstream_hard_timeout_seconds())
            if error:
                raise CompressionError(error)
        return validate_summary(text)
    finally:
        await cancel_task_quietly(pending, timeout=1.0)
        await cancel_task_quietly(stop_task, timeout=0.2)
        if stream is not None and hasattr(stream, 'aclose'):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(stream.aclose(), timeout=1.0)


@without_ui_history
@conversation_entry(execution=True)
async def run_context_compression(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   retry_job_id: Optional[str] = None):
    global _is_processing, _stop_generation_event, _compression_running
    if not await check_authorized_user_middleware(update, context):
        return
    message = update.message or update.callback_query.message
    await UserDataManager.init()
    if _conversation_processing_lock.locked():
        await message.reply_text('系统仍在处理上一个请求，请结束或停止后再压缩。')
        return
    async with _conversation_processing_lock:
        _is_processing = _compression_running = True
        stop_event = _stop_generation_event = get_conversations().stop_event() or asyncio.Event()
        get_conversations().attach_stop_event(stop_event)
        status = db = entry = snapshot = None
        warning = None
        usage_sink = []
        model = ''
        changed = False
        started = time.monotonic()
        restore_mirror = lambda: None
        typing_stop = asyncio.Event()
        typing_task = None
        try:
            if is_web_chat_running() and not getattr(context.bot, '_is_xgent_web_bot', False):
                outbox = get_web_outbox()
                if outbox is not None:
                    restore_mirror = install_tg_to_web_mirror(get_web_real_bot(), outbox)
            publish_conversation_event(context, {'type': 'compression_state', 'busy': True})
            typing_task = asyncio.create_task(keep_typing_while_waiting(
                context, update.effective_chat.id, typing_stop, max_duration=TYPING_MAX_DURATION_SECONDS))
            db = await BotMemoryDB.get_instance()
            _, session = await get_or_create_chat_session()
            provider, data = get_current_provider()
            model = resolve_effective_chat_model(session.get('model'), UserDataManager.get('default_model'), data)
            if not data or not model:
                raise CompressionError('请先配置对话模型。')
            # Retry deliberately takes the same path as a fresh compression.
            # Validate stale callbacks, but never restore/clear using an old source.
            if retry_job_id is not None:
                async with db._transaction() as conn:
                    old = await db._require_compression_job(conn, retry_job_id)
                    if old.get('status') == 'completed':
                        raise CompressionError('该任务已完成，不会重复压缩。')
            snapshot = await db.get_compression_snapshot()
            if not has_new_content(snapshot['records']):
                await message.reply_text('没有新的对话内容需要压缩。')
                return
            _, bundle = await create_conversation_export(snapshot)
            entry = await db.begin_compression(snapshot, bundle, update.effective_chat.id,
                                               provider, model, _RECORDER_SOURCE_ID, stop_event)
            warning = await deliver_conversation_export(context, update.effective_chat.id, bundle, record_failure=False)
            if warning:
                with contextlib.suppress(Exception):
                    await message.reply_text(warning)
            # Create this message only after archive delivery: editing an earlier
            # placeholder would leave the compression status above the ZIP in chat.
            with contextlib.suppress(Exception):
                status = await message.reply_text('正在压缩，成功前保留原上下文...', reply_markup=build_stop_keyboard())
            entry = await db.start_compression_attempt(entry['job_id'], provider, model)
            await asyncio.to_thread(verify_export, entry)
            history = await build_compression_history(snapshot, entry['instruction'])
            summary = await generate_compression_summary(provider, data, model, history, stop_event, usage_sink)
            await asyncio.to_thread(verify_export, entry)
            entry = await db.commit_compression(entry, summary, stop_event)
            await advance_ui_generation()
            changed = True
            cancel_pending_album_conversations()
            await get_or_create_chat_session()
            publish_conversation_event(context, {'type': 'history_reset'})
            # Delivery is after commit; a Telegram outage cannot undo the summary.
            with contextlib.suppress(Exception):
                await safe_send_message(context, update.effective_chat.id, summary)
                notice, _ = db._compression_notice(entry)
                await message.reply_text(notice)
        except asyncio.CancelledError:
            if entry is not None and not changed:
                with contextlib.suppress(Exception):
                    await db.fail_compression_attempt(entry, 'stopped', '压缩任务中断，可重试。')
            raise
        except Exception as exc:
            logger.warning('Context compression failed (committed=%s): %s', changed, exc)
            if changed:
                return
            from xgent_app.error_reporting import error_text
            detail = error_text(redact_sensitive_text(str(exc)))
            result = '压缩失败，原上下文已保留。\n' + detail
            if entry is not None:
                state = 'stopped' if stop_event.is_set() else 'failed'
                try:
                    entry = await db.fail_compression_attempt(entry, state, detail)
                except Exception as failure:
                    logger.error('Unable to save compression failure: %s', failure)
                if entry is None:
                    return  # User cleared the conversation; do not resurrect a notice.
                result, _ = db._compression_notice(entry)
            elif db is not None and snapshot is not None:
                # Export/preflight failures also get one model-visible system notice.
                if snapshot['generation'] != await db.get_attachment_generation():
                    return
                await db.record_global_message(
                    update.effective_chat.id, 0, MessageType.SYSTEM_OP, 'system', result,
                    current_scope().conversation_id, {'attachment_generation': snapshot['generation'],
                                               'src': _RECORDER_SOURCE_ID})
            with contextlib.suppress(Exception):
                if status is not None:
                    await safe_edit_text(status, result, reply_markup=compression_retry_keyboard(entry) if entry else None)
                    status = None
                else:
                    await message.reply_text(result, reply_markup=compression_retry_keyboard(entry) if entry else None)
        finally:
            if warning and db is not None:
                with contextlib.suppress(Exception):
                    if changed or snapshot is not None and snapshot['generation'] == await db.get_attachment_generation():
                        await GlobalRecorder.record_error(warning, update.effective_chat.id, source='export_delivery')
            typing_stop.set()
            try:
                await cancel_task_quietly(typing_task)
                if status is not None:
                    with contextlib.suppress(Exception):
                        await status.delete()
                if (db is not None and entry is not None
                        and entry.get('generation') == await db.get_attachment_generation()):
                    elapsed = time.monotonic() - started
                    for usage in usage_sink:
                        with contextlib.suppress(Exception):
                            await GlobalRecorder.record_token_usage(
                                build_token_usage_message(usage, elapsed) or '',
                                update.effective_chat.id, usage=usage, model=model)
                        # Recording makes usage visible in refreshed history; Telegram
                        # also needs an actual send through the active channel/mirror.
                        with contextlib.suppress(Exception):
                            await send_token_usage_message(
                                context, update.effective_chat.id, usage, elapsed)
            finally:
                _stop_generation_event = None
                _is_processing = _compression_running = False
                try:
                    publish_conversation_event(context, {'type': 'compression_state', 'busy': False, 'committed': changed})
                finally:
                    restore_mirror()


async def cmd_show_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    
    await UserDataManager.init()
    
    # 记录用户命令
    if update.message and update.message.text:
        await GlobalRecorder.record_user_message(update.message.text, MessageType.COMMAND, update.effective_chat.id)
        
    db = await BotMemoryDB.get_instance()
    _, cdata = await get_or_create_chat_session()
    
    # 统计全局消息数
    global_msgs = await db.get_global_messages(1000)
    global_count = len(global_msgs)
    
    # 分类统计
    type_counts = {}
    for msg in global_msgs:
        mt = msg.get('msg_type', 'unknown')
        type_counts[mt] = type_counts.get(mt, 0) + 1
    
    type_stats = "\n".join([f"  • {k}: {v}" for k, v in type_counts.items()]) or "  无记录"
    
    info = (
        f"ℹ️ <b>Bot 运行状态</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"💬 当前对话模型: {safe_text(format_model_target_summary('chat'))}\n"
        f"🖼️ 当前媒体模型: {safe_text(format_model_target_summary('media'))}\n"
        f"🪞 内部镜像消息数: {len(cdata.get('history', []))}\n"
        f"🗂 当前会话记录数: {global_count}\n"
        f"👤 绑定用户ID: <code>{BotConfig.AUTHORIZED_USER_ID}</code>\n"
        f"🌐 全局模式: 常驻开启\n"
        f"🤖 Agent模式: {'开启' if UserDataManager.get('agent_mode', False) else '关闭'}\n"
        f"📊 普通历史深度: {UserDataManager.get('global_depth', 30)}条\n"
        f"━━━━━━━━━━━━━━\n"
        f"📈 <b>全局记录分类:</b>\n{type_stats}\n"
        f"━━━━━━━━━━━━━━\n"
        f"服务正在运行"
    )
    
    message = update.message or update.callback_query.message
    await message.reply_text(info, parse_mode=constants.ParseMode.HTML)

async def cmd_export_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    
    await UserDataManager.init()
    
    # 记录用户命令
    if update.message and update.message.text:
        await GlobalRecorder.record_user_message(update.message.text, MessageType.COMMAND, update.effective_chat.id)
        
    message = update.message or update.callback_query.message
    status_msg = await message.reply_text("📦 正在整理并导出数据...")
    try:
        _, bundle = await create_conversation_export()
        await GlobalRecorder.record_system_op('导出全部数据')
        row_id = await GlobalRecorder.record_system_message(
            f"已生成全部数据导出文件。服务器文件路径：{bundle['archive_path']}（{bundle['size']} bytes）\n"
            f"文本记录：{bundle['text_dir']}", update.effective_chat.id,
            metadata={'display_media': [display_media_reference(bundle['archive_path'], '系统记忆.zip')]},
        )
        if row_id is None:
            raise CompressionError(f"归档已落盘，但下载关联未保存。服务器文件路径：{bundle['archive_path']}")
        warning = await deliver_conversation_export(context, update.effective_chat.id, bundle)
        if warning:
            await message.reply_text(warning)
    except Exception as exc:
        failure = await GlobalRecorder.record_error(exc, update.effective_chat.id, source='export',
            detail='导出失败，原有上下文未清除：\n' + format_provider_exception(exc))
        await message.reply_text(failure)
    finally:
        with contextlib.suppress(Exception):
            await status_msg.delete()

# --- ☆ 空闲提醒系统（仅全局模式下工作）☆ ---
