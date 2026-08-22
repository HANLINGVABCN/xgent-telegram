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
# 跨端同步观察者任务（CLI→Web 实时帧 + CLI→TG 镜像），随 bot 启停、
# 不随 Web 服务启停（见 start_external_sync_watcher）。
_web_external_watch_task: Optional[asyncio.Task] = None
# 观察者当前推送的 outbox。Web 服务开着时是它的 outbox（帧直达网页），
# 没开时是一个无订阅者的独立 outbox（put 落空，等 Web 启动后换接）。
_web_external_outbox: Any = WebOutbox()
# 真实 PTB bot 引用。网页发起对话时，MirrorBot 用它把消息同时投递到 Telegram。
_web_real_bot: Optional[Any] = None
_web_application: Optional[Any] = None

# 网页可改的参数白名单。刻意不含提供商增删改和 API Key——那些留在 Telegram 里。
WEB_EDITABLE_SETTINGS = {
    'thinking_level', 'stream_mode', 'agent_mode', 'text_stitch_mode',
    'global_depth', 'agent_max_iterations', 'stream_timeout', 'chat_model',
    'disabled_skills', 'agent_command_timeout', 'idle_message_interval',
    'smart_match_threshold',
    # token 统计相关：价格表 / 手动合并表 / 报表默认选项
    'model_price_table', 'model_merge_map', 'stats_auto_merge', 'stats_metric',
}


async def _web_read_history(limit: int) -> List[Dict[str, Any]]:
    db = await BotMemoryDB.get_instance()
    # 用显示专用查询：执行结果/媒体回复显示在 AI 侧，不沿用模型上下文的 user 映射。
    rows = await db.get_display_history(limit)
    result = []
    for row in rows:
        msg_type = row.get('msg_type')
        content = str(row.get('content') or '')
        # AI_REPLY 存的是 Markdown 原文：转成 Telegram HTML 再返回，前端 sanitizeHtml
        # 即可正常渲染粗体/标题/列表/引用/代码等。刷新后格式不再丢失。
        # TOKEN_USAGE/AGENT_RESULT/AGENT_CMD/MEDIA_REPLY 已是 HTML，不再二次转换，
        # 但要带 parse_mode=HTML 让前端走 sanitizeHtml 而非纯文本分支（否则 <i>/<pre>
        # 等标签被 escapeHtml 转义成字面文本）。
        if msg_type == MessageType.AI_REPLY:
            try:
                content = markdown_to_telegram_html(content)
            except Exception:
                pass  # 转换失败退回原文，总比报错好
            result.append({'role': 'assistant', 'content': content, 'parse_mode': 'HTML'})
        elif msg_type in (MessageType.TOKEN_USAGE, MessageType.AGENT_RESULT,
                          MessageType.AGENT_CMD, MessageType.AGENT_STATUS,
                          MessageType.MEDIA_REPLY, MessageType.SYSTEM_OP):
            # 这些类型存库时已是 Telegram HTML，直接带 parse_mode 让前端渲染。
            # token 统计行是元信息不是正文：降级成 system 角色，前端渲染成居中
            # 灰条，不再混在 AI 气泡流里（对齐实时流的观感）。
            result.append({
                'role': 'system' if msg_type == MessageType.TOKEN_USAGE
                        else str(row.get('role') or 'user'),
                'content': content,
                'parse_mode': 'HTML',
            })
        else:
            result.append({
                'role': str(row.get('role') or 'user'),
                'content': content,
            })
    return result


def _external_row_to_frame(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """外部进程（CLI）写入的一条全局记录 -> SSE 帧。

    映射**逐条对齐** get_display_history / _web_read_history 的显示语义：
    显示历史里整体隐藏的（AGENT_CMD 协议原文/媒体提示词）实时帧也不推；
    显示成居中灰条的（TOKEN_USAGE）推 notice 帧。此前这里把 AGENT_CMD 协议
    块也当 HTML 消息推给网页——CLI 一轮 Agent 对话的每个协议块、每条中间
    记录都变成一个新气泡（外部帧没有 message_id，前端无法原地更新，只能
    越堆越多），每秒一批地刷屏；刷新/断线重连后历史又按显示语义整页重画，
    这些气泡成批消失——就是"CLI 发消息时网页刷屏+吞消息"的机制。
    """
    msg_type = row.get('msg_type')
    content = str(row.get('content') or '')
    if not content.strip():
        return None
    if msg_type == MessageType.AGENT_CMD:
        # 协议块原文/媒体提示词：bot 界面从不把它们当独立消息显示（用户
        # 看到的是 AI 正文 + 每轮的 AGENT_STATUS 状态行），实时帧同样不推。
        return None
    if msg_type == MessageType.TOKEN_USAGE:
        # token 统计是元信息不是正文：显示历史里降级成 system 灰条，实时
        # 帧用 notice 对齐，不再混在 AI 气泡流里。
        return {"type": "notice", "text": content, "parse_mode": "HTML", "external": True}
    if msg_type == MessageType.AI_REPLY:
        try:
            content = markdown_to_telegram_html(content)
        except Exception:
            pass
        return {"type": "message", "text": content, "parse_mode": "HTML", "external": True}
    display_role = (
        'assistant'
        if msg_type in (MessageType.AGENT_RESULT, MessageType.AGENT_STATUS,
                        MessageType.MEDIA_REPLY)
        else str(row.get('role') or 'user')
    )
    if display_role == 'user':
        return {"type": "user_message", "text": content, "external": True}
    if display_role in ('system', 'media_module'):
        return {"type": "notice", "text": content, "external": True}
    html_types = (MessageType.AGENT_RESULT, MessageType.AGENT_STATUS,
                  MessageType.MEDIA_REPLY, MessageType.SYSTEM_OP)
    return {
        "type": "message",
        "text": content,
        "parse_mode": "HTML" if msg_type in html_types else None,
        "external": True,
    }


def _external_row_to_telegram_texts(row: Dict[str, Any]) -> list:
    """外部进程（CLI）的一条记录 -> 要镜像到 Telegram 的消息文本列表。

    只镜像真正的对话内容：带 origin=cli-chat 标记的 USER_TEXT（CLI 的对话
    文本；加记忆、填 API Key 这类状态机输入不带标记，不镜像）和**最终**
    AI_REPLY——agent_intermediate 标记的 Agent 中间轮响应、no_mirror 标记的
    停止标记（⏹️ 已停止）都跳过，否则 CLI 一轮 Agent 对话/连按 Ctrl+C 会把
    Telegram 刷成十几条中间输出。命令、菜单、系统操作一律不同步；TOKEN_USAGE
    则同步——CLI 里聊天本来就看不到 token 用量，镜像到 Telegram 后至少能在
    那边看到这轮花了多少 token（原生 Telegram 对话里这条消息本就存在，CLI
    镜像不该比原生体验差）。content 已是 `<i>…</i>` 包裹的 HTML，这里的镜像
    走纯文本 send_message（没有 parse_mode 通道），先把 <i> 标签剥掉再发，
    避免用户在 Telegram 里看到裸露的尖括号标签。
    """
    msg_type = row.get('msg_type')
    content = str(row.get('content') or '').strip()
    if not content:
        return []
    if msg_type == MessageType.USER_TEXT and row.get('origin') == 'cli-chat':
        return split_text_for_telegram(f"🖥 [CLI]\n{content}")
    if msg_type == MessageType.AI_REPLY and not row.get('agent_intermediate') \
            and not row.get('no_mirror'):
        # no_mirror：停止标记（⏹️ 已停止）这类回合收尾的内部状态，不是对话内容。
        return split_text_for_telegram(content)
    if msg_type == MessageType.TOKEN_USAGE:
        plain = re.sub(r'</?i>', '', content)
        return split_text_for_telegram(plain)
    return []


# 镜像限流保险丝：无论上游出什么 bug，Telegram 每分钟最多收到这么多条 CLI
# 镜像。CLI 一轮对话正常最多产生「1 条用户消息 + 1 条最终回复（可能分块）」，
# 这个上限给长回复分块和连续几轮对话留了余量，同时把失控场景（曾把用户逼到
# kill pm2 的刷屏事故）硬性截停。
_TG_MIRROR_MAX_PER_MINUTE = 15


def _web_external_tg_mirror_enabled() -> bool:
    """CLI->Telegram 镜像开关：要有真实 bot（纯 Web 模式没有），且环境变量
    XGENT_CLI_NO_TG_MIRROR 未设（不想让 Telegram 被 CLI 刷屏时关掉）。"""
    return (
        bool(BotConfig.TOKEN)
        and _web_real_bot is not None
        and not os.environ.get("XGENT_CLI_NO_TG_MIRROR")
    )


async def _web_external_record_watcher() -> None:
    """跨进程同步：盯 global_messages 的新记录，把**其它进程**（CLI）写入的
    记录推成 SSE 帧，并把对话内容镜像到 Telegram。

    CLI 与服务端是两个进程、只共享数据库，没有别的实时通道；轮询 rowid
    游标是最小代价的桥。本进程自己写的记录已经通过直发帧/MirrorBot 到达
    网页，靠 metadata.src（services._RECORDER_SOURCE_ID）跳过，否则同一句
    话会显示两遍。游标从启动时刻的最大 rowid 开始，不重播历史。

    CLI→Telegram 镜像放在这里而不是在 CLI 进程里做：服务端持有健康的 PTB
    连接池（网页↔TG 的秒同步走的就是它），而 CLI 进程自己开裸 urllib 连
    Telegram，每条消息都重新 TCP+TLS 握手，网络不好时逐条拖到 30 秒超时，
    就是"CLI→bot 同步极慢甚至不同步"；CLI 侧镜像还是 create_task 发完即忘，
    进程一退出未发完的任务直接被砍（吞消息）。改由服务端镜像后，CLI 只写
    库，网络与生命周期都由服务端兜底，三端速度一致。
    """
    db = await BotMemoryDB.get_instance()
    cursor_rowid = await db.get_max_global_rowid()
    mirror_send_times: list = []   # 滚动窗口：最近一分钟内实际发出的镜像条数
    # 行级去重：已处理过的 rowid 绝不二次推帧/镜像。游标是"从哪继续读"，
    # 去重集是"哪些已经发过"——两者独立。生产上出现过同一行被重复镜像的事故
    # （一条 CLI 消息在 TG 出现 12 遍，用户被迫 kill pm2），无论根因是游标
    # 回退、多实例还是别的，"每行只发一次"在这里硬性兜底。
    processed_rowids: set = set()
    while True:
        await asyncio.sleep(1.0)
        try:
            rows = await db.get_records_since_rowid(cursor_rowid)
        except Exception:
            logger.debug("web 跨端同步轮询失败", exc_info=True)
            continue
        mirror_texts = []
        for row in rows:
            cursor_rowid = max(cursor_rowid, int(row.get('rowid') or 0))
            rowid_val = int(row.get('rowid') or 0)
            if rowid_val and rowid_val in processed_rowids:
                continue
            if rowid_val:
                processed_rowids.add(rowid_val)
                # rowid 单调递增，旧值永远不会再命中；定期修剪防无限增长。
                if len(processed_rowids) > 10000:
                    processed_rowids = {
                        r for r in processed_rowids if r > cursor_rowid - 5000
                    }
            if row.get('src') == _RECORDER_SOURCE_ID:
                continue
            frame = _external_row_to_frame(row)
            outbox = _web_external_outbox
            if frame is not None and outbox is not None:
                outbox.put(frame)
            if _web_external_tg_mirror_enabled():
                mirror_texts.extend(_external_row_to_telegram_texts(row))
        # 网页帧先推完再发 Telegram：TG 那边网络慢时不能拖住下一轮网页帧。
        # 限流保险丝在文本生成侧就砍掉超量，绝不向上游（TG API）放行洪水。
        now = time.time()
        mirror_send_times = [t for t in mirror_send_times if now - t < 60.0]
        dropped = 0
        for text in mirror_texts:
            if len(mirror_send_times) >= _TG_MIRROR_MAX_PER_MINUTE:
                dropped += 1
                continue
            try:
                await _web_real_bot.send_message(
                    chat_id=BotConfig.AUTHORIZED_USER_ID, text=text,
                )
            except Exception:
                logger.warning("CLI 消息镜像到 Telegram 失败", exc_info=True)
            mirror_send_times.append(time.time())
        if dropped:
            logger.warning(
                "CLI 镜像限流：本批 %d 条文本超限，丢弃 %d 条（每分钟上限 %d）",
                len(mirror_texts), dropped, _TG_MIRROR_MAX_PER_MINUTE,
            )


async def start_external_sync_watcher(outbox: Any = None, app: Any = None) -> None:
    """启动/接线跨端同步观察者（CLI→网页帧 + CLI→Telegram 镜像）。

    与 Web 服务开关**解耦**：Web 关着时网页帧没有订阅者（put 落空，无害），
    但 CLI→Telegram 镜像仍然要工作，所以观察者跟随 bot 生命周期而不是 Web
    服务生命周期——此前它挂在 start_web_chat_if_enabled 里，Web 一关 CLI 的
    跨端同步就整个停摆。outbox 给了就记住（Web 服务启动后把自己的 outbox
    接进来），app 给了就刷新真实 bot 引用。
    """
    global _web_external_watch_task, _web_external_outbox, _web_real_bot
    if app is not None:
        _web_real_bot = getattr(app, "bot", None)
    if outbox is not None:
        _web_external_outbox = outbox
    if _web_external_watch_task is None or _web_external_watch_task.done():
        _web_external_watch_task = asyncio.create_task(_web_external_record_watcher())


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
    # skill 列表供前端渲染勾选项：每个 skill 的相对路径 + 显示名（stem）
    skill_files = list_skill_files()
    skill_list = [
        {
            'path': rp,
            'label': os.path.splitext(os.path.basename(rp))[0],
            'source': 'private' if rp.startswith('private/') else 'public',
        }
        for rp in skill_files
    ]
    disabled_raw = UserDataManager.get('disabled_skills', [])
    disabled_skills = disabled_raw if isinstance(disabled_raw, list) else []

    # token 统计相关：价格表 / 手动合并表 / 报表默认选项
    price_table_raw = UserDataManager.get('model_price_table', {}) or {}
    if isinstance(price_table_raw, str):
        try:
            price_table_raw = json.loads(price_table_raw)
        except Exception:
            price_table_raw = {}
    model_price_table = {}
    if isinstance(price_table_raw, dict):
        for k, v in price_table_raw.items():
            if isinstance(v, dict):
                model_price_table[k] = {
                    'input': float(v.get('input', 0) or 0),
                    'output': float(v.get('output', 0) or 0),
                    'cached': float(v.get('cached', 0) or 0),
                }
    merge_map_raw = UserDataManager.get('model_merge_map', {}) or {}
    if isinstance(merge_map_raw, str):
        try:
            merge_map_raw = json.loads(merge_map_raw)
        except Exception:
            merge_map_raw = {}
    model_merge_map = {}
    if isinstance(merge_map_raw, dict):
        for k, v in merge_map_raw.items():
            if isinstance(v, list):
                model_merge_map[k] = [str(x) for x in v if x]

    stats_auto_merge_val = UserDataManager.get('stats_auto_merge', True)
    stats_auto_merge = bool(stats_auto_merge_val) if not isinstance(stats_auto_merge_val, str) \
        else str(stats_auto_merge_val).lower() in {'1', 'true', 'yes', 'on'}
    stats_metric = UserDataManager.get('stats_metric', 'token') or 'token'

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
            'agent_command_timeout': normalize_command_timeout(
                UserDataManager.get('agent_command_timeout', DEFAULT_AGENT_COMMAND_TIMEOUT)
            ),
            'idle_message_interval': normalize_idle_message_interval(
                UserDataManager.get('idle_message_interval', DEFAULT_IDLE_MESSAGE_INTERVAL)
            ),
            'smart_match_threshold': int(UserDataManager.get('smart_match_threshold', 90) or 90),
            'chat_model': f"{prov_name}|{current_model}" if prov_name and current_model else '',
            'disabled_skills': disabled_skills,
            'model_price_table': model_price_table,
            'model_merge_map': model_merge_map,
            'stats_auto_merge': stats_auto_merge,
            'stats_metric': stats_metric,
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
            'skill_list': skill_list,
            'stats_metric': [
                {'value': 'token', 'label': 'Token 用量'},
                {'value': 'cost', 'label': '费用 (USD)'},
            ],
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
        if depth < 1:
            raise ValueError("记忆深度需大于 0")
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
    elif key == 'agent_command_timeout':
        timeout = normalize_command_timeout(value)
        UserDataManager.set(key, timeout)
        await UserDataManager.save_config(key, timeout)
    elif key == 'idle_message_interval':
        interval = normalize_idle_message_interval(value)
        UserDataManager.set(key, interval)
        await UserDataManager.save_config(key, interval)
    elif key == 'smart_match_threshold':
        try:
            pct = int(value)
        except (TypeError, ValueError):
            raise ValueError("智能匹配阈值必须是数字")
        if pct < 0:
            raise ValueError("智能匹配阈值需不小于 0")
        UserDataManager.set(key, pct)
        await UserDataManager.save_config(key, pct)
    elif key == 'disabled_skills':
        # 前端传一个被禁用 skill 的相对路径列表。normalize 成 list[str]，去重。
        raw = value if isinstance(value, list) else []
        cleaned = sorted({str(item) for item in raw if item})
        UserDataManager.set(key, cleaned)
        await UserDataManager.save_config(key, cleaned)
    elif key == 'model_price_table':
        # 前端传 dict: {model: {input,output,cached}}。校验并落库。
        raw = value if isinstance(value, dict) else {}
        cleaned = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                continue
            if not isinstance(v, dict):
                continue
            try:
                cleaned[k.strip()] = {
                    'input': float(v.get('input', 0) or 0),
                    'output': float(v.get('output', 0) or 0),
                    'cached': float(v.get('cached', 0) or 0),
                }
            except (TypeError, ValueError):
                raise ValueError(f"模型 {k} 的价格必须是数字")
        UserDataManager.set(key, cleaned)
        await UserDataManager.save_config(key, cleaned)
    elif key == 'model_merge_map':
        # 前端传 dict: {规范名: [实际名...]}。校验并落库。
        raw = value if isinstance(value, dict) else {}
        cleaned = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                continue
            members = [str(x).strip() for x in (v if isinstance(v, list) else []) if str(x).strip()]
            if members:
                cleaned[k.strip()] = members
        UserDataManager.set(key, cleaned)
        await UserDataManager.save_config(key, cleaned)
    elif key == 'stats_auto_merge':
        flag = normalize_bool(value, True)
        UserDataManager.set(key, flag)
        await UserDataManager.save_config(key, flag)
    elif key == 'stats_metric':
        metric = str(value or 'token').strip().lower()
        if metric not in {'token', 'cost'}:
            raise ValueError("统计指标必须是 token 或 cost")
        UserDataManager.set(key, metric)
        await UserDataManager.save_config(key, metric)

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
    # 配置状态（设置密码/端口/地址/提示词/Key 等）：走 Telegram 同款状态机，
    # 不要当 AI 对话。否则网页输入 cancel 或配置值会被发给 AI（既有 bug）。
    state = UserDataManager.get('state')
    if state != BotState.IDLE:
        try:
            update.message.text = text
        except Exception:
            pass
        # 状态处理器（设密码/端口）会调 restart_web_chat(context.application)，
        # mirror context 默认无该属性，这里补上真实 application。
        context.application = _web_application
        try:
            await handle_text_message(update, context)
        except Exception as e:
            logger.exception("Web 状态处理失败")
            outbox.put({"type": "turn_error", "text": redact_sensitive_text(str(e))[:300]})
        else:
            outbox.put({"type": "turn_end"})
        return
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
    """网页 /命令：路由到对应的 cmd_* 处理函数，输出同步到 Telegram。

    用 MirrorBot（带 real_bot）让命令的 send_message/edit_text 既推网页帧又发 TG。
    命令以 reply_text 新发消息为主，MirrorBot 的 _id_map 保证后续 edit_text 能
    通过假 id 找到 TG 真实 id 并更新，不像按钮点击那样编辑「别人发的旧消息」。
    """
    name = command.strip().split(" ", 1)[0].lstrip("/").split("@", 1)[0].lower()
    handler = _WEB_COMMAND_MAP.get(name)
    update, context, _bot = build_web_command_objects(
        BotConfig.AUTHORIZED_USER_ID, outbox, command, _web_real_bot,
    )
    # 命令可能触发 restart_web_chat（设端口/密码），状态处理器会取 context.application。
    context.application = _web_application
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
        ("params", "cmd_timeout_menu"),
        ("thinking", "cmd_thinking_menu"),
        ("web", "cmd_web_menu"),
        ("agent", "cmd_toggle_agent"),
        ("blacklist", "cmd_blacklist_menu"),
        ("stream", "cmd_toggle_stream"),
        ("skills", "cmd_skills_menu"),
        ("status", "cmd_show_info"),
        ("export", "cmd_export_all"),
        ("stats", "cmd_token_stats"),
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


async def _web_deliver_file_to_tg(abs_path: str, filename: str, caption: str) -> None:
    """把网页上传的文件同步发到 Telegram。三段分流对齐 agent_sendfile.py:79-174：

      ≤ MAX_FILE_SIZE              → 原生 send_document 上传
      > MAX_FILE_SIZE + 本地 server → 硬链到 .local-api-data/，用 file:// 容器路径直发
      > MAX_FILE_SIZE 无 server     → 只在网页告警，不发（对话照常进行）

    本函数刻意不抛异常：TG 同步失败用 outbox 推一条 sys 提示，网页对话不被拖崩。
    """
    if _web_real_bot is None:
        return  # 纯网页模式，无 TG 可同步

    chat_id = BotConfig.AUTHORIZED_USER_ID
    # 发到 TG 的 caption 带 [网页] 标记，与网页文本消息的镜像标记一致，
    # 让 TG 端知道这文件来自网页。仅用于 TG 发送，不影响喂给模型的附言。
    tg_caption = f"💬 [网页]\n{caption}" if caption else "💬 [网页]"
    try:
        file_size = os.path.getsize(abs_path)
        # AgentExecutor.MAX_FILE_SIZE 与发送侧同源（50MB），通过共享命名空间可见。
        max_file_size = AgentExecutor.MAX_FILE_SIZE
        api_base_url = BotConfig.API_BASE_URL

        if file_size > max_file_size and api_base_url:
            # 大文件 + 本地 server：硬链到宿主机数据目录，用容器内 file:// 路径直发。
            # _LOCAL_API_HOST_DATA_DIR / _LOCAL_API_CONTAINER_DATA_DIR 来自 core.py。
            import uuid as _uuid
            name_parts = filename.rsplit('.', 1)
            unique_name = (
                f"{name_parts[0]}_web_{_uuid.uuid4().hex[:8]}.{name_parts[1]}"
                if len(name_parts) == 2
                else f"{filename}_web_{_uuid.uuid4().hex[:8]}"
            )
            temp_host_path = os.path.join(_LOCAL_API_HOST_DATA_DIR, unique_name)

            def _prepare() -> None:
                os.makedirs(_LOCAL_API_HOST_DATA_DIR, exist_ok=True)
                try:
                    if os.path.exists(temp_host_path):
                        os.remove(temp_host_path)
                    os.link(abs_path, temp_host_path)
                except OSError:
                    shutil.copy2(abs_path, temp_host_path)

            await asyncio.to_thread(_prepare)
            try:
                container_path = f"file://{_LOCAL_API_CONTAINER_DATA_DIR}/{unique_name}"
                read_timeout = max(120, min(1800, int(file_size / (50 * 1024 * 1024) * 60)))
                await _web_real_bot.send_document(
                    chat_id=chat_id,
                    document=container_path,
                    filename=filename,
                    caption=tg_caption,
                    read_timeout=read_timeout,
                    write_timeout=read_timeout,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            finally:
                try:
                    await asyncio.to_thread(
                        lambda: os.path.exists(temp_host_path) and os.remove(temp_host_path)
                    )
                except OSError:
                    logger.warning("清理 web 上传临时文件失败: %s", temp_host_path)
            return

        if file_size > max_file_size:
            # 大文件但没配本地 server：发不了。用 message 帧推一条 AI 侧提示——
            # 不能用 turn_error（会让前端 setBusy(false) 中断本轮）；message 帧只
            # hideTyping + 显示气泡，不动 busy，正好。显示在 AI 侧而非用户侧。
            server_obj = _web_chat_server
            if server_obj is not None:
                server_obj.outbox.put({
                    "type": "message",
                    "text": (
                        f"⚠️ 文件 {filename} 超过 50MB({file_size} bytes)，未启用本地 API，"
                        "无法同步到 Telegram（仅网页处理）。如需同步大文件，请在 install.sh 菜单"
                        "选项 8 启用本地 API 容器。"
                    ),
                    "ts": time.time(),
                })
            return

        # 小文件：原生上传。
        with open(abs_path, "rb") as f:
            await _web_real_bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=filename,
                caption=caption or None,
                read_timeout=120,
                write_timeout=120,
            )
    except Exception as exc:
        # TG 同步失败不阻断网页对话，与 MirrorBot._tg_call 容错策略一致。
        # 用 message 帧显示在 AI 侧（系统告警），不用 user_message 误显示在用户侧。
        logger.warning("Web 文件同步到 Telegram 失败: %s", exc)
        server_obj = _web_chat_server
        if server_obj is not None:
            server_obj.outbox.put({
                "type": "message",
                "text": f"⚠️ 文件 {filename} 同步到 Telegram 失败：{str(exc)[:120]}",
                "ts": time.time(),
            })


async def _web_run_file_conversation(filename: str, content: bytes,
                                     caption: str, outbox: Any) -> None:
    """网页上传文件后跑一轮处理，复用 Telegram 端 process_incoming_document
    （messages.py）的状态机分流，保证提供商配置导入 / 黑名单导入 / 记忆导入 /
    提示词更新 / 普通文件对话这五条路径在网页和 Telegram 上行为完全一致。

    历史 bug：本函数曾经只硬编码"普通文件对话"这一条路径，完全不检查
    UserDataManager 的 state，导致网页上传 JSON 发起"覆盖导入提供商配置"
    时，文件被当成普通附件喂给了 AI，而不是真正触发导入。现在改为调用
    共享的 process_incoming_document，状态判断只维护一份。

    与 TG 端唯一的差别在「文件来源」：TG 端用 get_file 下载，这里直接拿
    上传字节。文件本体同步到 Telegram 这一步通过 sync_to_telegram 回调传入
    process_incoming_document，只会在“普通文件”分支被调用——提供商配置等
    配置类文件不会被转发进 Telegram 聊天记录（避免 API Key 泄露到聊天历史）。
    """
    try:
        update, context, _bot = build_web_mirror_objects(
            BotConfig.AUTHORIZED_USER_ID, outbox, _web_real_bot,
        )

        async def _sync_to_tg(abs_path: str, file_name: str, file_caption: str) -> None:
            # 三段分流对齐 agent_sendfile.py:79-174，阈值用同源的
            # AgentExecutor.MAX_FILE_SIZE，保证两端「能发多大」一致。
            # abs_path 是 process_incoming_document 里 save_binary_upload
            # 已经落盘的路径，这里不重复存盘，避免同一份文件产生两个路径。
            await _web_deliver_file_to_tg(abs_path, file_name, file_caption)

        await process_incoming_document(
            update, context,
            doc_name=filename,
            content_bytes=content,
            caption=caption,
            file_size=len(content),
            sync_to_telegram=_sync_to_tg,
        )
        outbox.put({"type": "turn_end"})
    except Exception as e:
        logger.exception("Web 文件对话失败")
        outbox.put({"type": "turn_error", "text": redact_sensitive_text(str(e))[:300]})


async def _web_run_photo_conversation(filename: str, content: bytes,
                                      caption: str, outbox: Any) -> None:
    """网页上传图片后跑一轮对话，让 AI 真正"看懂"图片内容。

    历史 bug：网页上传的图片此前统一走 _web_run_file_conversation（文档
    语义），只存盘、记路径索引，从不构造 multimodal image content——AI
    完全看不到图里画的是什么，只知道"有个文件"。这与 Telegram 端
    handle_photo_message（other_messages.py）把图片 base64 编码后塞进
    content_override 的行为不对等。

    现在改为调用 build_photo_multimodal_payload（other_messages.py 里从
    _prepare_photo_payload 抽取的纯函数），构造与 Telegram 端完全一致的
    multimodal content，让 Web 上传图片时 AI 的识图能力和 Telegram 对齐。

    图片本体仍同步到 Telegram（与文件上传路径一致），但图片是纯二进制，
    不存在提供商配置/黑名单等需要按状态机分流的场景，所以不需要复用
    process_incoming_document——这与 Telegram 端 handle_photo_message
    不检查文档状态机、直接处理的行为一致。
    """
    try:
        update, context, _bot = build_web_mirror_objects(
            BotConfig.AUTHORIZED_USER_ID, outbox, _web_real_bot,
        )

        payload = build_photo_multimodal_payload(content, caption, filename)
        saved_photo = payload["saved_photo"]
        memory_text = payload["index_text"]

        # 图片本体同步到 Telegram，对齐 _web_run_file_conversation 的现状行为。
        await _web_deliver_file_to_tg(saved_photo['abs_path'], filename, caption)

        await GlobalRecorder.record_user_message(
            memory_text, MessageType.USER_PHOTO, BotConfig.AUTHORIZED_USER_ID
        )

        multimodal_content: List[Dict[str, str]] = []
        if payload["caption"]:
            multimodal_content.append({"type": "text", "text": f"用户附言：{payload['caption']}"})
        multimodal_content.append({"type": "text", "text": payload["saved_notice"]})
        multimodal_content.append({
            "type": "image",
            "mime_type": "image/jpeg",
            "data": payload["image_b64"],
        })

        await process_conversation(update, context, memory_text, content_override=multimodal_content)
        outbox.put({"type": "turn_end"})
    except Exception as e:
        logger.exception("Web 图片对话失败")
        outbox.put({"type": "turn_error", "text": redact_sensitive_text(str(e))[:300]})


# 图片文件后缀白名单，用于网页上传时区分"图片"（走 multimodal，AI 能看懂）
# 和"普通文件"（走 process_incoming_document，只是路径索引）。与
# Telegram 端 filters.PHOTO 依赖 Telegram 自己的媒体分类不同，网页上传
# 只能拿到文件名，所以退化成按后缀判断——覆盖常见格式即可，不追求完备
# （不在名单里的图片格式，比如小众的 .heic，会走普通文件路径，只是
# AI 看不懂内容，不影响文件本身正常存盘和发送）。
_WEB_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _web_submit_upload(filename: str, content: bytes, caption: str, outbox: Any) -> None:
    """HTTP 线程调用：把网页上传文件的对话丢进事件循环，不等它跑完。

    按文件名后缀分流：图片走 _web_run_photo_conversation（AI 能看懂图片
    内容），其余走 _web_run_file_conversation（process_incoming_document
    的状态机分流，覆盖提供商配置导入等场景）。仅当当前不处于任何配置类
    状态时才按图片处理——如果用户正在"覆盖导入提供商配置"流程中发了张
    图片（几乎不会发生，但保持行为可预期），仍应交给状态机处理并提示
    格式错误，而不是被当成图片直接喂给 AI。
    """
    _, ext = os.path.splitext(filename.lower())
    state = UserDataManager.get('state')
    if ext in _WEB_IMAGE_EXTENSIONS and state == BotState.IDLE:
        loop = _web_chat_server.config.loop if _web_chat_server else None
        if loop is None:
            outbox.put({"type": "turn_error", "text": "服务未就绪"})
            return
        asyncio.run_coroutine_threadsafe(
            _web_run_photo_conversation(filename, content, caption, outbox), loop,
        )
        return

    loop = _web_chat_server.config.loop if _web_chat_server else None
    if loop is None:
        outbox.put({"type": "turn_error", "text": "服务未就绪"})
        return
    asyncio.run_coroutine_threadsafe(
        _web_run_file_conversation(filename, content, caption, outbox), loop,
    )


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


def mirror_to_web(handler):
    """装饰器：让 TG 端命令/按钮 handler 的输出同步镜像到 web。

    process_conversation 内部已装 install_tg_to_web_mirror，但命令和按钮回调不走
    process_conversation，所以 web 端看不到 TG 端的菜单切换/按钮变化。本装饰器在
    handler 入口装镜像（web 在线时），finally restore，让 handler 里的
    send_message / edit_text / edit_reply_markup 等也推网页 SSE 帧。

    web 未运行时零开销——直接调原 handler。重入安全由 install_tg_to_web_mirror
    内部的 _ACTIVE_MIRRORS 计数保证。
    """
    async def wrapped(update, context):
        if not is_web_chat_running():
            return await handler(update, context)
        from xgent_app.web_bridge import install_tg_to_web_mirror
        outbox = get_web_outbox()
        real_bot = get_web_real_bot()
        restore = install_tg_to_web_mirror(real_bot, outbox)
        try:
            return await handler(update, context)
        finally:
            restore()
    wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    return wrapped


def _web_request_stop() -> None:
    """复用 Telegram 侧的停止语义——全局只有一个停止事件。"""
    event = _stop_generation_event
    if event is not None and not event.is_set():
        event.set()


def _web_is_busy() -> bool:
    return _conversation_processing_lock.locked()


async def start_web_chat_if_enabled(app: Optional[Any] = None, *, force: bool = False) -> None:
    """按开关启动 Web 服务。失败只记日志，绝不影响 bot 主流程。

    ``app`` 为 None 时代表纯 Web 模式（未配置 BOT_TOKEN，没有 PTB
    Application）：``_web_real_bot``/``_web_application`` 保持 None，
    MirrorBot/_web_deliver_file_to_tg 等调用点已原生支持 real_bot=None
    （纯网页输出，不回灌 Telegram）。

    ``force=True`` 时跳过 ``web_enabled`` 开关检查——纯 Web 模式下 Web 服务
    本身就是唯一入口，不应该受“开关默认关闭”影响；密码缺失时也只记日志，
    不尝试通过 Telegram 通知（可能没有 bot 可用）。
    """
    global _web_chat_server, _web_real_bot, _web_application
    if _web_chat_server is not None:
        return
    # 跨端同步观察者无论 Web 开不开都要跑（它还承担 CLI→TG 镜像），先启动
    # 并记下真实 bot；Web 开着时下面再把新服务器的 outbox 接给它。
    await start_external_sync_watcher(app=app)
    web_on = normalize_bool(UserDataManager.get('web_enabled', False), False)
    term_on = normalize_bool(UserDataManager.get('terminal_enabled', False), False)
    if not force and not (web_on or term_on):
        return

    # 记下真实 bot，网页发起对话时 MirrorBot 用它把消息同步到 Telegram。
    # app 为 None（纯 Web 模式）时保持 None，等价于纯网页输出。
    _web_real_bot = getattr(app, "bot", None)
    # 记下 application：网页触发配置状态（设密码/端口需 restart_web_chat）时，
    # 状态处理器会取 context.application，mirror context 默认没有该属性。
    _web_application = app

    password_hash = await read_web_password_hash()
    if not password_hash:
        logger.warning("Web Chat 已开启但未设置密码，跳过启动")
        if app is not None:
            with contextlib.suppress(Exception):
                await app.bot.send_message(
                    chat_id=BotConfig.AUTHORIZED_USER_ID,
                    text="⚠️ Web/终端服务已开启但没有设置密码，未启动。请在 /start → 🌐 Web 里设置密码。",
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
        submit_upload=_web_submit_upload,
        # 上传上限按 API_BASE_URL 选档，与 agent_sendfile.py 发送侧阈值同源：
        # 官方 API 50MB、本地 Bot API server 2GB。保证「web 放进来 → bot 发出去」
        # 两端阈值一致，不会出现 web 放行却在 send_document 被 TG 拒。
        upload_body_limit=(
            2 * 1024 * 1024 * 1024 if BotConfig.API_BASE_URL
            else 50 * 1024 * 1024
        ),
        read_history=_web_read_history,
        read_settings=_web_read_settings,
        write_setting=_web_write_setting,
        request_stop=_web_request_stop,
        is_busy=_web_is_busy,
        is_terminal_enabled=lambda: normalize_bool(UserDataManager.get('terminal_enabled', False), False),
        # /api/media/resolve 的白名单根：本应用自己的数据目录。xgent_storage
        # 覆盖 uploads/generated_media/exports；workspace 是 Agent 的默认
        # 工作目录（sendfile/file 协议产出的文件大多在这里）。除此之外的
        # 服务器路径一律不给换 token——那等于开放任意文件读取。
        media_allowed_roots=[
            ArtifactManager.ROOT_DIR,
            os.path.join(AgentExecutor.WORK_DIR, 'workspace'),
        ],
        # 纯 Web 模式（force=True）下 Web 服务本身就是唯一入口，不受
        # web_enabled 开关影响，始终视为已开启。
        is_web_enabled=(
            (lambda: True) if force
            else (lambda: normalize_bool(UserDataManager.get('web_enabled', False), False))
        ),
    )
    server = WebChatServer(config)
    try:
        await asyncio.to_thread(server.start)
    except Exception as e:
        logger.error(f"Web Chat 启动失败: {e}")
        if app is not None:
            with contextlib.suppress(Exception):
                await app.bot.send_message(
                    chat_id=BotConfig.AUTHORIZED_USER_ID,
                    text=f"⚠️ Web Chat 启动失败：{safe_text(str(e)[:200])}",
                )
        return
    _web_chat_server = server
    # 跨端同步观察者已在函数开头启动，这里把新服务器的 outbox 接给它：
    # 此后 CLI 写库的实时帧直达当前在线的网页订阅者。
    await start_external_sync_watcher(server.outbox)


async def stop_web_chat() -> None:
    global _web_chat_server, _web_external_outbox
    if _web_chat_server is None:
        return
    server, _web_chat_server = _web_chat_server, None
    with contextlib.suppress(Exception):
        await asyncio.to_thread(server.stop)
    # 观察者**不**随 Web 服务停止：CLI→Telegram 镜像与 Web 开关无关。
    # 只把 outbox 换回无订阅者的独立实例——旧 outbox 已 close，继续用它
    # 会让后续帧全部落空；等下次 Web 启动再接新的。
    _web_external_outbox = WebOutbox()


async def restart_web_chat(app: Optional[Any] = None, *, force: Optional[bool] = None) -> None:
    """改端口/密码后重启，让新配置立即生效。

    ``force`` 省略时按 ``app is None`` 自动判断：纯 Web 模式（app 为 None，
    没有 PTB Application 可传）下重启也应强制起服务，不受 web_enabled 开关
    影响，语义与 start_web_chat_if_enabled 的初次启动保持一致。
    """
    if force is None:
        force = app is None
    await stop_web_chat()
    await start_web_chat_if_enabled(app, force=force)


def is_web_chat_running() -> bool:
    return _web_chat_server is not None and _web_chat_server.running

# --- ☆ 其他类型消息处理 ☆ ---
