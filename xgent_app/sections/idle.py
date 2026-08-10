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
    """跑一轮完整对话，结束后给网页发一帧收尾信号。"""
    update, context, _bot = build_web_conversation_objects(BotConfig.AUTHORIZED_USER_ID, outbox)
    try:
        await GlobalRecorder.record_user_message(
            text, MessageType.USER_TEXT, BotConfig.AUTHORIZED_USER_ID
        )
        await process_conversation(update, context, text)
        outbox.put({"type": "turn_end"})
    except Exception as e:
        logger.exception("Web 对话失败")
        outbox.put({"type": "turn_error", "text": redact_sensitive_text(str(e))[:300]})


def _web_submit_message(text: str, outbox: Any) -> None:
    """HTTP 线程调用：把对话丢进事件循环，不等它跑完。

    一轮 Agent 对话可能跑几分钟，HTTP 请求不能挂在那里等。
    """
    loop = _web_chat_server.config.loop if _web_chat_server else None
    if loop is None:
        outbox.put({"type": "turn_error", "text": "服务未就绪"})
        return
    asyncio.run_coroutine_threadsafe(_web_run_conversation(text, outbox), loop)


def _web_request_stop() -> None:
    """复用 Telegram 侧的停止语义——全局只有一个停止事件。"""
    event = _stop_generation_event
    if event is not None and not event.is_set():
        event.set()


def _web_is_busy() -> bool:
    return _conversation_processing_lock.locked()


async def start_web_chat_if_enabled(app: Any) -> None:
    """按开关启动 Web 服务。失败只记日志，绝不影响 bot 主流程。"""
    global _web_chat_server
    if _web_chat_server is not None:
        return
    if not normalize_bool(UserDataManager.get('web_enabled', False), False):
        return

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
