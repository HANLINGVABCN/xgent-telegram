# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

async def check_and_send_idle_message(context: ContextTypes.DEFAULT_TYPE):
    """检查是否需要发送提醒消息"""
    try:
        await UserDataManager.init()

        db = await BotMemoryDB.get_instance()
        
        idle_interval = normalize_idle_message_interval(
            UserDataManager.get('idle_message_interval', DEFAULT_IDLE_MESSAGE_INTERVAL)
        )
        if idle_interval <= 0:
            return

        # 获取用户最后发消息的时间
        last_time = await db.get_last_user_message_time()
        if not last_time:
            return
        
        # 检查是否超过配置的空闲提醒间隔
        hours_passed = (time.time() - last_time) / 3600
        if time.time() - last_time < idle_interval:
            return
        
        # 同一个间隔内最多发一次
        last_idle_notice_time = await db.get_config('last_idle_notice_time', 0)
        if time.time() - last_idle_notice_time < idle_interval:
            return
        
        # 获取Provider
        prov_name, prov_data = get_current_provider()
        if not prov_data:
            return
        
        model = UserDataManager.get('default_model')
        if not model:
            return
        if prov_name is None:
            logger.warning("空闲提醒跳过：当前 provider 名称为空")
            return

        # 获取全局对话记忆
        global_depth = max(1, int(UserDataManager.get('global_depth', 30)))
        global_history = await db.get_conversation_messages(global_depth)

        agent_mode = UserDataManager.get('agent_mode', False)
        idle_prompt = (
            build_conversation_system_prompt(agent_mode) +
            format_prompt_template('idle_message_prompt', hours_passed=int(hours_passed))
        )

        # 生成提醒消息。必须有硬上限：AI 回复超时设为“不限”时底层 HTTP 也不会
        # 超时，提供商静默挂起会让这个后台任务永远卡住，之后的空闲提醒全部停摆。
        try:
            response, error = await asyncio.wait_for(
                ModelClient.think_and_reply(
                    prov_name, get_next_api_key(prov_name, prov_data['api_key']), prov_data['base_url'],
                    model, idle_prompt, global_history,
                    api_format=prov_data.get('api_format', 'openai')
                ),
                timeout=IDLE_MESSAGE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"空闲提醒生成超时（>{int(IDLE_MESSAGE_TIMEOUT_SECONDS)}s）: "
                f"provider={prov_name}, model={model}"
            )
            return

        response_text = (response or "").strip()
        if not response_text:
            logger.warning(
                f"空闲提醒生成空内容: provider={prov_name}, model={model}, "
                f"error={redact_sensitive_text(error or '')}"
            )
            return
        
        if response_text:
            # 发送给用户
            idle_message = f"系统提醒\n\n{response_text}"
            idle_chunks = split_text_for_telegram(idle_message)
            for idle_chunk in idle_chunks:
                await context.bot.send_message(
                    chat_id=BotConfig.AUTHORIZED_USER_ID,
                    text=idle_chunk
                )
            
            # 记录发送时间
            await db.set_config('last_idle_notice_time', time.time())
            
            # 记录到全局消息
            await GlobalRecorder.record_ai_reply(f"[空闲提醒] {response_text}")
            
            logger.info("已发送提醒消息给用户")
    
    except Exception as e:
        logger.error(f"发送提醒消息失败: {e}")


# --- ☆ Web Chat 运行时接线 ☆ ---
# 放在这里是因为要同时用到 messages.py 的 process_conversation（更早加载）
# 和被 lifecycle.py 调用（更晚加载）。

_web_chat_server: Optional[Any] = None
# 真实 PTB bot 引用。网页发起对话时，MirrorBot 用它把消息同时投递到 Telegram。
_web_real_bot: Optional[Any] = None

# 网页可改的参数白名单。刻意不含提供商增删改和 API Key——那些留在 Telegram 里。
WEB_EDITABLE_SETTINGS = {
    'thinking_level', 'stream_mode', 'agent_mode', 'text_stitch_mode',
    'global_depth', 'agent_max_iterations', 'stream_timeout', 'chat_model',
}


async def _web_read_history(limit: int) -> List[Dict[str, Any]]:
    db = await BotMemoryDB.get_instance()
    rows = await db.get_conversation_messages(limit)
    return [
        {'role': str(row.get('role') or 'user'), 'content': str(row.get('content') or '')}
        for row in rows
    ]


async def _web_read_settings() -> Dict[str, Any]:
    """返回当前值 + 下拉框选项，供网页渲染设置面板。"""
    await UserDataManager.init()
    providers = UserDataManager.get('providers', {}) or {}
    prov_name = get_model_target_provider_name('chat')
    model_options = []
    for name, data in providers.items():
        for model in (data.get('models') or []):
            model_options.append({'value': f"{name}|{model}", 'label': f"{name} / {model}"})

    current_model = UserDataManager.get('default_model') or ''
    return {
        'values': {
            'thinking_level': normalize_thinking_level(UserDataManager.get('thinking_level')),
            'stream_mode': normalize_bool(UserDataManager.get('stream_mode', True), True),
            'agent_mode': bool(UserDataManager.get('agent_mode', False)),
            'text_stitch_mode': normalize_text_stitch_mode(UserDataManager.get('text_stitch_mode')),
            'global_depth': int(UserDataManager.get('global_depth', 30) or 30),
            'agent_max_iterations': normalize_agent_max_iterations(
                UserDataManager.get('agent_max_iterations', DEFAULT_AGENT_MAX_ITERATIONS)
            ),
            'stream_timeout': int(normalize_stream_timeout(UserDataManager.get('stream_timeout', 0))),
            'chat_model': f"{prov_name}|{current_model}" if prov_name and current_model else '',
        },
        'options': {
            'thinking_level': [
                {'value': level, 'label': THINKING_LEVEL_LABELS[level]}
                for level in THINKING_LEVEL_ORDER
            ],
            'text_stitch_mode': [
                {'value': TEXT_STITCH_MODE_AUTO, 'label': '自动判断'},
                {'value': TEXT_STITCH_MODE_FORCE, 'label': '强制拼接'},
                {'value': TEXT_STITCH_MODE_OFF, 'label': '不拼接'},
            ],
            'chat_model': model_options,
        },
    }


async def _web_write_setting(key: str, value: Any) -> Dict[str, Any]:
    """写入前一律过 normalize_*，与 Telegram 菜单走同一套校验。"""
    if key not in WEB_EDITABLE_SETTINGS:
        raise ValueError(f"不可修改的配置项: {key}")

    if key == 'chat_model':
        raw = str(value or '')
        prov_name, _, model = raw.partition('|')
        providers = UserDataManager.get('providers', {}) or {}
        if prov_name not in providers or model not in (providers[prov_name].get('models') or []):
            raise ValueError("提供商或模型不存在")
        await save_model_target_selection('chat', prov_name, model)
    elif key == 'thinking_level':
        level = normalize_thinking_level(value)
        UserDataManager.set(key, level)
        await UserDataManager.save_config(key, level)
        ModelClient._thinking_unsupported.clear()
    elif key in {'stream_mode', 'agent_mode'}:
        flag = normalize_bool(value, False)
        UserDataManager.set(key, flag)
        await UserDataManager.save_config(key, flag)
    elif key == 'text_stitch_mode':
        mode = normalize_text_stitch_mode(value)
        UserDataManager.set(key, mode)
        await UserDataManager.save_config(key, mode)
    elif key == 'global_depth':
        try:
            depth = int(value)
        except (TypeError, ValueError):
            raise ValueError("记忆深度必须是数字")
        if not (1 <= depth <= 500):
            raise ValueError("记忆深度需在 1-500 之间")
        UserDataManager.set(key, depth)
        await UserDataManager.save_config(key, depth)
    elif key == 'agent_max_iterations':
        iterations = normalize_agent_max_iterations(value)
        UserDataManager.set(key, iterations)
        await UserDataManager.save_config(key, iterations)
    elif key == 'stream_timeout':
        timeout = normalize_stream_timeout(value)
        UserDataManager.set(key, timeout)
        await UserDataManager.save_config(key, timeout)
        PortalManager._portals.clear()

    await GlobalRecorder.record_system_op(f"[Web] 修改配置 {key}", {"key": key})
    return await _web_read_settings()


async def _web_run_conversation(text: str, outbox: Any) -> None:
    """跑一轮完整对话，结束后给网页发一帧收尾信号。

    用 MirrorBot 而不是纯 WebBot：网页发的对话会同时投递到 Telegram，让两端
    保持同步。用户消息本身也单独发一条到 Telegram（标记来自网页），这样 TG
    那边能看到"网页问了什么"，而不只是 AI 的回复。
    """
    update, context, _bot = build_web_mirror_objects(
        BotConfig.AUTHORIZED_USER_ID, outbox, _web_real_bot,
    )
    try:
        # 先把用户消息镜像到 Telegram，再记录到记忆、跑对话。
        if _web_real_bot is not None:
            with contextlib.suppress(Exception):
                await _web_real_bot.send_message(
                    chat_id=BotConfig.AUTHORIZED_USER_ID,
                    text=f"💬 [网页]\n{text}",
                )
        await GlobalRecorder.record_user_message(
            text, MessageType.USER_TEXT, BotConfig.AUTHORIZED_USER_ID
        )
        await process_conversation(update, context, text)
        outbox.put({"type": "turn_end"})
    except Exception as e:
        logger.exception("Web 对话失败")
        outbox.put({"type": "turn_error", "text": redact_sensitive_text(str(e))[:300]})


async def _web_handle_callback(callback_data: str, message_id: int, outbox: Any) -> None:
    """网页按钮点击：复用 Telegram 的 handle_button_click 回调路由（纯网页，不回灌 TG）。"""
    update, context, _bot = build_web_callback_objects(
        BotConfig.AUTHORIZED_USER_ID, outbox, callback_data, message_id,
    )
    try:
        await handle_button_click(update, context)
        outbox.put({"type": "callback_done"})
    except Exception as e:
        logger.exception("Web 回调失败")
        outbox.put({"type": "turn_error", "text": redact_sensitive_text(str(e))[:300]})


async def _web_handle_command(command: str, outbox: Any) -> None:
    """网页 /命令：路由到对应的 cmd_* 处理函数（纯网页，不回灌 TG）。

    命令和按钮属于 UI 交互，不需要镜像到 Telegram；真正影响同步的是普通对话，
    那条路走 _web_run_conversation 的 MirrorBot。
    """
    name = command.strip().split(" ", 1)[0].lstrip("/").split("@", 1)[0].lower()
    handler = _WEB_COMMAND_MAP.get(name)
    update, context, _bot = build_web_command_objects(
        BotConfig.AUTHORIZED_USER_ID, outbox, command,
    )
    try:
        if handler is None:
            # 未知命令当成普通对话发出去，避免网页端 /xxx 没反应。
            await GlobalRecorder.record_user_message(
                command, MessageType.USER_TEXT, BotConfig.AUTHORIZED_USER_ID,
            )
            await process_conversation(update, context, command)
        else:
            await handler(update, context)
        outbox.put({"type": "turn_end"})
    except Exception as e:
        logger.exception("Web 命令失败: %s", command)
        outbox.put({"type": "turn_error", "text": redact_sensitive_text(str(e))[:300]})


# 网页可用的 /命令 -> cmd_* 处理函数映射，与 main.py 的 CommandHandler 注册一致。
# 在模块加载完成前这些名字可能还没就绪，所以延迟到首次使用时构建。
_WEB_COMMAND_MAP: Dict[str, Any] = {}


def _ensure_web_command_map() -> None:
    if _WEB_COMMAND_MAP:
        return
    pairs = [
        ("start", "cmd_start"),
        ("config", "cmd_settings_menu"),
        ("update", "cmd_update_system"),
        ("restart", "cmd_restart_system"),
        ("providers", "cmd_providers_menu"),
        ("provider_config", "cmd_provider_config"),
        ("models", "cmd_models_menu"),
        ("chat_model", "cmd_chat_model_menu"),
        ("media_model", "cmd_media_model_menu"),
        ("prompts", "cmd_prompts_menu"),
        ("clear_memory", "cmd_delete_chat"),
        ("depth", "cmd_depth_menu"),
        ("timeout", "cmd_timeout_menu"),
        ("thinking", "cmd_thinking_menu"),
        ("web", "cmd_web_menu"),
        ("agent", "cmd_toggle_agent"),
        ("blacklist", "cmd_blacklist_menu"),
        ("stream", "cmd_toggle_stream"),
        ("status", "cmd_show_info"),
        ("export", "cmd_export_all"),
        ("show_chat_info", "cmd_show_info"),
    ]
    g = globals()
    for cmd_name, fn_name in pairs:
        fn = g.get(fn_name)
        if fn is not None:
            _WEB_COMMAND_MAP[cmd_name] = fn


def _web_submit_message(text: str, outbox: Any) -> None:
    """HTTP 线程调用：把对话丢进事件循环，不等它跑完。

    一轮 Agent 对话可能跑几分钟，HTTP 请求不能挂在那里等。
    """
    loop = _web_chat_server.config.loop if _web_chat_server else None
    if loop is None:
        outbox.put({"type": "turn_error", "text": "服务未就绪"})
        return
    asyncio.run_coroutine_threadsafe(_web_run_conversation(text, outbox), loop)


def _web_submit_callback(callback_data: str, message_id: int, outbox: Any) -> None:
    """HTTP 线程调用：把网页按钮点击丢进事件循环。"""
    loop = _web_chat_server.config.loop if _web_chat_server else None
    if loop is None:
        outbox.put({"type": "turn_error", "text": "服务未就绪"})
        return
    asyncio.run_coroutine_threadsafe(
        _web_handle_callback(callback_data, message_id, outbox), loop,
    )


def _web_submit_command(command: str, outbox: Any) -> None:
    """HTTP 线程调用：把网页 /命令丢进事件循环。"""
    _ensure_web_command_map()
    loop = _web_chat_server.config.loop if _web_chat_server else None
    if loop is None:
        outbox.put({"type": "turn_error", "text": "服务未就绪"})
        return
    asyncio.run_coroutine_threadsafe(_web_handle_command(command, outbox), loop)


def get_web_outbox() -> Optional[Any]:
    """返回当前网页 SSE 队列；Web 未运行时返回 None。

    Telegram 侧对话用它把消息镜像到网页（TG -> Web 方向）。
    """
    if _web_chat_server is None:
        return None
    return _web_chat_server.outbox


def get_web_real_bot() -> Optional[Any]:
    """返回真实 PTB bot 引用，供 TG 侧构建 MirrorBot 镜像到网页。"""
    return _web_real_bot


def _web_request_stop() -> None:
    """复用 Telegram 侧的停止语义——全局只有一个停止事件。"""
    event = _stop_generation_event
    if event is not None and not event.is_set():
        event.set()


def _web_is_busy() -> bool:
    return _conversation_processing_lock.locked()


async def start_web_chat_if_enabled(app: Any) -> None:
    """按开关启动 Web 服务。失败只记日志，绝不影响 bot 主流程。"""
    global _web_chat_server, _web_real_bot
    if _web_chat_server is not None:
        return
    if not normalize_bool(UserDataManager.get('web_enabled', False), False):
        return

    # 记下真实 bot，网页发起对话时 MirrorBot 用它把消息同步到 Telegram。
    _web_real_bot = getattr(app, "bot", None)

    password_hash = await read_web_password_hash()
    if not password_hash:
        logger.warning("Web Chat 已开启但未设置密码，跳过启动")
        with contextlib.suppress(Exception):
            await app.bot.send_message(
                chat_id=BotConfig.AUTHORIZED_USER_ID,
                text="⚠️ Web Chat 已开启但没有设置密码，未启动。请在 /start → 🌐 Web 里设置密码。",
            )
        return

    config = WebChatConfig(
        host=DEFAULT_WEB_HOST,
        port=normalize_web_port(UserDataManager.get('web_port', DEFAULT_WEB_PORT)),
        password_hash=password_hash,
        bot_token=BotConfig.TOKEN,
        authorized_user_id=BotConfig.AUTHORIZED_USER_ID,
        loop=asyncio.get_running_loop(),
        submit_message=_web_submit_message,
        submit_callback=_web_submit_callback,
        submit_command=_web_submit_command,
        read_history=_web_read_history,
        read_settings=_web_read_settings,
        write_setting=_web_write_setting,
        request_stop=_web_request_stop,
        is_busy=_web_is_busy,
    )
    server = WebChatServer(config)
    try:
        await asyncio.to_thread(server.start)
    except Exception as e:
        logger.error(f"Web Chat 启动失败: {e}")
        with contextlib.suppress(Exception):
            await app.bot.send_message(
                chat_id=BotConfig.AUTHORIZED_USER_ID,
                text=f"⚠️ Web Chat 启动失败：{safe_text(str(e)[:200])}",
            )
        return
    _web_chat_server = server


async def stop_web_chat() -> None:
    global _web_chat_server
    if _web_chat_server is None:
        return
    server, _web_chat_server = _web_chat_server, None
    with contextlib.suppress(Exception):
        await asyncio.to_thread(server.stop)


async def restart_web_chat(app: Any) -> None:
    """改端口/密码后重启，让新配置立即生效。"""
    await stop_web_chat()
    await start_web_chat_if_enabled(app)


def is_web_chat_running() -> bool:
    return _web_chat_server is not None and _web_chat_server.running

# --- ☆ 其他类型消息处理 ☆ ---
