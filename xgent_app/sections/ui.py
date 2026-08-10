# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

class CallbackDataStore:
    """存储长callback数据，返回短ID"""
    _store: OrderedDict = OrderedDict()
    MAX_SIZE = 1000  # 最多存储1000条
    
    @classmethod
    def store(cls, data: str) -> str:
        """存储数据并返回短ID"""
        if len(data) <= 60:
            return data
        short_id = f"cb_{short_hash(data)}"
        # 碰撞检测：如果短ID已存在但对应不同数据，扩展哈希长度
        if short_id in cls._store and cls._store[short_id] != data:
            short_id = f"cb_{hashlib.md5(data.encode()).hexdigest()[:12]}"
        cls._store[short_id] = data
        # LRU 清理
        while len(cls._store) > cls.MAX_SIZE:
            cls._store.popitem(last=False)
        return short_id
    
    @classmethod
    def get(cls, short_id: str) -> str:
        """获取原始数据"""
        return cls._store.get(short_id, short_id)

# --- ☆ UI 构建 ☆ ---
def build_magic_keyboard(items: List[str], page: int, callback_prefix: str, back_callback: str,
                         search_callback: Optional[str] = None, filter_text: Optional[str] = None,
                         extra_buttons: Optional[List[InlineKeyboardButton]] = None,
                         marker_fn: Optional[callable] = None):
    PER_PAGE = 8
    display_list = [m for m in items if filter_text and filter_text.lower() in m.lower()] if filter_text else items
    total_pages = math.ceil(len(display_list) / PER_PAGE) or 1
    page = max(1, min(page, total_pages))
    
    current_items = display_list[(page - 1) * PER_PAGE : page * PER_PAGE]
    keyboard = []
    
    for m in current_items:
        display_name = pretty_model_name(m)
        if marker_fn:
            marker = marker_fn(m)
            if marker:
                display_name = f"{display_name} {marker}"
        # 使用短哈希避免超长
        cb_data = CallbackDataStore.store(f"{callback_prefix}{m}")
        btn = InlineKeyboardButton(display_name, callback_data=cb_data)
        # 每个模型独占整行，确保长名不被 Telegram 截断为省略号
        keyboard.append([btn])
    
    nav_row = []
    if total_pages > 1:
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"page_{page-1}_{callback_prefix}"))
        nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"page_{page+1}_{callback_prefix}"))
        keyboard.append(nav_row)
    
    func_row = []
    if search_callback:
        func_row.append(InlineKeyboardButton(f"🔍 {filter_text or '搜寻'}", callback_data=search_callback))
    if extra_buttons:
        func_row.extend(extra_buttons)
    if func_row:
        keyboard.append(func_row)
    keyboard.append([InlineKeyboardButton("🔙 返回", callback_data=back_callback)])
    return InlineKeyboardMarkup(keyboard)

def _fmt_timeout(val):
    """格式化超时值为简短显示"""
    val = normalize_stream_timeout(val)
    if val == 0 or val is None:
        return "∞"
    return f"{int(val)}s"

def _fmt_command_timeout(val):
    val = normalize_command_timeout(val)
    return f"{val}s"

def _fmt_agent_max_iterations(val):
    val = normalize_agent_max_iterations(val)
    return f"{val}轮"

def _fmt_idle_message_interval(val):
    seconds = normalize_idle_message_interval(val)
    if seconds <= 0:
        return "∞关闭"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days}天"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours}小时"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes}分钟"
    return f"{seconds}s"

def _fmt_thinking_level(val=None):
    return get_thinking_level_label(val)

def get_main_menu():
    agent_on = UserDataManager.get('agent_mode', False)
    stream_on = normalize_bool(UserDataManager.get('stream_mode', True), True)
    stitch_label = get_text_stitch_mode_label()
    web_on = normalize_bool(UserDataManager.get('web_enabled', False), False)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 提供商", callback_data="menu_providers"),
         InlineKeyboardButton("🎯 模型", callback_data="menu_default_models")],
        [InlineKeyboardButton(f"🤖 Agent:{'开' if agent_on else '关'}", callback_data="toggle_agent_mode"),
         InlineKeyboardButton(f"🌊 流式:{'开' if stream_on else '关'}", callback_data="toggle_stream_mode")],
        [InlineKeyboardButton(f"🧩{stitch_label}", callback_data="menu_text_stitch_mode"),
         InlineKeyboardButton("🧠记忆", callback_data="menu_memory")],
        [InlineKeyboardButton(f"🌐 Web:{'开' if web_on else '关'}", callback_data="menu_web"),
         InlineKeyboardButton("📝 提示词", callback_data="menu_prompts")],
        [InlineKeyboardButton("⚙️ 更多", callback_data="menu_more_settings")]
    ])


def _build_web_open_button():
    """Web 配置菜单里用的「打开网页版」按钮。

    主菜单的 Web 按钮现在是恒定的回调入口（menu_web），不再承担"打开"职责，
    所以无论是否配置公开地址，配置菜单都能进得来、改得了。
    这里只负责在已开启时给出一个打开动作：
      - 配了 HTTPS 公开地址 → WebApp 内嵌打开；
      - 否则 → 外部浏览器打开本地地址。
    未开启时返回 None，调用方据此决定是否显示该行。
    """
    enabled = normalize_bool(UserDataManager.get('web_enabled', False), False)
    if not enabled:
        return None

    public_url = str(UserDataManager.get('web_public_url', '') or '')
    if public_url.startswith("https://"):
        return InlineKeyboardButton("🌐 打开网页版", web_app=WebAppInfo(url=public_url))

    port = normalize_web_port(UserDataManager.get('web_port', DEFAULT_WEB_PORT))
    return InlineKeyboardButton("🌐 浏览器打开", url=f"http://{DEFAULT_WEB_HOST}:{port}")


def _build_terminal_open_button():
    """终端开启时的「打开终端」按钮。复用 Web 的公开地址 + /terminal 路径。

    终端依赖 Web 服务（同一端口、同一认证），所以只在 web_enabled 且
    terminal_enabled 时返回按钮，否则 None。终端是任意命令执行，默认关闭，
    必须显式开启。
    """
    web_on = normalize_bool(UserDataManager.get('web_enabled', False), False)
    term_on = normalize_bool(UserDataManager.get('terminal_enabled', False), False)
    if not (web_on and term_on):
        return None

    public_url = str(UserDataManager.get('web_public_url', '') or '')
    if public_url.startswith("https://"):
        return InlineKeyboardButton(
            "🖥 打开终端", web_app=WebAppInfo(url=public_url.rstrip("/") + "/terminal")
        )
    port = normalize_web_port(UserDataManager.get('web_port', DEFAULT_WEB_PORT))
    return InlineKeyboardButton(
        "🖥 浏览器打开终端", url=f"http://{DEFAULT_WEB_HOST}:{port}/terminal"
    )


def get_web_menu():
    enabled = normalize_bool(UserDataManager.get('web_enabled', False), False)
    port = normalize_web_port(UserDataManager.get('web_port', DEFAULT_WEB_PORT))
    public_url = str(UserDataManager.get('web_public_url', '') or '')
    has_password = bool(UserDataManager.get('_web_has_password', False))

    rows: List[List[InlineKeyboardButton]] = []
    # 已开启时，第一行就是「打开网页版」，和开关/端口/密码等配置项分开。
    open_button = _build_web_open_button()
    if open_button is not None:
        rows.append([open_button])
    rows.extend([
        [InlineKeyboardButton(
            f"{'🟢 已开启' if enabled else '🔴 已关闭'}　点击{'关闭' if enabled else '开启'}",
            callback_data="toggle_web_enabled"
        )],
        [InlineKeyboardButton(
            f"🔑 密码：{'已设置' if has_password else '未设置'}",
            callback_data="act_set_web_password"
        )],
        [InlineKeyboardButton(f"🔌 端口：{port}", callback_data="act_set_web_port")],
        [InlineKeyboardButton(
            f"🌐 公开地址：{'已配置' if public_url else '未配置'}",
            callback_data="act_set_web_public_url"
        )],
    ])
    if has_password:
        rows.append([InlineKeyboardButton("🗑️ 清除密码", callback_data="confirm_clear_web_password")])
    if public_url:
        rows.append([InlineKeyboardButton("🧹 清除公开地址", callback_data="do_clear_web_public_url")])
    # 终端开关 + 打开按钮。终端依赖 Web，Web 关时终端按钮不显示。
    term_on = normalize_bool(UserDataManager.get('terminal_enabled', False), False)
    term_button = _build_terminal_open_button()
    if term_button is not None:
        rows.append([term_button])
    rows.append([InlineKeyboardButton(
        f"🖥 终端：{'🟢 开' if term_on else '🔴 关'}　点击{'关闭' if term_on else '开启'}",
        callback_data="toggle_terminal_enabled"
    )])
    rows.append([InlineKeyboardButton("🔙 返回", callback_data="act_main_menu")])
    return InlineKeyboardMarkup(rows)


def build_web_text() -> str:
    enabled = normalize_bool(UserDataManager.get('web_enabled', False), False)
    port = normalize_web_port(UserDataManager.get('web_port', DEFAULT_WEB_PORT))
    public_url = str(UserDataManager.get('web_public_url', '') or '')
    has_password = bool(UserDataManager.get('_web_has_password', False))
    running = is_web_chat_running()
    term_on = normalize_bool(UserDataManager.get('terminal_enabled', False), False)

    status = "🟢 运行中" if running else ("🟡 已开启但未运行" if enabled else "🔴 已关闭")
    password_line = "✅ 已设置" if has_password else "⚠️ 未设置（未设置时拒绝启动）"
    public_line = (
        f"✅ <code>{safe_text(public_url)}</code>"
        if public_url else "未配置（按钮将用外部浏览器打开本地地址）"
    )
    term_line = (
        f"🖥 终端：{'🟢 已开启（任意命令执行，注意安全）' if term_on else '🔴 已关闭'}"
    )

    return (
        "🌐 <b>Web Chat</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"状态：{status}\n"
        f"监听：<code>{DEFAULT_WEB_HOST}:{port}</code>\n"
        f"密码：{password_line}\n"
        f"公开地址：{public_line}\n"
        f"{term_line}\n\n"
        "网页版复用同一套对话核心，Agent 模式、协议执行、记忆与 Telegram 完全共享。\n"
        "可在网页里聊天并调整常用参数；提供商与 API Key 仍只在 Telegram 里管理。\n\n"
        f"⚠️ 服务只监听 <code>{DEFAULT_WEB_HOST}</code>，不会直接暴露到公网。"
        "要远程访问请自行配置反向代理，并在上面填入反代的 HTTPS 地址。\n"
        "Telegram 的内嵌网页按钮只接受 HTTPS 地址，这也是公开地址单独配置的原因。"
    )


def get_text_stitch_mode_menu():
    mode = normalize_text_stitch_mode(UserDataManager.get('text_stitch_mode'))

    def label(mode_key: str, text: str) -> str:
        return f"✅ {text}" if mode == mode_key else text

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label(TEXT_STITCH_MODE_AUTO, "自动判断"), callback_data="set_text_stitch_mode:auto")],
        [InlineKeyboardButton(label(TEXT_STITCH_MODE_FORCE, "强制开启拼接"), callback_data="set_text_stitch_mode:force")],
        [InlineKeyboardButton(label(TEXT_STITCH_MODE_OFF, "强制不拼接"), callback_data="set_text_stitch_mode:off")],
        [InlineKeyboardButton("🔙 返回", callback_data="act_main_menu")]
    ])


def build_text_stitch_mode_text() -> str:
    mode = get_text_stitch_mode_label()
    return (
        "🧩 <b>文字拼接模式</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"当前模式: <b>{safe_text(mode)}</b>\n\n"
        f"自动判断：短消息直接问 AI；单条接近 Telegram 上限（约 {TEXT_STITCH_SPLIT_HINT_CHARS} 字）时进入拼接，点完成后发送。\n"
        "强制开启：每条普通文本都会先累计，适合连续发多段内容。\n"
        "强制不拼接：所有普通文本都直接发送给 AI。"
    )


def get_text_stitch_pending_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 完成，发送给AI", callback_data="act_finish_text_stitch")],
        [InlineKeyboardButton("🧹 清空拼接", callback_data="act_cancel_text_stitch")]
    ])


def build_text_stitch_pending_text(pending: PendingTextConversation) -> str:
    return (
        "🧩 <b>正在拼接文字</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"已累计: <b>{len(pending.parts)} 段</b>\n"
        f"总字数: <b>{pending.total_chars()}</b>\n\n"
        "继续发送文字会追加到本次内容；全部发送完后点“完成，发送给AI”。"
    )

def get_more_settings_menu():
    global_depth = UserDataManager.get('global_depth', 30)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📊 深度:{global_depth}", callback_data="cmd_set_global_depth"),
         InlineKeyboardButton("⏱️ 超时", callback_data="menu_timeout_settings")],
        [InlineKeyboardButton(f"🧠 思考:{_fmt_thinking_level()}", callback_data="menu_thinking_level"),
         InlineKeyboardButton("🚫 Agent黑名单", callback_data="menu_command_blacklist")],
        [InlineKeyboardButton("🧹 清空上下文", callback_data="cmd_delete"),
         InlineKeyboardButton("ℹ️ 状态", callback_data="cmd_info")],
        [InlineKeyboardButton(f"🔐 凭据配置{_credentials_badge()}", callback_data="menu_credentials"),
         InlineKeyboardButton("📤 导出", callback_data="cmd_export_all")],
        [InlineKeyboardButton("⬆️ 更新", callback_data="cmd_update"),
         InlineKeyboardButton("🔄 重启", callback_data="cmd_restart")],
        [InlineKeyboardButton("🔙 返回", callback_data="act_main_menu")]
    ])


def get_thinking_level_menu():
    """思考深度：8 档单选。"""
    current = normalize_thinking_level(UserDataManager.get('thinking_level'))

    def button(level: str) -> InlineKeyboardButton:
        label = THINKING_LEVEL_LABELS[level]
        text = f"✅ {label}" if level == current else label
        return InlineKeyboardButton(text, callback_data=f"set_thinking_level:{level}")

    return InlineKeyboardMarkup([
        [button(THINKING_LEVEL_OFF), button(THINKING_LEVEL_AUTO)],
        [button(THINKING_LEVEL_LOW), button(THINKING_LEVEL_MEDIUM), button(THINKING_LEVEL_HIGH)],
        [button(THINKING_LEVEL_XHIGH), button(THINKING_LEVEL_ULTRA), button(THINKING_LEVEL_MAX)],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_more_settings")]
    ])


def build_thinking_level_text() -> str:
    current = normalize_thinking_level(UserDataManager.get('thinking_level'))
    spec = THINKING_LEVEL_SPECS.get(current)
    if current == THINKING_LEVEL_AUTO:
        detail = "不发送任何思考参数，完全交给提供商的默认行为。"
    elif current == THINKING_LEVEL_OFF:
        detail = "显式关闭思考。Gemini 走预算 0，OpenRouter 走 reasoning.enabled=false；其余格式不发字段即为关闭。"
    else:
        budget = spec["budget"]
        budget_text = "动态（由模型决定）" if budget < 0 else f"{budget} tokens"
        detail = f"思考预算：<b>{budget_text}</b>　推理档位：<b>{spec['effort']}</b>"

    return (
        "🧠 <b>思考深度</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"当前档位: <b>{safe_text(THINKING_LEVEL_LABELS[current])}</b>\n"
        f"{detail}\n\n"
        "会按提供商自动翻译成对应字段：\n"
        "• Claude → <code>thinking.budget_tokens</code>（并同步抬高 max_tokens）\n"
        "• Gemini/Vertex → <code>thinkingConfig.thinkingBudget</code>\n"
        "• OpenAI 及兼容接口 → <code>reasoning_effort</code>\n"
        "• OpenRouter → <code>reasoning.effort</code>\n\n"
        "思考内容不会显示在对话里，也不写入记忆；思考消耗的 token 会计入用量行。\n"
        "模型不支持时会自动去掉参数重发一次，并记住该模型不再重试。"
    )


def _credentials_badge() -> str:
    """在按钮上直接显示已配置了几项，省得点进去才知道。"""
    done = sum([
        bool(BotConfig.UPDATE_GITHUB_TOKEN),
        bool(BotConfig.TAVILY_API_KEY),
    ])
    return f":{done}/2"


def get_credentials_menu():
    """凭据集中配置：GitHub Token 与搜索 Key。"""
    gh = bool(BotConfig.UPDATE_GITHUB_TOKEN)
    tv = bool(BotConfig.TAVILY_API_KEY)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔑 GitHub Token：{'已配置' if gh else '未配置'}",
            callback_data="menu_github_token"
        )],
        [InlineKeyboardButton(
            f"🌐 搜索 API Key：{'已配置' if tv else '未配置'}",
            callback_data="menu_search_settings"
        )],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_more_settings")]
    ])


def build_credentials_text() -> str:
    gh = BotConfig.UPDATE_GITHUB_TOKEN
    tv = BotConfig.TAVILY_API_KEY
    return (
        "🔐 <b>凭据配置</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔑 GitHub Token：{'✅ ' + safe_text(mask_update_github_token(gh)) if gh else '⚠️ 未配置'}\n"
        f"🌐 搜索 API Key：{'✅ ' + safe_text(mask_search_api_key(tv)) if tv else '⚠️ 未配置'}\n\n"
        "GitHub Token 用于从私有仓库拉取更新；搜索 Key 用于 Agent 联网搜索。\n"
        "都会写入项目根目录的 <code>.env</code>，保存后立即生效，无需重启。"
    )


def get_github_token_menu():
    configured = bool(BotConfig.UPDATE_GITHUB_TOKEN)
    rows = [
        [InlineKeyboardButton(
            "🔑 修改 Token" if configured else "🔑 设置 Token",
            callback_data="act_set_github_token"
        )],
    ]
    if configured:
        rows.append([InlineKeyboardButton("🧪 验证 Token", callback_data="act_test_github_token")])
        rows.append([InlineKeyboardButton("🗑️ 清除 Token", callback_data="confirm_clear_github_token")])
    rows.append([InlineKeyboardButton("🔙 返回", callback_data="menu_credentials")])
    return InlineKeyboardMarkup(rows)


def build_github_token_text() -> str:
    token = BotConfig.UPDATE_GITHUB_TOKEN
    status = (
        f"✅ 已配置 <code>{safe_text(mask_update_github_token(token))}</code>"
        if token else "⚠️ 未配置"
    )
    return (
        "🔑 <b>GitHub Token</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"状态：{status}\n"
        f"更新源：<code>{safe_text(get_update_source_label(BotConfig.UPDATE_ZIP_URL))}</code>\n\n"
        "从私有仓库拉取更新时需要。保存时会自动验证，"
        "能区分「Token 无效」和「没授权这个仓库」两种情况。\n\n"
        "生成 Fine-grained Token 时注意：\n"
        "1️⃣ <b>Repository access</b> → <code>Only select repositories</code> → 勾上目标仓库\n"
        "2️⃣ <b>Permissions</b> → <code>Contents</code> → <b>Read-only</b>（默认是 No access，容易漏）"
    )


def get_search_settings_menu():
    configured = bool(BotConfig.TAVILY_API_KEY)
    rows = [
        [InlineKeyboardButton(
            "🔑 修改 API Key" if configured else "🔑 设置 API Key",
            callback_data="act_set_search_key"
        )],
    ]
    if configured:
        rows.append([InlineKeyboardButton("🧪 测试搜索", callback_data="act_test_search")])
        rows.append([InlineKeyboardButton("🗑️ 清除 Key", callback_data="confirm_clear_search_key")])
    rows.append([InlineKeyboardButton("🔙 返回", callback_data="menu_credentials")])
    return InlineKeyboardMarkup(rows)


def build_search_settings_text() -> str:
    key = BotConfig.TAVILY_API_KEY
    status = (
        f"✅ 已配置 <code>{safe_text(mask_search_api_key(key))}</code>"
        if key else "⚠️ 未配置"
    )
    hint = (
        "Agent 可以使用 <code>search-x</code> 联网搜索、<code>fetch-x</code> 抓取网页正文。\n"
        if key else
        "配置后 Agent 才能联网搜索。未配置时不影响其他功能。\n"
    )
    return (
        "🌐 <b>联网搜索设置</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"状态：{status}\n"
        "服务商：<b>Tavily</b>\n\n"
        f"{hint}\n"
        "免费额度 1000 次/月，在 <code>tavily.com</code> 注册后获取 Key。\n"
        "Key 会写入项目根目录的 <code>.env</code>，保存后立即生效，无需重启。"
    )

def build_settings_menu_text() -> str:
    return (
        "⚙️ <b>更多设置</b>\n"
        "━━━━━━━━━━━━━━\n"
        "调整记忆深度、超时、思考深度、Agent 黑名单、联网搜索、更新与重启。"
    )

def get_timeout_settings_menu():
    ai_timeout = UserDataManager.get('stream_timeout', 0)
    command_timeout = UserDataManager.get('agent_command_timeout', DEFAULT_AGENT_COMMAND_TIMEOUT)
    agent_max_iterations = UserDataManager.get('agent_max_iterations', DEFAULT_AGENT_MAX_ITERATIONS)
    idle_interval = UserDataManager.get('idle_message_interval', DEFAULT_IDLE_MESSAGE_INTERVAL)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 AI回复超时：{_fmt_timeout(ai_timeout)}", callback_data="cmd_set_ai_timeout")],
        [InlineKeyboardButton(f"⌨️ 命令等待：{_fmt_command_timeout(command_timeout)}", callback_data="cmd_set_command_timeout")],
        [InlineKeyboardButton(f"🔁 Agent轮数：{_fmt_agent_max_iterations(agent_max_iterations)}", callback_data="cmd_set_agent_max_iterations")],
        [InlineKeyboardButton(f"💭 空闲提醒：{_fmt_idle_message_interval(idle_interval)}", callback_data="cmd_set_idle_message_interval")],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_more_settings")]
    ])

def build_timeout_settings_text() -> str:
    ai_timeout = UserDataManager.get('stream_timeout', 0)
    command_timeout = UserDataManager.get('agent_command_timeout', DEFAULT_AGENT_COMMAND_TIMEOUT)
    agent_max_iterations = UserDataManager.get('agent_max_iterations', DEFAULT_AGENT_MAX_ITERATIONS)
    idle_interval = UserDataManager.get('idle_message_interval', DEFAULT_IDLE_MESSAGE_INTERVAL)
    return (
        "⏱️ <b>超时设置</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"💬 AI回复超时：<b>{_fmt_timeout(ai_timeout)}</b>\n"
        f"⌨️ 命令等待窗口：<b>{_fmt_command_timeout(command_timeout)}</b>\n"
        f"🔁 Agent最大轮数：<b>{_fmt_agent_max_iterations(agent_max_iterations)}</b>\n"
        f"💭 空闲提醒间隔：<b>{_fmt_idle_message_interval(idle_interval)}</b>\n\n"
        "AI回复超时控制等待模型响应的时间；命令等待窗口控制 run 的最长等待，也是 shell 状态判断的硬上限；"
        "Agent最大轮数控制从最近一条真实用户消息开始，系统结果继续调用 AI 的累计次数；"
        "空闲提醒间隔控制用户多久没发消息后自动生成一条提醒回复。"
    )

def get_ai_timeout_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("30s", callback_data="set_ai_timeout_30"),
         InlineKeyboardButton("60s", callback_data="set_ai_timeout_60"),
         InlineKeyboardButton("120s", callback_data="set_ai_timeout_120")],
        [InlineKeyboardButton("300s", callback_data="set_ai_timeout_300"),
         InlineKeyboardButton("∞ 无限", callback_data="set_ai_timeout_0")],
        [InlineKeyboardButton("✍️ 自定义", callback_data="set_ai_timeout_custom")],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_timeout_settings")]
    ])

def get_command_timeout_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("60s", callback_data="set_command_timeout_60"),
         InlineKeyboardButton("120s", callback_data="set_command_timeout_120")],
        [InlineKeyboardButton("300s", callback_data="set_command_timeout_300"),
         InlineKeyboardButton("600s", callback_data="set_command_timeout_600")],
        [InlineKeyboardButton("✍️ 自定义", callback_data="set_command_timeout_custom")],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_timeout_settings")]
    ])

def get_agent_max_iterations_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5轮", callback_data="set_agent_max_iterations_5"),
         InlineKeyboardButton("10轮", callback_data="set_agent_max_iterations_10")],
        [InlineKeyboardButton("20轮", callback_data="set_agent_max_iterations_20"),
         InlineKeyboardButton("30轮", callback_data="set_agent_max_iterations_30")],
        [InlineKeyboardButton("✍️ 自定义", callback_data="set_agent_max_iterations_custom")],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_timeout_settings")]
    ])

def get_idle_message_interval_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1小时", callback_data="set_idle_message_interval_3600"),
         InlineKeyboardButton("24小时", callback_data="set_idle_message_interval_86400")],
        [InlineKeyboardButton("∞关闭", callback_data="set_idle_message_interval_0"),
         InlineKeyboardButton("✍️ 自定义", callback_data="set_idle_message_interval_custom")],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_timeout_settings")]
    ])

def get_command_blacklist_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 批量添加", callback_data="act_add_command_blacklist")],
        [InlineKeyboardButton("⭐ 查看推荐名单", callback_data="view_recommended_blacklist")],
        [InlineKeyboardButton("🔄 从文件重载", callback_data="act_reload_command_blacklist")],
        [InlineKeyboardButton("🧹 清空黑名单", callback_data="confirm_clear_command_blacklist")],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_more_settings")]
    ])


def get_memory_menu():
    """记忆管理主菜单。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 添加记忆", callback_data="act_add_memory")],
        [InlineKeyboardButton("📋 列出全部", callback_data="act_list_memory")],
        [InlineKeyboardButton("🗑️ 删除单条", callback_data="act_delete_memory_menu:1")],
        [InlineKeyboardButton("🧹 清空全部", callback_data="confirm_clear_user_memory")],
        [InlineKeyboardButton("🔙 返回", callback_data="act_main_menu")]
    ])


def build_memory_menu_text(title: str = "记忆管理") -> str:
    """记忆管理界面文案：显示条数 + 前 N 条预览。"""
    files = list_memory_files()
    preview_parts = []
    for idx, filename in enumerate(files[:10], start=1):
        content = read_memory_file(filename).strip()
        # 单行预览，过长截断
        one_line = ' '.join(content.split())
        if len(one_line) > 50:
            one_line = one_line[:50] + '…'
        preview_parts.append(f"{idx}. {safe_text(one_line)}")
    if not preview_parts:
        preview = "（暂无记忆）"
    else:
        preview = '\n'.join(preview_parts)
        if len(files) > 10:
            preview += f"\n... 还有 {len(files) - 10} 条"
    return (
        f"🧠 <b>{safe_text(title)}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"当前记忆: <b>{len(files)} 条</b>\n"
        f"存储位置: <code>{safe_text(to_display_path(MEMORY_DIR))}</code>\n\n"
        f"<pre>{preview}</pre>\n\n"
        "每条记忆无长度限制，可分段发送（自动拼接为一条）后保存。\n"
        "保存后立即拼入 system prompt，无需重启。"
    )


def get_memory_delete_keyboard(page: int) -> InlineKeyboardMarkup:
    """单条删除分页键盘：每页 8 条，带翻页。"""
    files = list_memory_files()
    PER_PAGE = 8
    total_pages = max(1, math.ceil(len(files) / PER_PAGE))
    page = max(1, min(page, total_pages))
    page_files = files[(page - 1) * PER_PAGE: page * PER_PAGE]
    base_index = (page - 1) * PER_PAGE
    rows = []
    for offset, filename in enumerate(page_files):
        idx = base_index + offset + 1
        content = read_memory_file(filename).strip()
        one_line = ' '.join(content.split())
        if len(one_line) > 40:
            one_line = one_line[:40] + '…'
        # 文件名经 CallbackDataStore 处理，避免超长或特殊字符问题
        cb = CallbackDataStore.store(f"act_delete_memory:{filename}")
        rows.append([InlineKeyboardButton(f"🗑️ #{idx} {one_line}", callback_data=cb)])
    rows.append([
        InlineKeyboardButton("◀️", callback_data=f"act_delete_memory_menu:{max(1, page - 1)}"),
        InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"),
        InlineKeyboardButton("▶️", callback_data=f"act_delete_memory_menu:{min(total_pages, page + 1)}"),
    ])
    rows.append([InlineKeyboardButton("🔙 返回", callback_data="menu_memory")])
    return InlineKeyboardMarkup(rows)


def build_command_blacklist_text(title: str = "Agent 命令黑名单") -> str:
    patterns = AgentCommandBlacklist.get_patterns()
    preview = "\n".join(patterns[:30])
    if len(patterns) > 30:
        preview += f"\n... 还有 {len(patterns) - 30} 条"
    preview = safe_text(preview) if preview else "（当前为空，内置危险命令关键词已解除限制）"
    return (
        f"🚫 <b>{safe_text(title)}</b>\n\n"
        f"当前启用: <b>{len(patterns)} 条</b>\n"
        f"文件: <code>{safe_text(AgentCommandBlacklist.get_display_path())}</code>\n\n"
        f"<pre>{preview}</pre>\n\n"
        "保存后立即生效，无需重启。手动编辑文件后，点“从文件重载”即可载入。\n"
        "批量输入：可以一次粘贴多条；每条一行，或用独立一行三个横杠 <code>---</code> 分隔。"
    )

def build_recommended_blacklist_text() -> str:
    recommended = "\n".join(AgentCommandBlacklist.RECOMMENDED_PATTERNS)
    preview = safe_text(recommended[:3200])
    if len(recommended) > 3200:
        preview += "\n..."
    return (
        "⭐ <b>推荐禁止名单</b>\n\n"
        "这些是原先内置的危险 shell 关键词。现在不会默认拦截，用户可以一键追加到自定义黑名单。\n\n"
        f"<pre>{preview}</pre>"
    )

def get_prompts_menu():
    keyboard = [
        [InlineKeyboardButton(f"📝 {PromptFileManager.get_label(key)}", callback_data=f"view_prompt:{key}")]
        for key in PromptFileManager.FILES
    ]
    keyboard.append([InlineKeyboardButton("🔄 从文件重载提示词", callback_data="act_reload_prompts")])
    keyboard.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="act_main_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_prompt_detail_menu(key: str):
    buttons = []
    buttons.append([InlineKeyboardButton("✍️ 修改提示词", callback_data=f"modify_prompt:{key}")])
    buttons.append([
        InlineKeyboardButton("🔄 从文件重载", callback_data=f"reload_prompt:{key}"),
        InlineKeyboardButton("📥 下载提示词", callback_data=f"download_prompt:{key}"),
    ])
    buttons.append([InlineKeyboardButton("🔙 返回", callback_data="menu_prompts")])
    return InlineKeyboardMarkup(buttons)

def get_prompt_edit_note(key: str) -> str:
    if key == 'unauthorized_reply_messages':
        return (
            "未授权用户回复语录支持多条。\n"
            "可以一次发送多条，条目之间用独立一行三个横杠 <code>---</code> 分隔；\n"
            "也可以一次发送一条、多次发送，系统会自动用独立一行三个横杠 <code>---</code> 拼接。"
        )
    if key == 'idle_message_prompt':
        return "空闲提醒提示词里的 --- 只是提示词边界文本；直接输入会按原文保存，不会自动追加 ---。"
    return ""

async def show_prompt_detail(query, key: str, title_suffix: str = ""):
    if key not in PromptFileManager.FILES:
        await query.answer("提示词不存在", show_alert=True)
        return

    curr = get_runtime_prompt(key) if key in {'assistant_prompt', 'global_prompt_addon'} else PromptFileManager.get(key)
    preview = safe_text(curr)[:500] + "..." if len(curr) > 500 else safe_text(curr)
    await query.message.edit_text(
        f"📝 <b>{safe_text(PromptFileManager.get_label(key))}{safe_text(title_suffix)}</b>\n"
        f"<i>文件: {safe_text(PromptFileManager.get_path(key))}</i>\n"
        f"{safe_text(get_prompt_edit_note(key))}\n\n<pre>{preview}</pre>",
        reply_markup=get_prompt_detail_menu(key),
        parse_mode=constants.ParseMode.HTML
    )

def get_providers_menu():
    providers = UserDataManager.get('providers', {})
    keyboard = []
    for name in providers:
        cb = CallbackDataStore.store(f"view_prov_{name}")
        keyboard.append([InlineKeyboardButton(f"{get_provider_usage_badges(name)} {name}", callback_data=cb)])
    keyboard.append([InlineKeyboardButton("➕ 添加提供商", callback_data="act_add_provider")])
    keyboard.append([
        InlineKeyboardButton("📤 导出配置", callback_data="export_provider_config"),
        InlineKeyboardButton("📥 导入配置", callback_data="import_provider_config")
    ])
    keyboard.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="act_main_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_provider_detail_menu(prov_name):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 模型", callback_data=CallbackDataStore.store(f"prov_models_{prov_name}"))],
        [InlineKeyboardButton("✏️ 名称", callback_data=CallbackDataStore.store(f"edit_pname_{prov_name}")),
         InlineKeyboardButton("🔗 URL", callback_data=CallbackDataStore.store(f"edit_purl_{prov_name}"))],
        [InlineKeyboardButton("🔑 Key", callback_data=CallbackDataStore.store(f"edit_pkey_{prov_name}")),
         InlineKeyboardButton("🗑️ 删除", callback_data=CallbackDataStore.store(f"del_prov_{prov_name}"))],
        [InlineKeyboardButton("🔙 返回", callback_data="menu_providers")]
    ])


def get_default_model_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 对话模型", callback_data="target_chat_models"),
         InlineKeyboardButton("🖼️ 媒体模型", callback_data="target_media_models")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="act_main_menu")]
    ])


def get_default_model_provider_menu(target: str):
    providers = UserDataManager.get('providers', {})
    keyboard = []
    current_provider = get_model_target_provider_name(target)
    for name in providers:
        cb = CallbackDataStore.store(f"pick_model_provider_{target}_{name}")
        marker = "🟢" if name == current_provider else "⚪"
        keyboard.append([InlineKeyboardButton(f"{marker} {name}", callback_data=cb)])
    keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="menu_default_models")])
    return InlineKeyboardMarkup(keyboard)


def make_manage_marker_fn(prov_name: str):
    """管理模式标记：模型是当前对话或媒体默认时返回 ✅"""
    def marker_fn(model_name: str) -> Optional[str]:
        chat_model = UserDataManager.get('default_model')
        chat_prov = UserDataManager.get('active_provider_key')
        media_model = UserDataManager.get('default_media_model')
        media_prov = UserDataManager.get('default_media_provider_key')
        if (chat_prov == prov_name and chat_model == model_name) or \
           (media_prov == prov_name and media_model == model_name):
            return "✅"
        return None
    return marker_fn


def make_select_marker_fn(target: str, prov_name: str):
    """选择模式标记：模型是当前 target 的默认时返回 ✅"""
    meta = get_model_target_meta(target)
    def marker_fn(model_name: str) -> Optional[str]:
        current_model = UserDataManager.get(meta['model_state_key'])
        current_prov = UserDataManager.get(meta['provider_state_key'])
        if current_prov == prov_name and current_model == model_name:
            return "✅"
        return None
    return marker_fn


def make_fetched_saved_marker_fn(prov_name: str):
    """联网获取列表标记：模型已经保存到该提供商时返回 ✅。"""
    providers = UserDataManager.get('providers', {})
    saved_models = set(providers.get(prov_name, {}).get('models', []))

    def marker_fn(model_name: str) -> Optional[str]:
        return "✅" if model_name in saved_models else None

    return marker_fn


def build_fetched_models_view(provider_name: str):
    """根据缓存状态构建联网模型列表，并让搜索结果返回完整列表。"""
    models = UserDataManager.get('fetched_cache', [])
    page = UserDataManager.get('temp_page', 1) or 1
    filter_text = UserDataManager.get('temp_filter')
    exit_callback = UserDataManager.get('temp_back_callback') or f"mng_saved_{provider_name}"
    list_back_callback = "back_fetched_all_models" if filter_text else exit_callback
    kb = build_magic_keyboard(
        models,
        page,
        "pick_fetch_",
        list_back_callback,
        "act_search_fetched",
        filter_text,
        marker_fn=make_fetched_saved_marker_fn(provider_name)
    )
    visible_count = sum(
        1 for model in models
        if not filter_text or filter_text.lower() in model.lower()
    )
    if filter_text:
        title = f"🔍 搜索 '{safe_text(filter_text)}'：找到 {visible_count} 个模型:"
    else:
        title = f"🌐 找到了 {len(models)} 个模型:"
    return title, kb


def build_saved_models_view(provider_name: str):
    """根据当前状态构建已保存模型列表；搜索结果返回完整的已保存列表。"""
    providers = UserDataManager.get('providers', {})
    models = providers.get(provider_name, {}).get('models', [])
    page = UserDataManager.get('temp_page', 1) or 1
    filter_text = UserDataManager.get('temp_saved_filter')
    list_back_callback = "back_saved_all_models" if filter_text else f"view_prov_{provider_name}"
    kb = build_magic_keyboard(
        models,
        page,
        f"act_saved_{provider_name}_",
        list_back_callback,
        "act_search_saved",
        filter_text,
        extra_buttons=[
            InlineKeyboardButton("➕ 手写", callback_data=f"act_manual_mod_{provider_name}"),
            InlineKeyboardButton("⚡ 联网获取", callback_data=f"fetch_market_{provider_name}"),
        ],
        marker_fn=make_manage_marker_fn(provider_name)
    )
    visible_count = sum(
        1 for model in models
        if not filter_text or filter_text.lower() in model.lower()
    )
    if filter_text:
        title = (
            f"🔍 <b>{safe_text(provider_name)}</b> 已保存的模型\n\n"
            f"搜索 '{safe_text(filter_text)}'：找到 {visible_count} 个模型:"
        )
    else:
        title = (
            f"🧰 <b>{safe_text(provider_name)}</b> 已保存的模型\n\n"
            "这里可以继续新增、联网获取、搜索，或点击模型进行设置。"
        )
    return title, kb


def build_saved_models_keyboard(provider_name: str, target: Optional[str] = None, page: int = 1):
    UserDataManager.set('temp_viewing_prov', provider_name)
    UserDataManager.set('temp_list_type', 'saved')
    UserDataManager.set('temp_page', page)
    UserDataManager.set('temp_saved_filter', None)
    UserDataManager.set('temp_model_target', target)
    UserDataManager.set('temp_model_menu_mode', 'manage')
    UserDataManager.set('temp_back_callback', f"view_prov_{provider_name}")
    _, kb = build_saved_models_view(provider_name)
    return kb


def build_model_detail_menu(prov_name: str, model_name: str, back_callback: Optional[str] = None):
    """构建模型详情菜单：设为对话模型、设为媒体模型、删除模型。"""
    back_callback = back_callback or f"mng_saved_{prov_name}"
    set_chat_cb = CallbackDataStore.store(f"set_mdl|chat|{prov_name}|{model_name}")
    set_media_cb = CallbackDataStore.store(f"set_mdl|media|{prov_name}|{model_name}")
    del_cb = CallbackDataStore.store(f"do_del|{prov_name}|{model_name}")

    chat_model = UserDataManager.get('default_model')
    chat_prov = UserDataManager.get('active_provider_key')
    media_model = UserDataManager.get('default_media_model')
    media_prov = UserDataManager.get('default_media_provider_key')

    status_parts = []
    if chat_prov == prov_name and chat_model == model_name:
        status_parts.append("💬 当前对话模型")
    if media_prov == prov_name and media_model == model_name:
        status_parts.append("🖼️ 当前媒体模型")
    status = "、".join(status_parts) if status_parts else "未设为默认"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 设为对话模型", callback_data=set_chat_cb)],
        [InlineKeyboardButton("🖼️ 设为媒体模型", callback_data=set_media_cb)],
        [InlineKeyboardButton("🗑️ 删除模型", callback_data=del_cb)],
        [InlineKeyboardButton("🔙 返回", callback_data=back_callback)]
    ])
    text = (
        f"⚙️ <b>{safe_text(model_name)}</b>\n"
        f"提供商: {safe_text(prov_name)}\n"
        f"状态: {status}"
    )
    return text, kb


def build_model_selection_keyboard(provider_name: str, target: str, page: int = 1):
    providers = UserDataManager.get('providers', {})
    models = providers.get(provider_name, {}).get('models', [])
    UserDataManager.set('temp_viewing_prov', provider_name)
    UserDataManager.set('temp_list_type', 'saved')
    UserDataManager.set('temp_page', page)
    UserDataManager.set('temp_model_target', target)
    UserDataManager.set('temp_model_menu_mode', 'select')
    UserDataManager.set('temp_back_callback', f"target_{target}_models")
    return build_magic_keyboard(
        models,
        page,
        "pick_default_",
        f"target_{target}_models",
        marker_fn=make_select_marker_fn(target, provider_name)
    )

# --- ☆ 核心：授权用户校验与未授权用户通报系统 ☆ ---
async def handle_unauthorized_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理未授权用户的互动"""
    user = update.effective_user
    chat = update.effective_chat
    
    rejection_messages = get_unauthorized_reply_messages()
    rejection_msg = random.choice(rejection_messages) if rejection_messages else ''
    
    try:
        if not rejection_msg:
            raise ValueError("unauthorized reply messages file is empty")
        if update.callback_query:
            await update.callback_query.answer(rejection_msg[:180], show_alert=True)
            await context.bot.send_message(chat_id=chat.id, text=rejection_msg)
        elif update.message:
            await update.message.reply_text(rejection_msg)
    except Exception as e:
        logger.error(f"无法回复未授权用户: {e}")

    # 收集情报
    unauthorized_input = "未知内容"
    action_type = "未知"
    
    if update.message:
        if update.message.text:
            unauthorized_input = update.message.text
            action_type = "发送文本"
        elif update.message.document:
            unauthorized_input = f"[文件] {update.message.document.file_name}"
            action_type = "发送文件"
        elif update.message.sticker:
            unauthorized_input = f"[贴纸] {update.message.sticker.emoji or '无表情'}"
            action_type = "发送贴纸"
        elif update.message.photo:
            unauthorized_input = "[图片]"
            action_type = "发送图片"
    elif update.callback_query:
        unauthorized_input = f"[按钮数据] {update.callback_query.data}"
        action_type = "点击按钮"

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    report = (
        f"🚨 <b>访问控制通知：未授权请求</b> 🚨\n"
        f"━━━━━━━━━━━━━━\n"
        f"⏰ <b>时间:</b> {current_time}\n"
        f"👤 <b>用户ID:</b> <code>{user.id}</code>\n"
        f"👤 <b>昵称:</b> {safe_text(user.full_name)}\n"
        f"🔗 <b>用户名:</b> @{safe_text(user.username or '无')}\n"
        f"📝 <b>行为:</b> {action_type}\n"
        f"📥 <b>对方发送内容:</b>\n"
        f"<pre>{safe_text(unauthorized_input)}</pre>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📤 <b>已发送回复:</b>\n"
        f"<i>{safe_text(rejection_msg)}</i>\n"
        f"━━━━━━━━━━━━━━\n"
        f"该请求已被拦截。"
    )

    try:
        await context.bot.send_message(
            chat_id=BotConfig.AUTHORIZED_USER_ID,
            text=report,
            parse_mode=constants.ParseMode.HTML
        )
        logger.info(f"已拦截未授权用户 {user.id} 并向用户汇报。")
    except Exception as e:
        logger.error(f"向用户汇报失败: {e}")
    
    # 记录到全局表和未授权用户专用表 - 防御性包装，避免DB失败导致主流程崩溃
    try:
        await GlobalRecorder.record(
            msg_type=MessageType.SYSTEM_OP,
            role='system',
            content=f"[未授权用户警报] 用户 {user.full_name}(@{user.username or '无'}) ID:{user.id} 尝试{action_type}: {unauthorized_input}",
            chat_id=BotConfig.AUTHORIZED_USER_ID
        )
        await GlobalRecorder.record(
            msg_type=MessageType.AI_REPLY,
            role='assistant',
            content=f"[对未授权用户的回复] {rejection_msg}",
            chat_id=BotConfig.AUTHORIZED_USER_ID
        )
        
        db = await BotMemoryDB.get_instance()
        await db.record_unauthorized_access(
            user_id=user.id,
            username=user.username or '',
            full_name=user.full_name,
            action_type=action_type,
            content=unauthorized_input,
            bot_reply=rejection_msg
        )
    except Exception as e:
        logger.error(f"记录未授权用户信息失败: {e}")

async def check_authorized_user_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """中间件：检查是否是用户"""
    if not update.effective_user:
        return False
    
    if update.effective_user.id == BotConfig.AUTHORIZED_USER_ID:
        return True
    
    await handle_unauthorized_user(update, context)
    return False

# --- ☆ 流式输出处理（优化版）☆ ---
