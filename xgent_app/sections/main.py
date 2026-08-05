# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

if __name__ == '__main__':
    try:
        print("=" * 60)
        print("XGent for Telegram starting...")
        if BotConfig.API_BASE_URL:
            print(f"Using LOCAL Telegram Bot API: {BotConfig.API_BASE_URL}")
        print("=" * 60)
        
        # 创建应用
        _app_builder = (
            Application.builder()
            .token(BotConfig.TOKEN)
            .post_init(setup_bot_commands)
            .read_timeout(30)
            .write_timeout(30)
            .connect_timeout(15)
            .pool_timeout(10)
            .concurrent_updates(True)
        )
        if BotConfig.API_BASE_URL:
            # 走本地 Telegram Bot API server：base_url / base_file_url 同源，开 local_mode
            # 注意：PTB v20 会自动在 base_url 后追加 token，这里只给前缀，不要带 token
            _app_builder = _app_builder.base_url(
                f"{BotConfig.API_BASE_URL}/bot"
            )
            _app_builder = _app_builder.base_file_url(
                f"{BotConfig.API_BASE_URL}/file/bot"
            )
            _app_builder = _app_builder.local_mode(True)
        app = _app_builder.build()
        
        # 注册关闭钩子 - 正确清理数据库连接
        app.post_shutdown = on_shutdown
        
        # 添加任务调度器 - 每小时检查一次是否需要发送提醒消息
        job_queue = app.job_queue
        if job_queue is None:
            raise RuntimeError(
                "JobQueue 不可用：请确认安装了 python-telegram-bot[job-queue]"
            )
        job_queue.run_repeating(check_and_send_idle_message, interval=3600, first=60)
        # 注意：不再注册 send_startup_menu 兜底任务。
        # post_init 已保证启动菜单发送且 flag 不回滚，兜底任务只会制造重复发送风险。
        
        # 注册命令。CommandHandler 必须放在兜底 MessageHandler 前面，否则命令会被普通消息处理器吃掉。
        app.add_handler(CommandHandler("start", cmd_start))
        commands = [
            ("config", cmd_settings_menu),
            ("update", cmd_update_system),
            ("restart", cmd_restart_system),
            ("providers", cmd_providers_menu),
            ("provider_config", cmd_provider_config),
            ("models", cmd_models_menu),
            ("chat_model", cmd_chat_model_menu),
            ("media_model", cmd_media_model_menu),
            ("prompts", cmd_prompts_menu),
            ("clear_memory", cmd_delete_chat),
            ("depth", cmd_depth_menu),
            ("timeout", cmd_timeout_menu),
            ("agent", cmd_toggle_agent),
            ("blacklist", cmd_blacklist_menu),
            ("stream", cmd_toggle_stream),
            ("status", cmd_show_info),
            ("export", cmd_export_all),
            ("show_chat_info", cmd_show_info),
        ]
        for cmd, handler in commands:
            app.add_handler(CommandHandler(cmd, handler))

        app.add_handler(CallbackQueryHandler(handle_button_click))
        app.add_handler(MessageHandler(
            filters.Regex(r"^/(?:黑名单|blacklist)(?:@\w+)?(?:\s|$)"),
            cmd_blacklist_menu
        ))
        
        # 文件处理器
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))
        
        # 图片处理器
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
        
        # 贴纸处理器
        app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker_message))
        
        # 文本处理器（含转发消息——转发消息和普通消息完全一样处理）
        app.add_handler(MessageHandler((filters.TEXT | filters.FORWARDED) & ~filters.COMMAND, handle_text_message))

        # 其他类型消息（排除转发、文本、文件/图片/贴纸）
        app.add_handler(MessageHandler(
            filters.ALL & ~filters.COMMAND & ~filters.TEXT & ~filters.FORWARDED & ~filters.Document.ALL & ~filters.PHOTO & ~filters.Sticker.ALL,
            handle_other_message
        ))
        
        app.add_error_handler(global_error_handler)
        
        logger.info("=" * 50)
        logger.info("XGent for Telegram ready.")
        logger.info("Features: Async SQLite | Fast Stream | Correct Storage")
        logger.info("=" * 50)
        
        app.run_polling()

    except InvalidToken:
        logger.critical("Telegram Bot Token 无效或已失效，请检查 .env 中的 BOT_TOKEN。")
        logger.critical("请到 BotFather 重新生成 Token，更新 .env 后再启动。")
        sys.exit(78)

    except Exception as e:
        safe_error = redact_sensitive_text(str(e))
        logger.critical(f"Fatal Error: {safe_error}")
        print(redact_sensitive_text(traceback.format_exc()), file=sys.stderr)
        sys.exit(1)
