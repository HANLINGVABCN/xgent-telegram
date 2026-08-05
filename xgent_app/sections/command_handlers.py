# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

async def cmd_delete_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    
    await UserDataManager.init()
    
    db = await BotMemoryDB.get_instance()
    counts = await db.clear_all_conversation_memory()
    UserDataManager.set('current_chat_id', SINGLE_MEMORY_SESSION_ID)
    await UserDataManager.save_config('current_chat_id', SINGLE_MEMORY_SESSION_ID)

    message = update.message or update.callback_query.message
    deleted_total = counts['global_messages']
    deleted_mirror = counts['chat_messages']
    deleted_sessions = counts['chat_sessions']

    if update.callback_query:
        await message.edit_text(
            "🧹 全局记忆已经清空了。\n"
            f"🌐 删除了 {deleted_total} 条全局记忆记录\n"
            f"🪞 删除了 {deleted_mirror} 条内部镜像消息\n"
            f"📦 清掉了 {deleted_sessions} 条内部索引记录\n\n"
            "Provider 配置、提示词、.env 都还在。",
            reply_markup=get_main_menu()
        )
    else:
        await message.reply_text(
            "🧹 全局记忆已经清空了。\n"
            f"🌐 删除了 {deleted_total} 条全局记忆记录\n"
            f"🪞 删除了 {deleted_mirror} 条内部镜像消息\n"
            f"📦 清掉了 {deleted_sessions} 条内部索引记录\n\n"
            "Provider 配置、提示词、.env 都还在。",
            reply_markup=get_main_menu()
        )

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
        f"🌐 全局记忆数: {global_count}\n"
        f"👤 绑定用户ID: <code>{BotConfig.AUTHORIZED_USER_ID}</code>\n"
        f"🌐 全局模式: 常驻开启\n"
        f"🤖 Agent模式: {'开启' if UserDataManager.get('agent_mode', False) else '关闭'}\n"
        f"📊 全局记忆深度: {UserDataManager.get('global_depth', 30)}条\n"
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
        
    db = await BotMemoryDB.get_instance()
    global_msgs = await db.get_global_messages(10000)  # 获取更多记录
    global_depth = max(1, int(UserDataManager.get('global_depth', 30)))
    ai_context_current = await db.get_conversation_messages(global_depth)
    unauthorized_access_logs = await db.get_unauthorized_access_logs(1000)
    
    if not global_msgs and not unauthorized_access_logs:
        message = update.message or update.callback_query.message
        await message.reply_text("📭 还没有可导出的记录。")
        return
    
    message = update.message or update.callback_query.message
    status_msg = await message.reply_text("📦 正在整理并导出数据...")
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        def _format_ai_context(messages: List[Dict[str, Any]]) -> str:
            parts = []
            for idx, msg in enumerate(messages, start=1):
                parts.append(
                    f"--- message {idx} ---\n"
                    f"role: {msg.get('role')}\n"
                    "content:\n"
                    f"{msg.get('content')}"
                )
            return "\n\n".join(parts)

        base_prompt = get_runtime_prompt('assistant_prompt')
        global_addon = get_runtime_prompt('global_prompt_addon')
        agent_mode = bool(UserDataManager.get('agent_mode', False))
        actual_system_prompt = base_prompt + global_addon + build_memory_prompt_section() + get_agent_runtime_prompt(agent_mode)

        def _format_global_memory_context(messages: List[Dict[str, Any]]) -> str:
            return (
                "说明:\n"
                "这是一份导出时按当前配置拼出来的 AI 历史上下文视图，便于核对 AI 大概看到了哪些历史消息。\n"
                f"当前历史深度: {global_depth} 条。\n"
                "不同接口会再转换成各自 JSON/parts 格式；临时文件/图片本体和过长命令原文可能只保留索引或截断结果。\n\n"
                "================ HISTORY ================\n"
                f"{_format_ai_context(messages)}"
            )

        zf.writestr("提示词.txt", actual_system_prompt)
        zf.writestr("全局记忆.txt", _format_global_memory_context(ai_context_current))
        
        if unauthorized_access_logs:
            unauthorized_lines = []
            for log in unauthorized_access_logs:
                ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(log.get('timestamp', 0)))
                line = f"[{ts}]\n用户: {log.get('full_name')}(@{log.get('username') or '无'}) ID:{log.get('user_id')}\n行为: {log.get('action_type')}\n内容: {log.get('content')}\nBot回复: {log.get('bot_reply')}"
                unauthorized_lines.append(line)
            unauthorized_content = "\n\n".join(unauthorized_lines)
        else:
            unauthorized_content = "暂无陌生人拦截记录。"
        zf.writestr("陌生人拦截记录.txt", unauthorized_content)
    
    zip_buffer.seek(0)
    await GlobalRecorder.record_system_op("导出全部数据")
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=InputFile(zip_buffer, "系统记忆.zip"),
        caption="导出完成。"
    )
    await status_msg.delete()

# --- ☆ 空闲提醒系统（仅全局模式下工作）☆ ---
