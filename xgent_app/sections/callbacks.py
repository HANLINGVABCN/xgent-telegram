# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_authorized_user_middleware(update, context):
        return

    # 停止按钮必须绕过普通菜单/记忆流程，尽快唤醒正在等待的生成或工具任务。
    data = CallbackDataStore.get(query.data or "")
    if data == "act_stop_generation":
        global _stop_generation_event
        if _stop_generation_event and not _stop_generation_event.is_set():
            _stop_generation_event.set()
            logger.info("用户手动停止了AI回答")
            await query.answer("已收到停止请求")
        else:
            await query.answer("当前没有正在生成的回答")
        return

    if data == "act_finish_text_stitch":
        await UserDataManager.init()
        await finish_text_conversation(update, context)
        return

    if data == "act_cancel_text_stitch":
        await UserDataManager.init()
        await cancel_text_conversation(update)
        return
    
    await UserDataManager.init()
    await query.answer()
    
    # 全局模式下记录按钮点击
    await GlobalRecorder.record_button_click(data, update.effective_chat.id)
    
    try:
        # --- 主菜单 ---
        if data == "act_main_menu":
            await query.message.edit_text(
                build_start_menu_text(),
                reply_markup=get_main_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "menu_more_settings":
            await query.message.edit_text(
                build_settings_menu_text(),
                reply_markup=get_more_settings_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "menu_text_stitch_mode":
            await query.message.edit_text(
                build_text_stitch_mode_text(),
                reply_markup=get_text_stitch_mode_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data.startswith("set_text_stitch_mode:"):
            mode = normalize_text_stitch_mode(data.split(":", 1)[1])
            UserDataManager.set('text_stitch_mode', mode)
            await UserDataManager.save_config('text_stitch_mode', mode)
            if mode == TEXT_STITCH_MODE_OFF:
                key = get_text_conversation_buffer_key(update)
                with _pending_text_conversations_lock:
                    pending = _pending_text_conversations.pop(key, None)
                if pending and pending.prompt_message is not None:
                    with contextlib.suppress(Exception):
                        await pending.prompt_message.edit_text("🧹 已关闭文字拼接，并清空本次拼接内容。")
            await GlobalRecorder.record_system_op(
                f"文字拼接模式切换为: {get_text_stitch_mode_label(mode)}",
                {"text_stitch_mode": mode}
            )
            await query.message.edit_text(
                build_text_stitch_mode_text(),
                reply_markup=get_text_stitch_mode_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "noop":
            return

        elif data == "select_update_source_normal":
            set_update_source(BotConfig.NORMAL_UPDATE_ZIP_URL)
            UserDataManager.set('pending_update_zip_url', "")
            await GlobalRecorder.record_system_op(
                "选择正常 bot 项目更新源",
                {"update_source": BotConfig.UPDATE_ZIP_URL},
                update.effective_chat.id
            )
            await send_update_confirmation_message(query.message)
            return

        elif data == "select_update_source_test":
            set_update_source(BotConfig.TEST_UPDATE_ZIP_URL)
            await GlobalRecorder.record_system_op(
                "选择 test 私有目录更新源",
                {"update_source": BotConfig.UPDATE_ZIP_URL},
                update.effective_chat.id
            )
            if not BotConfig.UPDATE_GITHUB_TOKEN:
                await request_update_github_token(query.message, BotConfig.TEST_UPDATE_ZIP_URL)
                return
            UserDataManager.set('pending_update_zip_url', "")
            await send_update_confirmation_message(query.message)
            return

        elif data == "do_update_keep_custom_files":
            await perform_update_system(update, context, overwrite_local_custom_files=False)
            return

        elif data == "do_update_overwrite_custom_files":
            await perform_update_system(update, context, overwrite_local_custom_files=True)
            return

        elif data == "menu_command_blacklist":
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('command_blacklist_buffer', "")
            await query.message.edit_text(
                build_command_blacklist_text(),
                reply_markup=get_command_blacklist_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_add_command_blacklist":
            UserDataManager.set('state', BotState.SET_COMMAND_BLACKLIST)
            UserDataManager.set('command_blacklist_buffer', "")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 完成添加", callback_data="act_confirm_command_blacklist")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_command_blacklist")]
            ])
            await query.message.reply_text(
                "🚫 <b>批量添加 Agent 命令黑名单</b>\n"
                "━━━━━━━━━━━━━━\n"
                "每行写一个禁止片段；命令中包含该片段就会被拦截。\n"
                "可以一次粘贴多条，也可以多次发送。\n"
                "批量输入时，每条一行；如果想分组或分隔，也可以用独立一行三个横杠 <code>---</code>。\n"
                "最后点“完成添加”。\n"
                "━━━━━━━━━━━━━━\n"
                "<i>发送 cancel 取消。保存后立即生效，无需重启。</i>",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_confirm_command_blacklist":
            buffer = UserDataManager.get('command_blacklist_buffer', "")
            patterns = AgentCommandBlacklist.parse_user_input(buffer)
            if not patterns:
                await query.answer("⚠️ 还没有可添加的黑名单内容", show_alert=True)
                return
            added = AgentCommandBlacklist.add(patterns)
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('command_blacklist_buffer', "")
            await GlobalRecorder.record_system_op(
                "添加 Agent 命令黑名单",
                {"input_count": len(patterns), "added_count": added}
            )
            await query.message.reply_text(
                f"✅ 已添加 {added} 条黑名单，当前共 {len(AgentCommandBlacklist.get_patterns())} 条。\n"
                "已立即生效，无需重启。",
                reply_markup=get_command_blacklist_menu()
            )

        elif data == "view_recommended_blacklist":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ 追加推荐名单", callback_data="act_add_recommended_blacklist")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_command_blacklist")]
            ])
            await query.message.edit_text(
                build_recommended_blacklist_text(),
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_add_recommended_blacklist":
            added = AgentCommandBlacklist.add(AgentCommandBlacklist.RECOMMENDED_PATTERNS)
            await GlobalRecorder.record_system_op(
                "追加推荐 Agent 命令黑名单",
                {"added_count": added}
            )
            await query.message.edit_text(
                f"✅ 已追加推荐名单，新增 {added} 条。\n\n"
                + build_command_blacklist_text("Agent 命令黑名单（已更新）"),
                reply_markup=get_command_blacklist_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_reload_command_blacklist":
            AgentCommandBlacklist.reload()
            await GlobalRecorder.record_system_op(
                "从文件重载 Agent 命令黑名单",
                {"count": len(AgentCommandBlacklist.get_patterns())}
            )
            await query.message.edit_text(
                build_command_blacklist_text("Agent 命令黑名单（已重载）"),
                reply_markup=get_command_blacklist_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "confirm_clear_command_blacklist":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 确认清空", callback_data="do_clear_command_blacklist")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_command_blacklist")]
            ])
            await query.message.edit_text(
                "⚠️ <b>确认清空 Agent 命令黑名单？</b>\n\n"
                "清空后，内置危险命令关键词也不会拦截；仍会保留交互/阻塞命令保护。",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "do_clear_command_blacklist":
            AgentCommandBlacklist.clear()
            await GlobalRecorder.record_system_op("清空 Agent 命令黑名单")
            await query.message.edit_text(
                build_command_blacklist_text("Agent 命令黑名单（已清空）"),
                reply_markup=get_command_blacklist_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        # --- 凭据配置 ---
        elif data == "menu_credentials":
            UserDataManager.set('state', BotState.IDLE)
            await query.message.edit_text(
                build_credentials_text(),
                reply_markup=get_credentials_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "menu_github_token":
            UserDataManager.set('state', BotState.IDLE)
            await query.message.edit_text(
                build_github_token_text(),
                reply_markup=get_github_token_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_set_github_token":
            UserDataManager.set('state', BotState.SET_UPDATE_TOKEN)
            UserDataManager.set('pending_update_zip_url', BotConfig.UPDATE_ZIP_URL)
            await query.message.reply_text(
                "🔑 <b>设置 GitHub Token</b>\n"
                "━━━━━━━━━━━━━━\n"
                "请发送 Fine-grained GitHub Token（<code>github_pat_</code> 开头）。\n\n"
                "生成时必须同时满足：\n"
                "1️⃣ <b>Repository access</b> → <code>Only select repositories</code> "
                "→ 勾上目标仓库\n"
                "2️⃣ <b>Permissions</b> → <code>Repository permissions</code> → "
                "<code>Contents</code> → <b>Read-only</b>\n\n"
                "<i>第 2 条默认是 No access，最容易漏。</i>\n"
                "━━━━━━━━━━━━━━\n"
                "保存时会自动验证，聊天记录里只保留掩码形式。\n"
                "<i>发送 cancel 取消。</i>",
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_test_github_token":
            status_msg = await query.message.reply_text("🔍 正在验证 Token...")
            result = await asyncio.to_thread(
                verify_update_github_token,
                BotConfig.UPDATE_GITHUB_TOKEN,
                BotConfig.UPDATE_ZIP_URL,
            )
            icon = "✅" if result['ok'] else "❌"
            await status_msg.edit_text(
                f"{icon} {result['message']}",
                reply_markup=get_github_token_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "confirm_clear_github_token":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 确认清除", callback_data="do_clear_github_token")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_github_token")]
            ])
            await query.message.edit_text(
                "⚠️ <b>确认清除 GitHub Token？</b>\n\n"
                "清除后无法从私有仓库拉取更新（会返回 404）。\n"
                "正常更新源不受影响。",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "do_clear_github_token":
            await asyncio.to_thread(clear_update_github_token)
            await GlobalRecorder.record_system_op("清除 GitHub Token")
            await query.message.edit_text(
                build_github_token_text(),
                reply_markup=get_github_token_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        # --- 联网搜索设置 ---
        elif data == "menu_search_settings":
            UserDataManager.set('state', BotState.IDLE)
            await query.message.edit_text(
                build_search_settings_text(),
                reply_markup=get_search_settings_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_set_search_key":
            UserDataManager.set('state', BotState.SET_SEARCH_KEY)
            await query.message.reply_text(
                "🔑 <b>设置搜索 API Key</b>\n"
                "━━━━━━━━━━━━━━\n"
                "请发送 Tavily API Key（通常以 <code>tvly-</code> 开头）。\n\n"
                "获取方式：访问 <code>tavily.com</code> 注册账号，"
                "在 Dashboard 复制 API Key，免费额度 1000 次/月。\n\n"
                "Key 会写入 <code>.env</code>，保存后立即生效，无需重启。\n"
                "为安全起见，收到后聊天记录里只会保留掩码形式。\n"
                "━━━━━━━━━━━━━━\n"
                "<i>发送 cancel 取消。</i>",
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_test_search":
            status_msg = await query.message.reply_text("🔍 正在测试搜索...")
            try:
                result = await run_search("hello world\nmax: 2", BotConfig.TAVILY_API_KEY)
            except Exception as e:
                logger.exception("搜索测试失败")
                await status_msg.edit_text(
                    f"❌ 测试失败：<code>{safe_text(format_provider_exception(e))}</code>",
                    parse_mode=constants.ParseMode.HTML
                )
                return
            if result.get('success'):
                hits = len(result.get('results') or [])
                await status_msg.edit_text(
                    f"✅ 搜索可用，返回 {hits} 条结果。\n"
                    "Agent 现在可以使用 search-x 和 fetch-x 了。",
                    reply_markup=get_search_settings_menu()
                )
            else:
                await status_msg.edit_text(
                    f"⚠️ 搜索不可用：\n<code>{safe_text(str(result.get('output') or '')[:600])}</code>",
                    reply_markup=get_search_settings_menu(),
                    parse_mode=constants.ParseMode.HTML
                )

        elif data == "confirm_clear_search_key":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 确认清除", callback_data="do_clear_search_key")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_search_settings")]
            ])
            await query.message.edit_text(
                "⚠️ <b>确认清除搜索 API Key？</b>\n\n"
                "清除后 Agent 无法联网搜索，search-x 会返回未配置提示。\n"
                "其他功能不受影响。",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "do_clear_search_key":
            await asyncio.to_thread(clear_search_api_key)
            await GlobalRecorder.record_system_op("清除搜索 API Key")
            await query.message.edit_text(
                build_search_settings_text(),
                reply_markup=get_search_settings_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        # --- 记忆管理 ---
        elif data == "menu_memory":
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('memory_buffer', "")
            await query.message.edit_text(
                build_memory_menu_text(),
                reply_markup=get_memory_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_add_memory":
            UserDataManager.set('state', BotState.SET_MEMORY)
            UserDataManager.set('memory_buffer', "")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 完成并保存", callback_data="act_confirm_memory")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_memory")]
            ])
            await query.message.reply_text(
                "🧠 <b>添加记忆</b>\n"
                "━━━━━━━━━━━━━━\n"
                "发送你希望 AI 牢记的内容（事实、偏好、背景等）。\n"
                "单条无长度限制：可以一次发送完整内容，也可以分多条发送，"
                "系统会自动拼接成一条后再保存。\n"
                "也可以发送 txt / md / text 文件，内容会追加到当前拼接。\n"
                "━━━━━━━━━━━━━━\n"
                "<i>全部发送完后点“完成并保存”。发送 cancel 取消。</i>",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_confirm_memory":
            buffer = UserDataManager.get('memory_buffer', "")
            buffer = buffer.strip()
            if not buffer:
                await query.answer("⚠️ 还没有输入任何内容", show_alert=True)
                return
            filename = save_memory_file(buffer)
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('memory_buffer', "")
            await GlobalRecorder.record_system_op(
                "添加用户记忆",
                {"filename": filename, "chars": len(buffer)}
            )
            await query.message.reply_text(
                f"✅ 已保存 1 条记忆（{len(buffer)} 字），已立即拼入 system prompt。",
                reply_markup=get_memory_menu()
            )

        elif data == "act_list_memory":
            files = list_memory_files()
            if not files:
                await query.answer("暂无记忆", show_alert=True)
                return
            # 完整列出每条内容（每条之间用分隔线）
            lines = []
            for idx, filename in enumerate(files, start=1):
                content = read_memory_file(filename).strip()
                lines.append(f"#{idx} [{filename}]\n{safe_text(content)}")
            full_text = "\n\n━━━━━━━━\n\n".join(lines)
            # Telegram 单条消息 4096 字符上限，超长则分段发送
            MAX_MSG = 3800
            chunks = [full_text[i:i + MAX_MSG] for i in range(0, len(full_text), MAX_MSG)] if full_text else ["（空）"]
            await query.message.reply_text(
                f"📋 <b>全部记忆（共 {len(files)} 条）</b>",
                parse_mode=constants.ParseMode.HTML
            )
            for chunk in chunks:
                await query.message.reply_text(
                    f"<pre>{chunk}</pre>",
                    parse_mode=constants.ParseMode.HTML
                )
            await query.message.reply_text(
                build_memory_menu_text(),
                reply_markup=get_memory_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data.startswith("act_delete_memory_menu:"):
            page_str = data.split(":", 1)[1]
            page = int(page_str) if page_str.isdigit() else 1
            files = list_memory_files()
            if not files:
                await query.answer("暂无记忆，无法删除", show_alert=True)
                return
            await query.message.edit_text(
                "🗑️ <b>删除记忆</b>\n点击要删除的条目：",
                reply_markup=get_memory_delete_keyboard(page),
                parse_mode=constants.ParseMode.HTML
            )

        elif data.startswith("act_delete_memory:"):
            filename = data.split(":", 1)[1]
            ok = delete_memory_file(filename)
            if ok:
                await GlobalRecorder.record_system_op(
                    "删除用户记忆",
                    {"filename": filename}
                )
                await query.answer(f"已删除: {filename}", show_alert=False)
            else:
                await query.answer("删除失败：文件不存在", show_alert=True)
            # 回到删除菜单或记忆主页
            files = list_memory_files()
            if not files:
                await query.message.edit_text(
                    build_memory_menu_text("记忆管理（已无记忆）"),
                    reply_markup=get_memory_menu(),
                    parse_mode=constants.ParseMode.HTML
                )
            else:
                await query.message.edit_text(
                    "🗑️ <b>删除记忆</b>\n点击要删除的条目：",
                    reply_markup=get_memory_delete_keyboard(1),
                    parse_mode=constants.ParseMode.HTML
                )

        elif data == "confirm_clear_user_memory":
            files = list_memory_files()
            if not files:
                await query.answer("暂无记忆", show_alert=True)
                return
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 确认清空", callback_data="do_clear_user_memory")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_memory")]
            ])
            await query.message.edit_text(
                f"⚠️ <b>确认清空全部 {len(files)} 条记忆？</b>\n\n"
                "清空后不可恢复，且记忆会立即从 system prompt 移除。",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "do_clear_user_memory":
            count = clear_all_memory()
            await GlobalRecorder.record_system_op(
                "清空全部用户记忆",
                {"cleared_count": count}
            )
            await query.message.edit_text(
                build_memory_menu_text(f"记忆管理（已清空 {count} 条）"),
                reply_markup=get_memory_menu(),
                parse_mode=constants.ParseMode.HTML
            )


        # --- Agent模式切换 ---
        elif data == "toggle_agent_mode":
            current = UserDataManager.get('agent_mode', False)
            new_mode = not current
            UserDataManager.set('agent_mode', new_mode)
            await UserDataManager.save_config('agent_mode', new_mode)
            
            await GlobalRecorder.record_system_op(
                f"Agent模式切换为: {'开启' if new_mode else '关闭'}",
                {"agent_mode": new_mode}
            )
            
            await query.message.edit_text(
                build_start_menu_text(),
                reply_markup=get_main_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        
        # --- 请求模式切换 ---
        elif data == "toggle_stream_mode":
            current = normalize_bool(UserDataManager.get('stream_mode', True), True)
            new_mode = not current
            UserDataManager.set('stream_mode', new_mode)
            await UserDataManager.save_config('stream_mode', new_mode)
            
            await GlobalRecorder.record_system_op(
                f"流式输出切换为: {'开启' if new_mode else '关闭'}",
                {"stream_mode": new_mode}
            )
            
            await query.message.edit_text(
                build_start_menu_text(),
                reply_markup=get_main_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        
        # --- Web Chat ---
        elif data == "menu_web":
            UserDataManager.set('state', BotState.IDLE)
            await query.message.edit_text(
                build_web_text(),
                reply_markup=get_web_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "toggle_web_enabled":
            enabled = not normalize_bool(UserDataManager.get('web_enabled', False), False)
            if enabled and not UserDataManager.get('_web_has_password', False):
                await query.answer("请先设置访问密码", show_alert=True)
                return
            UserDataManager.set('web_enabled', enabled)
            await UserDataManager.save_config('web_enabled', enabled)
            # 解耦: 只在服务器运行状态需要改变时才 start/stop, 不无谓 restart
            running = is_web_chat_running()
            term_still_on = normalize_bool(UserDataManager.get('terminal_enabled', False), False)
            if enabled or term_still_on:
                if not running:
                    await start_web_chat_if_enabled(context.application)
            else:
                if running:
                    await stop_web_chat()
            await GlobalRecorder.record_system_op(
                f"Web Chat {'开启' if enabled else '关闭'}",
                {"web_enabled": enabled}
            )
            await query.message.edit_text(
                build_web_text(),
                reply_markup=get_web_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_set_web_password":
            UserDataManager.set('state', BotState.SET_WEB_PASSWORD)
            await query.message.reply_text(
                "🔑 <b>设置 Web 访问密码</b>\n"
                "━━━━━━━━━━━━━━\n"
                "请发送新密码（至少 6 位）。\n\n"
                "密码只以 PBKDF2 哈希形式存进数据库，聊天记录里不会保留原文。\n"
                "保存后会自动重启 Web 服务使其生效。\n"
                "━━━━━━━━━━━━━━\n"
                "<i>发送 cancel 取消。</i>",
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_set_web_port":
            UserDataManager.set('state', BotState.SET_WEB_PORT)
            await query.message.reply_text(
                f"🔌 请输入监听端口（{MIN_WEB_PORT}-{MAX_WEB_PORT}）。\n"
                f"当前：{normalize_web_port(UserDataManager.get('web_port', DEFAULT_WEB_PORT))}。\n"
                "发送 cancel 取消。"
            )

        elif data == "act_set_web_public_url":
            UserDataManager.set('state', BotState.SET_WEB_PUBLIC_URL)
            await query.message.reply_text(
                "🌐 <b>设置公开访问地址</b>\n"
                "━━━━━━━━━━━━━━\n"
                "请发送反向代理的完整地址，必须以 <code>https://</code> 开头。\n"
                "例如：<code>https://chat.example.com</code>\n\n"
                "Telegram 的内嵌网页按钮只接受 HTTPS 地址；配置后 /start 里的 Web 按钮"
                "就能直接在 Telegram 内弹出页面。\n"
                "━━━━━━━━━━━━━━\n"
                "<i>发送 cancel 取消。</i>",
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "confirm_clear_web_password":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 确认清除", callback_data="do_clear_web_password")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_web")]
            ])
            await query.message.edit_text(
                "⚠️ <b>确认清除 Web 访问密码？</b>\n\n"
                "清除后 Web 服务会立即停止，直到重新设置密码。",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "do_clear_web_password":
            await clear_web_password()
            # 没有密码就不能继续对外服务，连同开关一起关掉。
            UserDataManager.set('web_enabled', False)
            await UserDataManager.save_config('web_enabled', False)
            await stop_web_chat()
            await GlobalRecorder.record_system_op("清除 Web 访问密码并停止服务")
            await query.message.edit_text(
                build_web_text(),
                reply_markup=get_web_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "do_clear_web_public_url":
            UserDataManager.set('web_public_url', '')
            await UserDataManager.save_config('web_public_url', '')
            await GlobalRecorder.record_system_op("清除 Web 公开地址")
            await query.message.edit_text(
                build_web_text(),
                reply_markup=get_web_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "toggle_terminal_enabled":
            term_on = not normalize_bool(UserDataManager.get('terminal_enabled', False), False)
            if term_on and not UserDataManager.get('_web_has_password', False):
                await query.answer("请先设置访问密码", show_alert=True)
                return
            UserDataManager.set('terminal_enabled', term_on)
            await UserDataManager.save_config('terminal_enabled', term_on)
            # 解耦: 终端与 Web 共享服务器但独立开关
            running = is_web_chat_running()
            web_still_on = normalize_bool(UserDataManager.get('web_enabled', False), False)
            if term_on or web_still_on:
                if not running:
                    await start_web_chat_if_enabled(context.application)
            else:
                if running:
                    await stop_web_chat()
            await GlobalRecorder.record_system_op(
                f"终端{'开启' if term_on else '关闭'}",
                {"terminal_enabled": term_on}
            )
            await query.message.edit_text(
                build_web_text(),
                reply_markup=get_web_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        # --- 思考深度 ---
        elif data == "menu_thinking_level":
            await query.message.edit_text(
                build_thinking_level_text(),
                reply_markup=get_thinking_level_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data.startswith("set_thinking_level:"):
            level = normalize_thinking_level(data.split(":", 1)[1])
            UserDataManager.set('thinking_level', level)
            await UserDataManager.save_config('thinking_level', level)
            # 换档位后允许重新试探：之前被记为"不支持"的模型可能只是不支持旧档位。
            ModelClient._thinking_unsupported.clear()
            await GlobalRecorder.record_system_op(
                f"设置思考深度: {get_thinking_level_label(level)}",
                {"thinking_level": level}
            )
            await query.message.edit_text(
                build_thinking_level_text(),
                reply_markup=get_thinking_level_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        # --- 超时设置 ---
        elif data == "menu_timeout_settings":
            await query.message.edit_text(
                build_timeout_settings_text(),
                reply_markup=get_timeout_settings_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data in {"cmd_set_ai_timeout", "cmd_set_stream_timeout"}:
            current_timeout = UserDataManager.get('stream_timeout', 0)
            await query.message.edit_text(
                f"💬 <b>AI回复超时</b>\n\n"
                f"当前: <b>{_fmt_timeout(current_timeout)}</b>\n"
                f"这决定等待模型下一段数据或完整结果时的最长时间。\n"
                f"设为 ∞ 表示不限制，适合慢模型或长任务。",
                reply_markup=get_ai_timeout_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "set_ai_timeout_custom":
            UserDataManager.set('state', BotState.SET_AI_TIMEOUT)
            await query.message.reply_text(
                "💬 请输入自定义 AI 回复超时秒数。\n"
                "例如: 45、180、300s。发送 cancel 取消。"
            )
        
        elif data.startswith("set_ai_timeout_") or data.startswith("set_timeout_"):
            timeout_val = int(data.rsplit("_", 1)[1])
            UserDataManager.set('stream_timeout', timeout_val)
            await UserDataManager.save_config('stream_timeout', timeout_val)
            # 清除 Portal 缓存，使新超时生效
            PortalManager._portals.clear()
            await GlobalRecorder.record_system_op(f"设置 AI 回复超时: {_fmt_timeout(timeout_val)}")
            await query.message.edit_text(
                f"✅ AI回复超时已设为 <b>{_fmt_timeout(timeout_val)}</b>。",
                reply_markup=get_timeout_settings_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "cmd_set_command_timeout":
            current_timeout = UserDataManager.get('agent_command_timeout', DEFAULT_AGENT_COMMAND_TIMEOUT)
            await query.message.edit_text(
                f"⌨️ <b>命令等待窗口</b>\n\n"
                f"当前: <b>{_fmt_command_timeout(current_timeout)}</b>\n"
                f"run 命令会最多等待这个时间；shell/stdin 会结合输出活跃度、静默、交互提示和长驻预判决定何时回灌，等待窗口是硬上限。\n"
                f"系统会把有判断价值的 shell 结果交给 AI，AI 可继续自动执行下一步协议。",
                reply_markup=get_command_timeout_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "set_command_timeout_custom":
            UserDataManager.set('state', BotState.SET_COMMAND_TIMEOUT)
            await query.message.reply_text(
                f"⌨️ 请输入自定义命令等待窗口秒数 ({MIN_AGENT_COMMAND_TIMEOUT}-{MAX_AGENT_COMMAND_TIMEOUT})。\n"
                "例如: 90、300、600s。发送 cancel 取消。"
            )

        elif data.startswith("set_command_timeout_"):
            timeout_val = normalize_command_timeout(data.rsplit("_", 1)[1])
            UserDataManager.set('agent_command_timeout', timeout_val)
            await UserDataManager.save_config('agent_command_timeout', timeout_val)
            await GlobalRecorder.record_system_op(f"设置命令等待窗口: {_fmt_command_timeout(timeout_val)}")
            await query.message.edit_text(
                f"✅ 命令等待窗口已设为 <b>{_fmt_command_timeout(timeout_val)}</b>。",
                reply_markup=get_timeout_settings_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "cmd_set_agent_max_iterations":
            current_iterations = UserDataManager.get('agent_max_iterations', DEFAULT_AGENT_MAX_ITERATIONS)
            await query.message.edit_text(
                f"🔁 <b>Agent最大轮数</b>\n\n"
                f"当前: <b>{_fmt_agent_max_iterations(current_iterations)}</b>\n"
                f"只有新的真实用户消息会把轮数重置为 0。工具结果和后台 trigger 结果会继续累计；"
                f"超出上限后仍显示系统结果，但不会继续请求 AI。",
                reply_markup=get_agent_max_iterations_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "set_agent_max_iterations_custom":
            UserDataManager.set('state', BotState.SET_AGENT_MAX_ITERATIONS)
            await query.message.reply_text(
                f"🔁 请输入自定义 Agent 最大轮数 ({MIN_AGENT_MAX_ITERATIONS}-{MAX_AGENT_MAX_ITERATIONS})。\n"
                "例如: 8、15、25轮。发送 cancel 取消。"
            )

        elif data.startswith("set_agent_max_iterations_"):
            iterations = normalize_agent_max_iterations(data.rsplit("_", 1)[1])
            UserDataManager.set('agent_max_iterations', iterations)
            await UserDataManager.save_config('agent_max_iterations', iterations)
            await GlobalRecorder.record_system_op(f"设置 Agent 最大轮数: {_fmt_agent_max_iterations(iterations)}")
            await query.message.edit_text(
                f"✅ Agent最大轮数已设为 <b>{_fmt_agent_max_iterations(iterations)}</b>。",
                reply_markup=get_timeout_settings_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "cmd_set_idle_message_interval":
            current_interval = UserDataManager.get('idle_message_interval', DEFAULT_IDLE_MESSAGE_INTERVAL)
            await query.message.edit_text(
                f"💭 <b>空闲提醒间隔</b>\n\n"
                f"当前: <b>{_fmt_idle_message_interval(current_interval)}</b>\n"
                f"到达这个时间没有收到你的消息后，系统会按正常聊天上下文额外追加空闲提醒提示词生成回复。\n"
                f"设为 ∞关闭 表示不自动触发空闲提醒。",
                reply_markup=get_idle_message_interval_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "set_idle_message_interval_custom":
            UserDataManager.set('state', BotState.SET_IDLE_MESSAGE_INTERVAL)
            await query.message.reply_text(
                "💭 请输入自定义空闲提醒间隔。\n"
                "例如: 90m、2h、3天、7200s；发送 0、∞ 或 关闭 可停用。发送 cancel 取消。"
            )

        elif data.startswith("set_idle_message_interval_"):
            interval = normalize_idle_message_interval(data.rsplit("_", 1)[1])
            UserDataManager.set('idle_message_interval', interval)
            await UserDataManager.save_config('idle_message_interval', interval)
            await GlobalRecorder.record_system_op(f"设置空闲提醒间隔: {_fmt_idle_message_interval(interval)}")
            await query.message.edit_text(
                f"✅ 空闲提醒间隔已设为 <b>{_fmt_idle_message_interval(interval)}</b>。",
                reply_markup=get_timeout_settings_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        
        # --- 记忆深度设置 ---
        elif data == "cmd_set_global_depth":
            current_depth = UserDataManager.get('global_depth', 30)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("10条", callback_data="set_depth_10"),
                 InlineKeyboardButton("20条", callback_data="set_depth_20"),
                 InlineKeyboardButton("30条", callback_data="set_depth_30")],
                [InlineKeyboardButton("50条", callback_data="set_depth_50"),
                 InlineKeyboardButton("100条", callback_data="set_depth_100"),
                 InlineKeyboardButton("200条", callback_data="set_depth_200")],
                [InlineKeyboardButton("✍️ 自定义", callback_data="set_depth_custom")],
                [InlineKeyboardButton("🔙 返回", callback_data="act_main_menu")]
            ])
            await query.message.edit_text(
                f"📊 <b>全局记忆深度设置</b>\n\n"
                f"当前深度: <b>{current_depth}条</b>\n"
                f"这决定了全局模式下系统能回顾多少条历史消息。",
                reply_markup=keyboard,
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data.startswith("set_depth_"):
            depth_str = data.split("_")[2]
            if depth_str == "custom":
                UserDataManager.set('state', BotState.SET_GLOBAL_DEPTH)
                await query.message.reply_text("🔢 请输入自定义的记忆深度 (1-500)，或发送 'cancel' 取消:")
            else:
                depth = int(depth_str)
                UserDataManager.set('global_depth', depth)
                await UserDataManager.save_config('global_depth', depth)
                await GlobalRecorder.record_system_op(f"设置记忆深度: {depth}")
                await query.message.edit_text(
                    f"✅ 记忆深度已设为 <b>{depth}条</b> 。",
                    reply_markup=get_more_settings_menu(),
                    parse_mode=constants.ParseMode.HTML
                )
        
        # --- 提示词菜单 ---
        elif data == "menu_prompts":
            await query.message.edit_text(
                "📝 <b>提示词设置</b>\n\n选择要查看或修改的提示词。",
                reply_markup=get_prompts_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "toggle_silent_unauthorized":
            # 未授权静默：开启后未授权用户发消息不回复（仍记录+通知授权用户）。
            on = not normalize_bool(UserDataManager.get('silent_unauthorized', False), False)
            UserDataManager.set('silent_unauthorized', on)
            await UserDataManager.save_config('silent_unauthorized', on)
            await GlobalRecorder.record_system_op(
                f"未授权静默模式{'开启' if on else '关闭'}",
                {"silent_unauthorized": on}
            )
            await query.answer(f"已{'开启' if on else '关闭'}静默", show_alert=False)
            await query.message.edit_text(
                "📝 <b>提示词设置</b>\n\n选择要查看或修改的提示词。",
                reply_markup=get_prompts_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data.startswith("view_prompt:"):
            key = data.split(":", 1)[1]
            await show_prompt_detail(query, key)

        elif data.startswith("reload_prompt:"):
            key = data.split(":", 1)[1]
            if key not in PromptFileManager.FILES:
                await query.answer("提示词不存在", show_alert=True)
                return
            PromptFileManager.reload_all()
            if key in {'assistant_prompt', 'global_prompt_addon'}:
                await reload_runtime_prompt(key)
            await GlobalRecorder.record_system_op(
                f"从文件重载提示词: {PromptFileManager.get_label(key)}"
            )
            await query.answer("✅ 已从文件重载！", show_alert=True)
            await show_prompt_detail(query, key, " (已重载)")

        elif data.startswith("download_prompt:"):
            key = data.split(":", 1)[1]
            if key not in PromptFileManager.FILES:
                await query.answer("提示词不存在", show_alert=True)
                return
            path = PromptFileManager.get_abs_path(key)
            if not os.path.exists(path):
                await query.answer("提示词文件不存在", show_alert=True)
                return
            await query.answer("正在发送提示词文件")
            with open(path, 'rb') as f:
                await query.message.reply_document(
                    document=InputFile(f, filename=os.path.basename(path)),
                    caption=f"📥 {PromptFileManager.get_label(key)}"
                )

        elif data.startswith("modify_prompt:"):
            key = data.split(":", 1)[1]
            if key not in PromptFileManager.FILES:
                await query.answer("提示词不存在", show_alert=True)
                return
            UserDataManager.set('state', BotState.SET_ANY_PROMPT)
            UserDataManager.set('editing_prompt_key', key)
            UserDataManager.set('prompt_buffer', "")
            msg = (
                f"📝 <b>修改 {safe_text(PromptFileManager.get_label(key))}</b>\n"
                "━━━━━━━━━━━━━━\n"
                "1️⃣ 发送 .txt / .md 文件\n"
                "2️⃣ 或直接发送文字（可多次发送）\n"
                f"{get_prompt_edit_note(key)}\n"
                "━━━━━━━━━━━━━━\n"
                "<i>发送 'cancel' 取消</i>"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 完成输入", callback_data=f"act_confirm_prompt:{key}")]
            ])
            await query.message.reply_text(msg, reply_markup=kb, parse_mode=constants.ParseMode.HTML)
        
        elif data == "view_normal_prompt":
            curr = get_runtime_prompt('assistant_prompt')
            preview = safe_text(curr)[:500] + "..." if len(curr) > 500 else safe_text(curr)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ 修改提示词", callback_data="act_modify_normal_prompt")],
                [InlineKeyboardButton("🔄 从文件重载", callback_data="reset_normal_prompt")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_prompts")]
            ])
            await query.message.edit_text(
                f"📝 <b>助手提示词</b>\n"
                f"<i>文件: {safe_text(PromptFileManager.get_path('assistant_prompt'))}</i>\n\n<pre>{preview}</pre>",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "view_global_prompt":
            curr = get_runtime_prompt('global_prompt_addon')
            preview = safe_text(curr)[:500] + "..." if len(curr) > 500 else safe_text(curr)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ 修改追加提示词", callback_data="act_modify_global_prompt")],
                [InlineKeyboardButton("🔄 从文件重载", callback_data="reset_global_prompt")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_prompts")]
            ])
            await query.message.edit_text(
                f"🌐 <b>全局追加提示词</b>\n"
                f"<i>文件: {safe_text(PromptFileManager.get_path('global_prompt_addon'))}</i>\n\n<pre>{preview}</pre>",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "view_agent_prompt":
            curr = PromptFileManager.get('agent_prompt_addon')
            preview = safe_text(curr)[:500] + "..." if len(curr) > 500 else safe_text(curr)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 从文件重载", callback_data="reload_agent_prompt")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_prompts")]
            ])
            await query.message.edit_text(
                f"🤖 <b>Agent模式提示词</b>\n"
                f"<i>文件: {safe_text(PromptFileManager.get_path('agent_prompt_addon'))}</i>\n"
                f"<i>(请直接编辑文件后点击重载)</i>\n\n<pre>{preview}</pre>",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "reload_agent_prompt":
            PromptFileManager.reload_all()
            await query.answer("✅ Agent提示词已从文件重载！", show_alert=True)
            # 重新显示
            curr = PromptFileManager.get('agent_prompt_addon')
            preview = safe_text(curr)[:500] + "..." if len(curr) > 500 else safe_text(curr)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 从文件重载", callback_data="reload_agent_prompt")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_prompts")]
            ])
            await query.message.edit_text(
                f"🤖 <b>Agent模式提示词 (已重载)</b>\n"
                f"<i>文件: {safe_text(PromptFileManager.get_path('agent_prompt_addon'))}</i>\n\n<pre>{preview}</pre>",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_reload_prompts":
            PromptFileManager.reload_all()
            await reload_runtime_prompt('assistant_prompt')
            await reload_runtime_prompt('global_prompt_addon')
            await GlobalRecorder.record_system_op("从文件重载所有提示词")
            prompt_lengths = ''.join(
                f"📝 {safe_text(PromptFileManager.get_label(key))}: {len(PromptFileManager.get(key))}字\n"
                for key in PromptFileManager.FILES
            )
            await query.message.edit_text(
                "✅ <b>所有提示词已从文件重新加载！</b>\n\n"
                f"{prompt_lengths}",
                reply_markup=get_prompts_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "act_modify_normal_prompt":
            UserDataManager.set('state', BotState.SET_PROMPT)
            UserDataManager.set('prompt_buffer', "")
            msg = (
                "📝 <b>修改 助手提示词</b>\n"
                "━━━━━━━━━━━━━━\n"
                "1️⃣ 发送 .txt / .md 文件\n"
                "2️⃣ 或直接发送文字（可多次发送）\n"
                "━━━━━━━━━━━━━━\n"
                "<i>发送 'cancel' 取消</i>"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 完成输入", callback_data="act_confirm_normal_prompt")]
            ])
            await query.message.reply_text(msg, reply_markup=kb, parse_mode=constants.ParseMode.HTML)

        elif data == "act_modify_global_prompt":
            UserDataManager.set('state', BotState.SET_GLOBAL_PROMPT)
            UserDataManager.set('prompt_buffer', "")
            msg = (
                "🌐 <b>修改全局追加提示词</b>\n"
                "━━━━━━━━━━━━━━\n"
                "1️⃣ 发送 .txt / .md 文件\n"
                "2️⃣ 或直接发送文字（可多次发送）\n"
                "━━━━━━━━━━━━━━\n"
                "<i>发送 'cancel' 取消</i>"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 完成输入", callback_data="act_confirm_global_prompt")]
            ])
            await query.message.reply_text(msg, reply_markup=kb, parse_mode=constants.ParseMode.HTML)

        elif data.startswith("act_confirm_prompt:"):
            key = data.split(":", 1)[1]
            if key not in PromptFileManager.FILES:
                await query.answer("提示词不存在", show_alert=True)
                return
            buffer = UserDataManager.get('prompt_buffer', "")
            if not buffer:
                await query.answer("⚠️ 还没有输入内容。", show_alert=True)
                return
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('editing_prompt_key', "")
            UserDataManager.set('prompt_buffer', "")
            if key in {'assistant_prompt', 'global_prompt_addon'}:
                await save_runtime_prompt(key, buffer)
            else:
                PromptFileManager.set(key, buffer)
            await GlobalRecorder.record_system_op(
                f"修改提示词: {PromptFileManager.get_label(key)}",
                {"length": len(buffer)}
            )
            await query.message.reply_text(
                f"✅ {safe_text(PromptFileManager.get_label(key))}已更新！共 {len(buffer)} 字。\n"
                f"<i>(已同步到 {safe_text(PromptFileManager.get_path(key))})</i>",
                reply_markup=get_prompts_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_confirm_normal_prompt":
            buffer = UserDataManager.get('prompt_buffer', "")
            if not buffer:
                await query.answer("⚠️ 还没有输入内容。", show_alert=True)
                return
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('prompt_buffer', "")
            await save_runtime_prompt('assistant_prompt', buffer)
            await GlobalRecorder.record_system_op("修改Bot提示词", {"length": len(buffer)})
            await query.message.reply_text(
                f"✅ 助手提示词已更新！共 {len(buffer)} 字。\n<i>(已同步到 {safe_text(PromptFileManager.get_path('assistant_prompt'))})</i>",
                reply_markup=get_main_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_confirm_global_prompt":
            buffer = UserDataManager.get('prompt_buffer', "")
            if not buffer:
                await query.answer("⚠️ 还没有输入内容。", show_alert=True)
                return
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('prompt_buffer', "")
            await save_runtime_prompt('global_prompt_addon', buffer)
            await GlobalRecorder.record_system_op("修改全局追加提示词", {"length": len(buffer)})
            await query.message.reply_text(
                f"✅ 全局追加提示词已更新！共 {len(buffer)} 字。\n<i>(已同步到 {safe_text(PromptFileManager.get_path('global_prompt_addon'))})</i>",
                reply_markup=get_main_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "reset_normal_prompt":
            PromptFileManager.reload_all()
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('prompt_buffer', "")
            await reload_runtime_prompt('assistant_prompt')
            await GlobalRecorder.record_system_op("从文件重载Bot提示词")
            await query.message.reply_text(
                "✅ 助手提示词已从文件重新加载。",
                reply_markup=get_main_menu()
            )

        elif data == "reset_global_prompt":
            PromptFileManager.reload_all()
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('prompt_buffer', "")
            await reload_runtime_prompt('global_prompt_addon')
            await GlobalRecorder.record_system_op("从文件重载全局追加提示词")
            await query.message.reply_text(
                "✅ 全局追加提示词已从文件重新加载。",
                reply_markup=get_main_menu()
            )
        
        # --- Provider 管理 ---
        elif data == "menu_providers":
            if UserDataManager.get('state') == BotState.IMPORT_PROVIDER_CONFIG:
                UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('provider_import_mode', None)
            await UserDataManager.reload_providers()
            await query.message.edit_text(
                "🔌 <b>提供商管理</b>\n\n"
                "这里管理连接信息，也管理每个提供商下面保存的模型列表。\n"
                "默认对话模型 / 媒体模型 请到【默认模型】里单独选择。",
                reply_markup=get_providers_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "export_provider_config":
            await send_provider_config_export(update, context)

        elif data == "import_provider_config":
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('provider_import_mode', None)
            await query.message.edit_text(
                "📥 <b>选择导入方式</b>\n\n"
                "➕ <b>合并导入</b>\n"
                "同名提供商覆盖，文件中没有的现有提供商继续保留。\n\n"
                "♻️ <b>覆盖导入</b>\n"
                "先删除全部现有提供商，再完全按导入文件重建。\n"
                "未包含在文件中的提供商会被删除。",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ 合并导入", callback_data="provider_import_mode_merge")],
                    [InlineKeyboardButton("♻️ 覆盖导入", callback_data="provider_import_mode_replace")],
                    [InlineKeyboardButton("🔙 返回", callback_data="menu_providers")]
                ])
            )

        elif data in {"provider_import_mode_merge", "provider_import_mode_replace"}:
            import_mode = 'replace' if data.endswith('_replace') else 'merge'
            UserDataManager.set('provider_import_mode', import_mode)
            UserDataManager.set('state', BotState.IMPORT_PROVIDER_CONFIG)
            mode_label = '覆盖导入' if import_mode == 'replace' else '合并导入'
            mode_note = (
                "⚠️ 覆盖导入会删除文件中没有的现有提供商。\n"
                if import_mode == 'replace'
                else "同名提供商会更新，其他现有提供商会保留。\n"
            )
            await query.message.edit_text(
                f"📥 <b>{mode_label}</b>\n\n"
                "请发送由本 Bot 导出的 <code>提供商配置-*.json</code> 文件，"
                "也可以直接粘贴完整 JSON。\n\n"
                f"{mode_note}"
                "只有有效的默认模型选择才会恢复。\n"
                "配置内含 API Key，请仅在私聊中操作。\n\n"
                "发送 <code>cancel</code> 可取消。",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 重新选择方式", callback_data="import_provider_config")],
                    [InlineKeyboardButton("取消导入", callback_data="menu_providers")]
                ])
            )

        elif data == "menu_default_models":
            await query.message.edit_text(
                "🎯 <b>默认模型</b>\n\n"
                f"💬 对话模型: <b>{safe_text(format_model_target_summary('chat'))}</b>\n"
                f"🖼️ 媒体模型: <b>{safe_text(format_model_target_summary('media'))}</b>\n\n"
                "这里只负责选择默认模型。\n"
                "新增 / 删除 / 联网获取模型，请去【提供商】里管理。",
                reply_markup=get_default_model_menu(),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "target_chat_models":
            UserDataManager.set('temp_model_target', 'chat')
            await query.message.edit_text(
                "💬 <b>选择默认对话模型</b>\n\n"
                f"当前设置: <b>{safe_text(format_model_target_summary('chat'))}</b>\n\n"
                "先挑一个提供商。",
                reply_markup=get_default_model_provider_menu('chat'),
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "target_media_models":
            UserDataManager.set('temp_model_target', 'media')
            await query.message.edit_text(
                "🖼️ <b>选择默认媒体模型</b>\n\n"
                f"当前设置: <b>{safe_text(format_model_target_summary('media'))}</b>\n\n"
                "先挑一个提供商。",
                reply_markup=get_default_model_provider_menu('media'),
                parse_mode=constants.ParseMode.HTML
            )

        elif data.startswith("pick_model_provider_"):
            _, _, _, target, provider_name = data.split("_", 4)
            providers = UserDataManager.get('providers', {})
            if provider_name not in providers:
                await query.answer("⚠️ 找不到这个提供商", show_alert=True)
                return
            kb = build_model_selection_keyboard(provider_name, target)
            await query.message.edit_text(
                f"📚 <b>{safe_text(provider_name)}</b> 的{safe_text(get_model_target_label(target))}\n\n"
                f"当前默认: <b>{safe_text(format_model_target_summary(target))}</b>\n"
                "这里只能选择已保存模型。\n"
                "如果要新增、删除或联网获取模型，请回【提供商】里管理。",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data.startswith("prov_models_"):
            name = data[len("prov_models_"):]
            providers = UserDataManager.get('providers', {})
            if name not in providers:
                await query.answer("⚠️ 找不到这个提供商", show_alert=True)
                return
            kb = build_saved_models_keyboard(name)
            await query.message.edit_text(
                f"🧰 <b>{safe_text(name)}</b> 的模型管理\n\n"
                "这里可以手写新增、联网获取、搜索，或点击模型进行设置。",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "act_add_provider":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Gemini (原生)", callback_data="quick_add_gemini")],
                [InlineKeyboardButton("🌐 Vertex (原生)", callback_data="quick_add_vertex")],
                [InlineKeyboardButton("🧠 OpenAI (官方)", callback_data="quick_add_openai")],
                [InlineKeyboardButton("🔧 OpenAI 兼容", callback_data="quick_add_custom")],
                [InlineKeyboardButton("💜 Claude (原生)", callback_data="quick_add_claude")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_providers")]
            ])
            await query.message.edit_text(
                "🔌 <b>添加提供商</b>\n\n"
                "选择接口模式：\n\n"
                "✨ <b>Gemini</b> — Google AI Studio\n"
                "<i>Gemini 原生格式，URL 自动填写</i>\n"
                "请求: <code>.../models/模型名:streamGenerateContent</code>\n\n"
                "🌐 <b>Vertex</b> — Google Cloud\n"
                "<i>Vertex 的 Gemini 原生格式，URL 自动填写</i>\n"
                "请求: <code>.../models/模型名:streamGenerateContent</code>\n\n"
                "🧠 <b>OpenAI</b> — 官方接口\n"
                "<i>OpenAI 官方格式，URL 自动填写</i>\n"
                "请求: <code>.../chat/completions</code>\n\n"
                "🔧 <b>OpenAI 兼容</b> — 手动填写\n"
                "<i>适合深求 / 魔塔社区 / 月之暗面等兼容接口</i>\n"
                "请求: <code>.../chat/completions</code>\n\n"
                "💜 <b>Claude</b> — Anthropic\n"
                "<i>Claude 原生格式，URL 自动填写</i>\n"
                "请求: <code>.../messages</code>",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "quick_add_gemini":
            UserDataManager.set('temp_prov_url', 'https://generativelanguage.googleapis.com/v1beta')
            UserDataManager.set('temp_prov_format', 'gemini')
            UserDataManager.set('state', BotState.ADD_PROV_NAME)
            await query.message.reply_text(
                "✨ <b>添加 Gemini 提供商 (原生格式)</b>\n\n"
                "URL 已自动设置为：\n"
                "<code>https://generativelanguage.googleapis.com/v1beta</code>\n\n"
                "实际请求路径：\n"
                "<code>.../models/gemini-2.5-flash:streamGenerateContent</code>\n\n"
                "API Key 获取: https://aistudio.google.com/apikey\n\n"
                "请输入一个名字（如: Gemini），最多20字符：",
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "quick_add_vertex":
            UserDataManager.set('temp_prov_url', 'https://aiplatform.googleapis.com/v1/publishers/google')
            UserDataManager.set('temp_prov_format', 'vertex')
            UserDataManager.set('state', BotState.ADD_PROV_NAME)
            await query.message.reply_text(
                "🌐 <b>添加 Vertex 提供商 (原生格式)</b>\n\n"
                "URL 已自动设置为：\n"
                "<code>https://aiplatform.googleapis.com/v1/publishers/google</code>\n\n"
                "实际请求路径：\n"
                "<code>.../models/gemini-2.5-flash:streamGenerateContent</code>\n\n"
                "请输入一个名字（如: Vertex），最多20字符：",
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "quick_add_openai":
            UserDataManager.set('temp_prov_url', 'https://api.openai.com/v1')
            UserDataManager.set('temp_prov_format', 'openai')
            UserDataManager.set('state', BotState.ADD_PROV_NAME)
            await query.message.reply_text(
                "🧠 <b>添加 OpenAI 提供商 (官方接口)</b>\n\n"
                "URL 已自动设置为：\n"
                "<code>https://api.openai.com/v1</code>\n\n"
                "实际请求路径：\n"
                "<code>.../chat/completions</code>\n\n"
                "请输入一个名字（如: OpenAI），最多20字符：",
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "quick_add_claude":
            UserDataManager.set('temp_prov_url', 'https://api.anthropic.com/v1')
            UserDataManager.set('temp_prov_format', 'claude')
            UserDataManager.set('state', BotState.ADD_PROV_NAME)
            await query.message.reply_text(
                "💜 <b>添加 Claude 提供商</b>\n\n"
                "URL 已自动设置为：\n"
                "<code>https://api.anthropic.com/v1</code>\n\n"
                "实际请求路径：\n"
                "<code>https://api.anthropic.com/v1/messages</code>\n\n"
                "请输入一个名字（如: Claude），最多20字符：",
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "quick_add_custom":
            UserDataManager.set('temp_prov_url', None)
            UserDataManager.set('temp_prov_format', 'openai_compatible')
            UserDataManager.set('state', BotState.ADD_PROV_NAME)
            await query.message.reply_text(
                "🔧 <b>添加 OpenAI 兼容提供商</b>\n\n"
                "请输入一个名字（如: 深求），最多20字符：",
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data.startswith("view_prov_"):
            name = data.split("_", 2)[2]
            providers = UserDataManager.get('providers', {})
            if name not in providers:
                await query.answer("⚠️ 找不到这个Provider", show_alert=True)
                return
            prov = providers[name]
            masked_key = prov['api_key'][:4] + "..." + prov['api_key'][-4:] if len(prov['api_key']) > 8 else "***"
            format_label = get_provider_mode_label(prov.get('api_format', 'openai'), prov.get('base_url', ''))
            platform_hint = get_provider_platform_hint(prov.get('api_format', 'openai'), prov.get('base_url', ''))
            request_hint = get_provider_request_hint(prov.get('api_format', 'openai'), prov.get('base_url', ''))
            info = (
                f"🏢 <b>{safe_text(name)}</b>\n"
                f"📌 模式: {safe_text(format_label)}\n"
                f"🧭 说明: {safe_text(platform_hint)}\n"
                f"🔗 Base URL: <code>{safe_text(prov['base_url'])}</code>\n"
                f"📨 请求形式: <code>{safe_text(request_hint)}</code>\n"
                f"🔑 API Key: {safe_text(masked_key)}"
            )
            await query.message.edit_text(
                info,
                reply_markup=get_provider_detail_menu(name),
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data.startswith("del_prov_"):
            name = data.split("_", 2)[2]
            providers = UserDataManager.get('providers', {})
            if name in providers:
                del providers[name]
                db = await BotMemoryDB.get_instance()
                await db.delete_provider(name)
                PortalManager.remove_portal(name)
                await UserDataManager.reload_providers()
                await GlobalRecorder.record_system_op(f"删除Provider: {name}")
            
            if UserDataManager.get('active_provider_key') == name:
                UserDataManager.set('active_provider_key', None)
                UserDataManager.set('default_model', None)
                await UserDataManager.save_config('active_provider', None)
                await UserDataManager.save_config('default_model', None)

            if UserDataManager.get('default_media_provider_key') == name:
                UserDataManager.set('default_media_provider_key', None)
                UserDataManager.set('default_media_model', None)
                await UserDataManager.save_config('default_media_provider', None)
                await UserDataManager.save_config('default_media_model', None)
            
            await query.message.edit_text(
                f"🗑️ 已删除 {safe_text(name)}，相关默认模型也一起解绑了。",
                reply_markup=get_providers_menu()
            )
        
        elif data.startswith("edit_pname_"):
            provider_name = data[len("edit_pname_"):]
            if provider_name not in (UserDataManager.get('providers', {}) or {}):
                await query.answer("⚠️ 找不到这个提供商", show_alert=True)
                return
            UserDataManager.set('editing_provider', provider_name)
            UserDataManager.set('state', BotState.EDIT_PROV_NAME)
            await query.message.reply_text(
                "✏️ 请输入新的提供商名称（最多 20 个字符，或发送 cancel）："
            )

        elif data.startswith("edit_pkey_"):
            UserDataManager.set('editing_provider', data.split("_", 2)[2])
            UserDataManager.set('state', BotState.EDIT_PROV_KEY)
            await query.message.reply_text("🔑 请发送新的 Key  (或 'cancel')\n💡 支持多个 Key，用英文逗号隔开即可轮询调用，空格会被忽略。")
        
        elif data.startswith("edit_purl_"):
            UserDataManager.set('editing_provider', data.split("_", 2)[2])
            UserDataManager.set('state', BotState.EDIT_PROV_URL)
            await query.message.reply_text("🔗 请发送新的 URL 地址 (或 'cancel'):")
        
        elif data.startswith("mng_saved_"):
            name = data.split("_", 2)[2]
            providers = UserDataManager.get('providers', {})
            if name not in providers:
                return
            kb = build_saved_models_keyboard(name)
            await query.message.edit_text(
                f"🧰 <b>{safe_text(name)}</b> 已保存的模型\n\n"
                "这里可以继续新增、联网获取、搜索，或点击模型进行设置。",
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data.startswith("act_manual_mod_"):
            UserDataManager.set('editing_provider', data.split("_", 3)[3])
            UserDataManager.set('state', BotState.ADD_MODEL_MANUAL)
            await query.message.reply_text(
                "✍️ <b>手动添加模型</b>\n\n"
                "请输入模型代号，单个或批量都可以。\n"
                "批量输入时，用英文逗号 <code>,</code> 断开。\n\n"
                "例：\n"
                "<code>gpt-4.1,gpt-4.1-mini,gpt-4.1-nano</code>\n\n"
                "取消请输入 <code>cancel</code>。",
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data.startswith("act_saved_"):
            content = data[len("act_saved_"):]
            prov_name = UserDataManager.get('temp_viewing_prov')
            if prov_name and content.startswith(prov_name + "_"):
                model_name = content[len(prov_name)+1:]
            else:
                model_name = content

            detail_back_callback = (
                "back_saved_models"
                if UserDataManager.get('temp_saved_filter')
                else f"mng_saved_{prov_name}"
            )
            detail_text, detail_kb = build_model_detail_menu(
                prov_name,
                model_name,
                back_callback=detail_back_callback
            )
            await query.message.edit_text(
                detail_text,
                reply_markup=detail_kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data.startswith("pick_default_"):
            model_name = data[len("pick_default_"):]
            prov_name = UserDataManager.get('temp_viewing_prov')
            target = UserDataManager.get('temp_model_target') or 'chat'
            if not prov_name:
                await query.answer("⚠️ 当前没有选中的提供商", show_alert=True)
                return
            await save_model_target_selection(target, prov_name, model_name)

            cid = UserDataManager.get('current_chat_id')
            if target == 'chat' and cid:
                db = await BotMemoryDB.get_instance()
                await db.update_session(cid, model=model_name)

            await GlobalRecorder.record_system_op(
                f"设置{get_model_target_label(target)}: {model_name}",
                {"provider": prov_name, "target": target}
            )

            await query.message.reply_text(
                f"✅ {get_model_target_label(target)} 已切换为 <b>{safe_text(prov_name)} / {safe_text(model_name)}</b>。",
                reply_markup=get_default_model_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data.startswith("set_mdl|"):
            _, target, prov_name, model_name = data.split("|", 3)
            await save_model_target_selection(target, prov_name, model_name)

            cid = UserDataManager.get('current_chat_id')
            if target == 'chat' and cid:
                db = await BotMemoryDB.get_instance()
                await db.update_session(cid, model=model_name)

            await GlobalRecorder.record_system_op(
                f"设置{get_model_target_label(target)}: {model_name}",
                {"provider": prov_name, "target": target}
            )

            target_label = get_model_target_label(target)
            await query.answer(f"✅ 已设为{target_label}")
            # 若详情页是从联网获取列表进入的，操作完成后回到联网列表，
            # 保留当前页码/搜索条件，而不是跳到已保存模型列表。
            if (
                UserDataManager.get('temp_list_type') == 'fetched'
                and UserDataManager.get('temp_viewing_prov') == prov_name
                and UserDataManager.get('fetched_cache')
            ):
                title, kb = build_fetched_models_view(prov_name)
                await query.message.edit_text(
                    f"✅ <b>{safe_text(model_name)}</b> 已设为{target_label}！\n\n{title}",
                    reply_markup=kb,
                    parse_mode=constants.ParseMode.HTML
                )
            else:
                kb = build_saved_models_keyboard(prov_name)
                await query.message.edit_text(
                    f"✅ <b>{safe_text(model_name)}</b> 已设为{target_label}！\n\n"
                    f"🧰 <b>{safe_text(prov_name)}</b> 已保存的模型\n\n"
                    "这里可以继续新增、联网获取、搜索，或点击模型进行设置。",
                    reply_markup=kb,
                    parse_mode=constants.ParseMode.HTML
                )

        elif data.startswith("do_use|") or data.startswith("do_use_"):
            if data.startswith("do_use|"):
                _, target, prov_name, model_name = data.split("|", 3)
            else:
                parts = data.split("_", 4)
                target, prov_name, model_name = parts[2], parts[3], parts[4]
            await save_model_target_selection(target, prov_name, model_name)
            
            cid = UserDataManager.get('current_chat_id')
            if target == 'chat' and cid:
                db = await BotMemoryDB.get_instance()
                await db.update_session(cid, model=model_name)
            
            await GlobalRecorder.record_system_op(
                f"设置{get_model_target_label(target)}: {model_name}",
                {"provider": prov_name, "target": target}
            )
            
            await query.message.reply_text(
                f"✅ {get_model_target_label(target)} 已切换为 <b>{safe_text(prov_name)} / {safe_text(model_name)}</b>。",
                reply_markup=get_default_model_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data.startswith("do_del|") or data.startswith("do_del_"):
            if data.startswith("do_del|"):
                _, pname, mname = data.split("|", 2)
            else:
                parts = data.split("_", 3)
                pname, mname = parts[2], parts[3]
            providers = UserDataManager.get('providers', {})
            if pname in providers and mname in providers[pname].get('models', []):
                providers[pname]['models'].remove(mname)
                db = await BotMemoryDB.get_instance()
                await db.update_provider_models(pname, providers[pname]['models'])
                await GlobalRecorder.record_system_op(f"删除模型: {mname}", {"provider": pname})
            await query.answer(f"🗑️ 已删除 {mname}")
            # 同上：从联网获取列表进入的详情页，删除后回到联网列表。
            if (
                UserDataManager.get('temp_list_type') == 'fetched'
                and UserDataManager.get('temp_viewing_prov') == pname
                and UserDataManager.get('fetched_cache')
            ):
                title, kb = build_fetched_models_view(pname)
                await query.message.edit_text(
                    f"🗑️ <b>{safe_text(mname)}</b> 已从模型列表中删除！\n\n{title}",
                    reply_markup=kb,
                    parse_mode=constants.ParseMode.HTML
                )
            else:
                kb = build_saved_models_keyboard(pname)
                await query.message.edit_text(
                    f"🗑️ <b>{safe_text(mname)}</b> 已从模型列表中删除！\n\n"
                    f"🧰 <b>{safe_text(pname)}</b> 已保存的模型\n\n"
                    "这里可以继续新增、联网获取、搜索，或点击模型进行设置。",
                    reply_markup=kb,
                    parse_mode=constants.ParseMode.HTML
                )
        
        elif data.startswith("fetch_market_"):
            name = data.split("_", 2)[2]
            providers = UserDataManager.get('providers', {})
            if name not in providers:
                return
            prov = providers[name]
            UserDataManager.set('temp_viewing_prov', name)
            target = UserDataManager.get('temp_model_target') or 'chat'
            menu_mode = UserDataManager.get('temp_model_menu_mode') or 'manage'
            await query.message.reply_text("⏳ 正在获取模型列表...")
            models, fetch_error = await ModelClient.fetch_knowledge_detailed(
                name,
                prov['api_key'],
                prov['base_url'],
                api_format=prov.get('api_format', 'openai')
            )
            if not models:
                if fetch_error:
                    await query.message.reply_text(f"⚠️ 模型列表获取失败。\n\n{fetch_error}")
                else:
                    await query.message.reply_text("⚠️ 接口请求成功，但没有返回可用的生成模型。")
                return
            UserDataManager.set('fetched_cache', models)
            UserDataManager.set('temp_page', 1)
            UserDataManager.set('temp_filter', None)
            UserDataManager.set('temp_list_type', 'fetched')
            back_callback = f"mng_saved_{name}" if menu_mode == 'manage' else f"target_{target}_models"
            UserDataManager.set('temp_back_callback', back_callback)
            title, kb = build_fetched_models_view(name)
            await query.message.reply_text(title, reply_markup=kb)
        
        elif data.startswith("pick_fetch_"):
            mname = data[len("pick_fetch_"):]
            pname = UserDataManager.get('temp_viewing_prov')
            target = UserDataManager.get('temp_model_target') or 'chat'
            menu_mode = UserDataManager.get('temp_model_menu_mode') or 'manage'
            providers = UserDataManager.get('providers', {})
            if pname and pname in providers:
                if 'models' not in providers[pname]:
                    providers[pname]['models'] = []
                if mname not in providers[pname]['models']:
                    providers[pname]['models'].append(mname)
                    db = await BotMemoryDB.get_instance()
                    await db.update_provider_models(pname, providers[pname]['models'])
                    await GlobalRecorder.record_system_op(f"添加模型: {mname}", {"provider": pname})
                    await query.answer(f"✅ 已保存。{mname}", show_alert=False)
                else:
                    await query.answer("⚠️ 该模型已存在", show_alert=False)
                if menu_mode == 'manage':
                    # 从联网获取列表进入详情时，返回按钮应回到缓存的获取结果，
                    # 而不是跳到已保存模型列表并丢失当前页码/搜索条件。
                    detail_text, detail_kb = build_model_detail_menu(
                        pname,
                        mname,
                        back_callback="back_fetched_models"
                    )
                    await query.message.edit_text(
                        f"✅ 模型已保存。\n\n{detail_text}",
                        reply_markup=detail_kb,
                        parse_mode=constants.ParseMode.HTML
                    )
                else:
                    await save_model_target_selection(target, pname, mname)
                    cid = UserDataManager.get('current_chat_id')
                    if target == 'chat' and cid:
                        db = await BotMemoryDB.get_instance()
                        await db.update_session(cid, model=mname)
                    await GlobalRecorder.record_system_op(
                        f"设置{get_model_target_label(target)}: {mname}",
                        {"provider": pname, "target": target}
                    )
                    await query.message.reply_text(
                        f"✅ {get_model_target_label(target)} 已切换为 <b>{safe_text(pname)} / {safe_text(mname)}</b>。",
                        reply_markup=get_default_model_menu(),
                        parse_mode=constants.ParseMode.HTML
                    )
        
        elif data == "back_saved_models":
            pname = UserDataManager.get('temp_viewing_prov')
            providers = UserDataManager.get('providers', {})
            if not pname or pname not in providers:
                await query.message.edit_text(
                    "⚠️ 已保存模型列表已失效，请重新选择提供商。",
                    reply_markup=get_providers_menu()
                )
                return
            UserDataManager.set('temp_list_type', 'saved')
            UserDataManager.set('temp_model_menu_mode', 'manage')
            title, kb = build_saved_models_view(pname)
            await query.message.edit_text(
                title,
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "back_saved_all_models":
            pname = UserDataManager.get('temp_viewing_prov')
            providers = UserDataManager.get('providers', {})
            if not pname or pname not in providers:
                await query.message.edit_text(
                    "⚠️ 已保存模型列表已失效，请重新选择提供商。",
                    reply_markup=get_providers_menu()
                )
                return
            UserDataManager.set('temp_saved_filter', None)
            UserDataManager.set('temp_page', 1)
            UserDataManager.set('temp_list_type', 'saved')
            UserDataManager.set('temp_model_menu_mode', 'manage')
            title, kb = build_saved_models_view(pname)
            await query.message.edit_text(
                title,
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "back_fetched_models":
            pname = UserDataManager.get('temp_viewing_prov')
            models = UserDataManager.get('fetched_cache', [])
            if not pname or not models:
                if pname:
                    kb = build_saved_models_keyboard(pname)
                    fallback_text = (
                        f"🧰 <b>{safe_text(pname)}</b> 已保存的模型\n\n"
                        "⚠️ 获取结果已失效，请点击【⚡ 联网获取】重新拉取。"
                    )
                else:
                    kb = get_providers_menu()
                    fallback_text = "⚠️ 获取结果已失效，请重新选择提供商并联网获取。"
                await query.message.edit_text(
                    fallback_text,
                    reply_markup=kb,
                    parse_mode=constants.ParseMode.HTML
                )
                return

            UserDataManager.set('temp_list_type', 'fetched')
            title, kb = build_fetched_models_view(pname)
            await query.message.edit_text(
                title,
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "back_fetched_all_models":
            pname = UserDataManager.get('temp_viewing_prov')
            models = UserDataManager.get('fetched_cache', [])
            if not pname or not models:
                if pname:
                    kb = build_saved_models_keyboard(pname)
                    fallback_text = (
                        f"🧰 <b>{safe_text(pname)}</b> 已保存的模型\n\n"
                        "⚠️ 获取结果已失效，请点击【⚡ 联网获取】重新拉取。"
                    )
                else:
                    kb = get_providers_menu()
                    fallback_text = "⚠️ 获取结果已失效，请重新选择提供商并联网获取。"
                await query.message.edit_text(
                    fallback_text,
                    reply_markup=kb,
                    parse_mode=constants.ParseMode.HTML
                )
                return

            UserDataManager.set('temp_filter', None)
            UserDataManager.set('temp_page', 1)
            UserDataManager.set('temp_list_type', 'fetched')
            title, kb = build_fetched_models_view(pname)
            await query.message.edit_text(
                title,
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )

        elif data == "act_search_saved":
            UserDataManager.set('state', BotState.SEARCH_SAVED)
            await query.message.reply_text(
                "🔍 请输入要搜索的已保存模型名称 (或 'cancel'):"
            )

        elif data == "act_search_fetched":
            UserDataManager.set('state', BotState.SEARCH_FETCHED)
            await query.message.reply_text(
                "🔍 请输入搜索的内容 (或 'cancel'):"
            )
        
        elif data.startswith("page_"):
            parts = data.split("_")
            page = int(parts[1])
            prefix = "_".join(parts[2:])
            UserDataManager.set('temp_page', page)
            pname = UserDataManager.get('temp_viewing_prov')
            providers = UserDataManager.get('providers', {})
            
            if UserDataManager.get('temp_list_type') == 'saved':
                items = providers.get(pname, {}).get('models', [])
                menu_mode = UserDataManager.get('temp_model_menu_mode') or 'manage'
                if menu_mode == 'manage':
                    _, kb = build_saved_models_view(pname)
                else:
                    target = UserDataManager.get('temp_model_target') or 'chat'
                    back_callback = f"target_{target}_models"
                    kb = build_magic_keyboard(
                        items, page, prefix, back_callback,
                        marker_fn=make_select_marker_fn(target, pname)
                    )
            else:
                _, kb = build_fetched_models_view(pname)
            try:
                await query.message.edit_reply_markup(kb)
            except Exception as e:
                logger.warning(f"翻页更新失败: {e}")
        
        # --- 聊天功能 ---
        elif data == "cmd_delete":
            db = await BotMemoryDB.get_instance()
            global_count = len(await db.get_global_messages(10000))
            mirror_count = len(await db.get_chat_messages(SINGLE_MEMORY_SESSION_ID))
            await query.message.edit_text(
                "⚠️ <b>确认清空全局记忆吗？</b>\n\n"
                f"这会删除当前所有对话记忆。\n"
                f"🌐 全局记忆记录: <b>{global_count}</b> 条\n"
                f"🪞 内部镜像消息: <b>{mirror_count}</b> 条\n\n"
                "不会删除 Provider 配置、.env、提示词文件。",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🧹 确认清空", callback_data="confirm_clear_memory")],
                    [InlineKeyboardButton("🔙 返回主菜单", callback_data="act_main_menu")]
                ])
            )
        elif data == "cmd_info":
            await cmd_show_info(update, context)
        elif data == "cmd_export_all":
            await cmd_export_all(update, context)
        elif data == "cmd_update":
            await cmd_update_system(update, context)
        elif data == "cmd_restart":
            await cmd_restart_system(update, context)
        elif data == "confirm_clear_memory":
            await cmd_delete_chat(update, context)
        elif data in {"cmd_new_chat", "cmd_save", "cmd_list_chats", "cmd_rename_chat"} or data.startswith("load_chat_"):
            await query.answer("现在只有一份全局记忆，不再支持分段管理。", show_alert=True)
        else:
            # 没有任何分支命中。最常见的原因是重启后旧按钮里的短 ID 已经从
            # 内存映射表里消失（CallbackDataStore 是纯内存的）。以前这里
            # 直接静默结束：转圈已经被 query.answer() 清掉了，用户看到的是
            # 一个"点了但什么都没发生"的按钮。
            logger.warning(f"未识别的 callback data: {data!r}")
            await query.answer(
                "这个按钮已失效（可能是重启前的旧消息）。请重新打开菜单。",
                show_alert=True,
            )

    except Exception as e:
        logger.error(f"Callback Error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("操作失败，请稍后重试。")

# --- ☆ 文档消息处理 ☆ ---
