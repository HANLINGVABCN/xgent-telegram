# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

def build_start_menu_text() -> str:
    prov_name, _ = get_current_provider()
    active_prov = prov_name if prov_name else '未设置'
    curr_model = format_model_target_summary('chat')
    media_model = format_model_target_summary('media')
    global_depth = UserDataManager.get('global_depth', 30)
    agent_mode = "开启 🟢" if UserDataManager.get('agent_mode', False) else "关闭 🔴"
    stitch_mode = get_text_stitch_mode_label()
    thinking_level = get_thinking_level_label()

    welcome_msg = (
        f"<b>XGent for Telegram 已就绪</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🛡️ 防御系统: <b>已开启</b>\n"
        f"📡 当前对话提供商: <b>{safe_text(active_prov)}</b>\n"
        f"💬 对话模型: <b>{safe_text(curr_model)}</b>\n"
        f"🖼️ 媒体模型: <b>{safe_text(media_model)}</b>\n"
        f"🌐 全局模式: <b>常驻开启</b>\n"
        f"🤖 Agent模式: <b>{agent_mode}</b>\n"
        f"🧠 思考深度: <b>{safe_text(thinking_level)}</b>\n"
        f"🧩 文字拼接: <b>{safe_text(stitch_mode)}</b>\n"
        f"📊 全局记忆深度: <b>{global_depth}条</b>\n"
        f"💾 记忆系统: <b>异步SQLite + 内存缓存</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"用户，服务正在运行"
    )
    return welcome_msg

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    
    await UserDataManager.init()
    
    # 记录用户命令
    if update.message and update.message.text:
        await GlobalRecorder.record_user_message(update.message.text, MessageType.COMMAND, update.effective_chat.id)
    
    welcome_msg = build_start_menu_text()
    
    # 记录系统操作
    await GlobalRecorder.record_system_op("启动机器人", {"command": "/start"})
    
    message = update.message or update.callback_query.message
    await message.reply_text(
        welcome_msg, 
        reply_markup=get_main_menu(), 
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_restart_system(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
        
    # 记录用户命令
    if update.message and update.message.text:
        await GlobalRecorder.record_user_message(update.message.text, MessageType.COMMAND, update.effective_chat.id)
        
    message = update.message or update.callback_query.message
    sent = await message.reply_text("🔄 服务正在重启。")
    # 给 Telegram 一点时间把提示消息发出去，避免 sys.exit 截断未完成的发送
    if sent is not None:
        await asyncio.sleep(0.3)
    
    # 记录并关闭数据库
    await GlobalRecorder.record_system_op("重启机器人")
    await restart_current_process(update.effective_chat.id, context.bot)

def _find_external_pm2_app():
    """当前进程不在 PM2 里时，看服务本身是否被 PM2 托管。

    为什么需要它：/restart 从 Telegram 发起时，当前进程就是 PM2 托管的服务，
    环境变量（PM2_HOME/pm_id）一查便知；但从 CLI 发起时，当前进程是**另一个**
    进程，环境里什么都没有——于是误报"没有守护进程"然后 sys.exit 把用户的
    CLI 会话带走，服务本身反倒没重启。按"应用工作目录 == 项目目录，或应用名
    是 install.sh 的固定名"去 pm2 jlist 里找，找到就说明服务有 PM2 兜底。
    """
    try:
        result = subprocess.run(
            ['pm2', 'jlist'], capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        apps = json.loads(result.stdout or '[]')
        if not isinstance(apps, list):
            return None
        known_names = {'xgent-telegram', 'telegram-ai-bot'}  # install.sh 的 PM2 应用名
        for app in apps:
            if not isinstance(app, dict):
                continue
            env = app.get('pm2_env') or {}
            cwd = str(env.get('pm_cwd') or '')
            name = str(app.get('name') or '')
            if cwd == PROJECT_ROOT or name in known_names:
                return name or None
    except Exception:
        return None
    return None


async def restart_current_process(chat_id: int, bot: Any = None):
    """彻底重启进程，确保重新加载所有配置和代码。

    支持三种守护模式：
    - PM2 / nohup：调用 install.sh restart（detached，含完整 stop+start），由它拉起新进程。
    - systemd：依赖 unit 的 Restart= 自动拉起。
    - 兜底（无任何守护）：先给用户发提示，再退出，避免静默掉线。

    CLI 场景（bot 是 CliBot 垫片）：当前进程不是被托管的那个——按环境变量
    判断守护一定落空，所以额外用 pm2 jlist 查服务是否被外部 PM2 托管；查到
    就照样触发 install.sh restart 重启服务，而 CLI 会话本身**不退出**。
    退出前写入重启标记（PID + 时间戳），新进程启动时据此判断"代码是否真的换了"。
    """
    # CLI 垫片上带着这个标记；真 PTB bot 没有。
    from_cli = bool(getattr(bot, '_is_xgent_cli_bot', False)) if bot is not None else False

    db = await BotMemoryDB.get_instance()
    await db.set_config('restart_notify_chat_id', chat_id)
    # 写入重启校验标记：新进程启动时对比 PID，判断是否真的换了新进程/新代码
    await db.set_config('restart_expected_ts', time.time())
    await db.set_config('restart_expected_pid', os.getpid())
    # CLI 会话要继续跑，不能提前关库；只有即将退出的服务进程才关。
    if not from_cli:
        await db.close()

    install_sh = os.path.join(PROJECT_ROOT, 'install.sh')
    has_install_sh = os.path.exists(install_sh)
    is_pm2 = any(k in os.environ for k in ('PM2_HOME', 'pm_id', 'PM2_USAGE'))
    is_nohup = os.path.exists(os.path.join(PROJECT_ROOT, 'xgent.pid'))
    # systemd 会在被托管进程的环境里注入 INVOCATION_ID（及 JOURNAL_STREAM）
    is_systemd = 'INVOCATION_ID' in os.environ or 'JOURNAL_STREAM' in os.environ

    # 当前进程不受托管时，服务本身可能仍被 PM2 托管（CLI 发起的场景）。
    external_pm2 = None
    if not (is_pm2 or is_nohup or is_systemd):
        external_pm2 = await asyncio.to_thread(_find_external_pm2_app)

    async def _notify(text: str) -> None:
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.send_message(chat_id=chat_id, text=text)
            await asyncio.sleep(0.3)

    restart_via_install = False
    if is_pm2:
        logger.info("检测到 PM2 环境，调用 install.sh restart 彻底重启")
        restart_via_install = True
    elif is_nohup and has_install_sh:
        logger.info("检测到 nohup（xgent.pid）环境，调用 install.sh restart 彻底重启")
        restart_via_install = True
    elif external_pm2 and has_install_sh:
        logger.info(f"当前进程不受托管，但服务由 PM2 托管({external_pm2})，触发 install.sh restart")
        restart_via_install = True
    elif is_systemd:
        # systemd 会按 unit 的 Restart= 策略自动拉起新进程
        logger.info("检测到 systemd 托管环境，进程退出后由 systemd 自动拉起")

    if restart_via_install:
        try:
            subprocess.Popen(
                ['bash', install_sh, 'restart'],
                cwd=PROJECT_ROOT,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"调用 install.sh restart 失败: {e}，回退到直接退出")
            # 失败时若 PM2 仍在，PM2 还会自动拉起；否则需要兜底提示
            if not is_pm2 and not external_pm2:
                await _notify(
                    "⚠️ 自动重启脚本调用失败，进程即将退出。"
                    "如果没有自动恢复，请手动运行 install.sh 重启。"
                )
    elif not is_systemd:
        # 兜底：既不是 PM2/nohup 也不是 systemd，退出后没有守护进程拉起，
        # 先提示用户手动重启，避免静默掉线后不知所措。
        logger.warning("未检测到 PM2/nohup/systemd 守护，进程退出后可能无法自动恢复")
        if from_cli:
            await _notify(
                "⚠️ 未检测到 PM2/nohup/systemd 守护，未触发自动重启。\n"
                "请手动到服务器运行 install.sh 重启。CLI 会话保持。"
            )
        else:
            await _notify(
                "⚠️ 检测到当前没有 PM2/nohup/systemd 守护进程。\n"
                "进程即将退出，可能无法自动重启。\n"
                "如果 Bot 没有恢复，请手动到服务器运行 install.sh 启动。"
            )

    if from_cli:
        # CLI 不是被托管的进程：服务的重启交给 install.sh，当前会话保持。
        if restart_via_install:
            await _notify("🔄 已触发服务重启（PM2 托管）。CLI 会话保持，可继续使用。")
        return

    # 彻底退出进程，让外层管理器（PM2/systemd/nohup）重新启动
    # 这样确保重新加载 .env 和所有配置文件，以及最新代码
    logger.info("进程即将退出以完成重启")
    sys.exit(0)

async def cmd_update_system(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return

    if update.message and update.message.text:
        await GlobalRecorder.record_user_message(update.message.text, MessageType.COMMAND, update.effective_chat.id)

    await UserDataManager.init()
    message = update.message or update.callback_query.message
    await send_update_source_menu(message)

async def send_update_source_menu(message: Any):
    await message.reply_text(
        "⬆️ <b>选择更新来源</b>\n\n"
        "请选择这次要从哪里拉取最新代码：\n"
        "1. 正常更新：从 <code>xgent-telegram</code> 项目拉取。\n"
        "2. Test 更新：从 <code>xgent-telegram-test</code> 私有目录拉取，需要 GitHub Token。",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("1 正常更新（bot 项目）", callback_data="select_update_source_normal")],
            [InlineKeyboardButton("2 Test 更新（私有，需要 Token）", callback_data="select_update_source_test")],
            [InlineKeyboardButton("取消", callback_data="menu_more_settings")]
        ])
    )

async def request_update_github_token(message: Any, update_url: str):
    set_update_source(update_url)
    UserDataManager.set('state', BotState.SET_UPDATE_TOKEN)
    UserDataManager.set('pending_update_zip_url', BotConfig.UPDATE_ZIP_URL)
    await message.reply_text(
        "🔐 <b>需要 GitHub Token</b>\n\n"
        "你选择的是 test 私有目录：\n"
        f"<code>{safe_text(BotConfig.UPDATE_ZIP_URL)}</code>\n\n"
        "请发送一个 Fine-grained GitHub Token，权限只需要该仓库 <b>Contents: Read-only</b>。\n"
        "收到后会写入项目根目录的 <code>.env</code>，然后继续更新确认。\n\n"
        "<i>发送 cancel 可取消。</i>",
        parse_mode=constants.ParseMode.HTML
    )

async def show_update_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.callback_query.message
    await send_update_confirmation_message(message)

async def send_update_confirmation_message(message: Any):
    source_label = get_update_source_label(BotConfig.UPDATE_ZIP_URL)
    auth_text = (
        "已检测到 <code>UPDATE_GITHUB_TOKEN</code>，将用于本次 GitHub 下载请求。\n\n"
        if is_test_update_source(BotConfig.UPDATE_ZIP_URL)
        else "正常更新源不需要 GitHub Token。\n\n"
    )
    await message.reply_text(
        "⬆️ <b>更新确认</b>\n\n"
        f"更新会从 <b>{safe_text(source_label)}</b> 下载最新代码并覆盖当前项目文件，然后自动重启。\n"
        f"更新源：<code>{safe_text(BotConfig.UPDATE_ZIP_URL)}</code>\n"
        "运行数据、数据库、日志、存储目录、虚拟环境和 Git 目录会保留。\n"
        "skill 文件随仓库更新（<code>skill-private/</code> 私有 skill 永不覆盖）。\n\n"
        f"{auth_text}"
        "请选择是否覆盖 <code>prompts/</code> 提示词：\n"
        "• 保留：继续使用服务器当前提示词。\n"
        "• 覆盖：使用 GitHub 最新版本，覆盖前会把 prompts/ 备份到新文件夹。\n\n"
        "建议：如果你在机器人里手动改过提示词，优先选“保留当前提示词”。",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("保留当前提示词", callback_data="do_update_keep_custom_files")],
            [InlineKeyboardButton("覆盖并备份提示词", callback_data="do_update_overwrite_custom_files")],
            [InlineKeyboardButton("取消", callback_data="menu_more_settings")]
        ])
    )

async def perform_update_system(update: Update, context: ContextTypes.DEFAULT_TYPE, overwrite_local_custom_files: bool):
    if not await check_authorized_user_middleware(update, context):
        return

    message = update.message or update.callback_query.message
    custom_files_mode = "覆盖并自动备份 prompts/" if overwrite_local_custom_files else "保留当前 prompts/"
    status_msg = await message.reply_text(
        "⬇️ 正在从更新源下载最新代码并覆盖当前目录...\n"
        f"本地文件策略：{custom_files_mode}\n"
        "运行数据、数据库、日志、存储目录、虚拟环境和 Git 目录会保留。"
    )

    try:
        result = await asyncio.to_thread(download_and_apply_project_update, overwrite_local_custom_files)
    except Exception as e:
        logger.exception("项目更新失败")
        await status_msg.edit_text(
            f"❌ 更新失败：<code>{safe_text(format_provider_exception(e))}</code>",
            parse_mode=constants.ParseMode.HTML
        )
        return

    reload_result = None
    if overwrite_local_custom_files:
        try:
            reload_result = await reload_overwritten_custom_prompts()
            result["reloaded_custom_prompts"] = reload_result
        except Exception as e:
            logger.exception("覆盖更新后重载提示词失败")
            await status_msg.edit_text(
                f"❌ 更新文件已覆盖，但提示词重载失败：<code>{safe_text(format_provider_exception(e))}</code>",
                parse_mode=constants.ParseMode.HTML
            )
            return

    await GlobalRecorder.record_system_op(
        "更新机器人代码",
        {
            "source": result.get("source"),
            "count": result.get("count"),
            "files": result.get("files"),
            "truncated": result.get("truncated"),
            "overwrite_local_custom_files": result.get("overwrite_local_custom_files"),
            "backup_path": result.get("backup_path"),
            "skipped_local_custom_files": result.get("skipped_local_custom_files"),
            "reloaded_custom_prompts": result.get("reloaded_custom_prompts"),
        },
        update.effective_chat.id
    )

    backup_line = ""
    if result.get("backup_path"):
        backup_line = f"\nprompts/ 备份: {result.get('backup_path')}"
    skipped_line = ""
    if result.get("skipped_local_custom_files"):
        skipped_line = f"\n已保留 prompts/，跳过 {int(result.get('skipped_local_custom_files') or 0)} 个自定义文件。"
    reload_line = ""
    if reload_result:
        reload_line = (
            f"\n已从覆盖后的 prompts/ 重载 {int(reload_result.get('prompt_files') or 0)} 个提示词文件，"
            f"并同步 {int(reload_result.get('runtime_prompts') or 0)} 个运行时提示词。"
        )

    if result.get("overwrite_local_custom_files"):
        success_title = f"✅ 已覆盖 {int(result.get('count') or 0)} 个文件，正在自动重启。"
    else:
        success_title = f"✅ 已更新 {int(result.get('count') or 0)} 个非自定义文件，正在自动重启。"

    await status_msg.edit_text(
        f"{success_title}"
        f"{skipped_line}{reload_line}{backup_line}"
    )
    # 给 Telegram 一点时间把“更新成功”消息发出去，避免 sys.exit 截断
    await asyncio.sleep(0.4)
    await restart_current_process(update.effective_chat.id, context.bot)

def build_provider_config_export() -> Dict[str, Any]:
    """构建可移植的 Provider 配置；API Key 会原样包含在导出文件中。

    刻意不做掩码：导出的唯一用途就是迁移/恢复，掩码后的文件无法导入，
    默认给一份不可用的文件只会把主流程弄坏。接收方是已授权的用户本人，
    发送时会附带明确的保管提示。
    """
    providers = UserDataManager.get('providers', {}) or {}
    exported_providers: Dict[str, Dict[str, Any]] = {}
    for name, provider in providers.items():
        exported_providers[str(name)] = {
            'base_url': str(provider.get('base_url', '')),
            'api_key': str(provider.get('api_key', '')),
            'models': [str(model) for model in provider.get('models', [])],
            'api_format': str(provider.get('api_format', 'openai'))
        }

    return {
        'format': PROVIDER_CONFIG_FORMAT,
        'version': PROVIDER_CONFIG_VERSION,
        'exported_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'providers': exported_providers,
        'defaults': {
            'active_provider': UserDataManager.get('active_provider_key'),
            'default_model': UserDataManager.get('default_model'),
            'default_media_provider': UserDataManager.get('default_media_provider_key'),
            'default_media_model': UserDataManager.get('default_media_model')
        }
    }


def parse_provider_config_import(raw_bytes: bytes) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """解析并严格校验 Provider 配置 JSON。"""
    if not raw_bytes:
        raise ValueError('配置文件为空')
    if len(raw_bytes) > PROVIDER_CONFIG_MAX_BYTES:
        raise ValueError(f'配置文件不能超过 {PROVIDER_CONFIG_MAX_BYTES // 1024 // 1024} MB')
    try:
        payload = json.loads(raw_bytes.decode('utf-8-sig'))
    except UnicodeDecodeError as exc:
        raise ValueError('配置文件必须使用 UTF-8 编码') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列') from exc

    if not isinstance(payload, dict):
        raise ValueError('配置文件根节点必须是 JSON 对象')
    file_format = payload.get('format')
    if file_format != PROVIDER_CONFIG_FORMAT and file_format not in LEGACY_PROVIDER_CONFIG_FORMATS:
        raise ValueError('不是 XGent for Telegram 导出的提供商配置文件')
    version = payload.get('version')
    if version != PROVIDER_CONFIG_VERSION:
        raise ValueError(f'不支持的配置版本：{version!r}')

    raw_providers = payload.get('providers')
    if not isinstance(raw_providers, dict):
        raise ValueError('providers 必须是 JSON 对象')
    if not raw_providers:
        raise ValueError('配置文件中没有提供商')
    if len(raw_providers) > PROVIDER_CONFIG_MAX_PROVIDERS:
        raise ValueError(f'一次最多导入 {PROVIDER_CONFIG_MAX_PROVIDERS} 个提供商')

    providers: Dict[str, Dict[str, Any]] = {}
    for raw_name, raw_provider in raw_providers.items():
        if not isinstance(raw_name, str):
            raise ValueError('提供商名称必须是字符串')
        name = raw_name.strip()
        if not name or len(name) > 20 or any(ord(ch) < 32 for ch in name):
            raise ValueError(f'提供商名称无效：{raw_name!r}（不能为空，最多 20 个字符）')
        if name in providers:
            raise ValueError(f'提供商名称去除首尾空格后重复：{name}')
        if not isinstance(raw_provider, dict):
            raise ValueError(f'提供商 {name} 的配置必须是 JSON 对象')

        base_url = raw_provider.get('base_url')
        api_key = raw_provider.get('api_key')
        api_format = raw_provider.get('api_format', 'openai')
        models = raw_provider.get('models', [])
        if not isinstance(base_url, str) or not base_url.strip().lower().startswith(('http://', 'https://')):
            raise ValueError(f'提供商 {name} 的 base_url 必须以 http:// 或 https:// 开头')
        if not isinstance(api_key, str):
            raise ValueError(f'提供商 {name} 的 api_key 必须是字符串')
        if not isinstance(api_format, str) or api_format not in VALID_PROVIDER_API_FORMATS:
            raise ValueError(f'提供商 {name} 的 api_format 无效')
        if not isinstance(models, list):
            raise ValueError(f'提供商 {name} 的 models 必须是数组')

        normalized_models: List[str] = []
        seen_models = set()
        for model in models:
            if not isinstance(model, str):
                raise ValueError(f'提供商 {name} 的模型名称必须是字符串')
            model_name = model.strip()
            if not model_name:
                continue
            if len(model_name) > 300:
                raise ValueError(f'提供商 {name} 存在过长的模型名称')
            if model_name not in seen_models:
                normalized_models.append(model_name)
                seen_models.add(model_name)

        providers[name] = {
            'base_url': base_url.strip(),
            'api_key': api_key.replace(' ', ''),
            'models': normalized_models,
            'api_format': api_format
        }

    defaults = payload.get('defaults', {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError('defaults 必须是 JSON 对象')
    return providers, defaults


async def apply_provider_config_import(
        providers: Dict[str, Dict[str, Any]], defaults: Dict[str, Any], mode: str = 'merge') -> Dict[str, Any]:
    """按 merge/replace 模式导入 Provider，并仅恢复有效的默认模型选择。"""
    if mode not in {'merge', 'replace'}:
        raise ValueError(f'无效的导入模式：{mode}')
    db = await BotMemoryDB.get_instance()
    existing_names = set((UserDataManager.get('providers', {}) or {}).keys())
    imported_names = set(providers.keys())
    overwritten = len(existing_names.intersection(imported_names))
    removed = len(existing_names - imported_names) if mode == 'replace' else 0
    await db.import_providers(providers, replace=(mode == 'replace'))
    for name in existing_names.union(imported_names):
        PortalManager.remove_portal(name)
    await UserDataManager.reload_providers()
    merged = UserDataManager.get('providers', {}) or {}

    restored_defaults: List[str] = []
    skipped_defaults: List[str] = []

    if mode == 'replace':
        for target in ('chat', 'media'):
            meta = get_model_target_meta(target)
            UserDataManager.set(meta['provider_state_key'], None)
            UserDataManager.set(meta['model_state_key'], None)
            await UserDataManager.save_config(meta['provider_config_key'], None)
            await UserDataManager.save_config(meta['model_config_key'], None)

    async def restore_target(target: str, provider_field: str, model_field: str, label: str):
        if provider_field not in defaults and model_field not in defaults:
            return
        provider_name = defaults.get(provider_field)
        model_name = defaults.get(model_field)
        provider = merged.get(provider_name) if isinstance(provider_name, str) else None
        meta = get_model_target_meta(target)
        if provider and isinstance(model_name, str) and model_name in provider.get('models', []):
            await save_model_target_selection(target, provider_name, model_name)
            restored_defaults.append(label)
        elif provider_name is None and model_name is None:
            UserDataManager.set(meta['provider_state_key'], None)
            UserDataManager.set(meta['model_state_key'], None)
            await UserDataManager.save_config(meta['provider_config_key'], None)
            await UserDataManager.save_config(meta['model_config_key'], None)
            restored_defaults.append(f'{label}（未设置）')
        else:
            skipped_defaults.append(label)

    await restore_target('chat', 'active_provider', 'default_model', '默认对话模型')
    await restore_target(
        'media', 'default_media_provider', 'default_media_model', '默认媒体模型'
    )
    return {
        'mode': mode,
        'count': len(providers),
        'added': len(providers) - overwritten,
        'overwritten': overwritten,
        'removed': removed,
        'restored_defaults': restored_defaults,
        'skipped_defaults': skipped_defaults
    }


async def send_provider_config_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await UserDataManager.init()
    await UserDataManager.reload_providers()
    providers = UserDataManager.get('providers', {}) or {}
    message = update.message or update.callback_query.message
    if not providers:
        await message.reply_text('📭 当前没有可导出的提供商配置。')
        return

    payload = build_provider_config_export()
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    buffer = io.BytesIO(content)
    filename = f"提供商配置-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=InputFile(buffer, filename),
        caption=(
            f'✅ 已导出 {len(providers)} 个提供商。\n'
            '⚠️ 文件包含完整 API Key，请妥善保管，不要转发给他人。\n'
            'ℹ️ Telegram 聊天默认不是端到端加密；用完建议删除这条消息。'
        )
    )
    await GlobalRecorder.record_system_op('导出提供商配置', {'count': len(providers)})

    # 记录导出成功到上下文
    await GlobalRecorder.record_system_message(
        f"✅ 已成功导出 {len(providers)} 个提供商配置到 JSON 文件。文件包含完整 API Key。",
        update.effective_chat.id
    )


async def cmd_provider_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    UserDataManager.set('state', BotState.IDLE)
    UserDataManager.set('provider_import_mode', None)
    await UserDataManager.reload_providers()
    await update.message.reply_text(
        '🔐 <b>提供商配置导入 / 导出</b>\n\n'
        '导出文件包含提供商 URL、API Key、模型列表和默认模型选择。\n'
        '导入时可选择合并或覆盖模式。',
        reply_markup=get_providers_menu(),
        parse_mode=constants.ParseMode.HTML
    )


async def cmd_providers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    UserDataManager.set('state', BotState.IDLE)
    UserDataManager.set('provider_import_mode', None)
    await UserDataManager.reload_providers()
    await update.message.reply_text(
        "🔌 <b>提供商管理</b>\n\n"
        "这里管理连接信息，也管理每个提供商下面保存的模型列表。\n"
        "默认对话模型 / 媒体模型 请到【默认模型】里单独选择。",
        reply_markup=get_providers_menu(),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_models_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    await update.message.reply_text(
        "🎯 <b>默认模型</b>\n\n"
        f"💬 对话模型: <b>{safe_text(format_model_target_summary('chat'))}</b>\n"
        f"🖼️ 媒体模型: <b>{safe_text(format_model_target_summary('media'))}</b>\n\n"
        "这里只负责选择默认模型。\n"
        "新增 / 删除 / 联网获取模型，请去【提供商】里管理。",
        reply_markup=get_default_model_menu(),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_chat_model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    UserDataManager.set('temp_model_target', 'chat')
    await update.message.reply_text(
        "💬 <b>选择默认对话模型</b>\n\n"
        f"当前设置: <b>{safe_text(format_model_target_summary('chat'))}</b>\n\n"
        "先挑一个提供商。",
        reply_markup=get_default_model_provider_menu('chat'),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_media_model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    UserDataManager.set('temp_model_target', 'media')
    await update.message.reply_text(
        "🖼️ <b>选择默认媒体模型</b>\n\n"
        f"当前设置: <b>{safe_text(format_model_target_summary('media'))}</b>\n\n"
        "先挑一个提供商。",
        reply_markup=get_default_model_provider_menu('media'),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_prompts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    await update.message.reply_text(
        "📝 <b>提示词设置</b>\n\n选择要查看或修改的提示词。",
        reply_markup=get_prompts_menu(),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    await update.message.reply_text(
        build_settings_menu_text(),
        reply_markup=get_more_settings_menu(),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_blacklist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    await update.message.reply_text(
        build_command_blacklist_text(),
        reply_markup=get_command_blacklist_menu(),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_web_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    await update.message.reply_text(
        build_web_text(),
        reply_markup=get_web_menu(),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_thinking_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    await update.message.reply_text(
        build_thinking_level_text(),
        reply_markup=get_thinking_level_menu(),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_depth_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
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
    await update.message.reply_text(
        f"📊 <b>全局记忆深度设置</b>\n\n"
        f"当前深度: <b>{current_depth}条</b>\n"
        f"这决定了全局模式下系统能回顾多少条历史消息。",
        reply_markup=keyboard,
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_timeout_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    await update.message.reply_text(
        build_timeout_settings_text(),
        reply_markup=get_timeout_settings_menu(),
        parse_mode=constants.ParseMode.HTML
    )

async def cmd_toggle_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    new_mode = not UserDataManager.get('agent_mode', False)
    await UserDataManager.save_config('agent_mode', new_mode)
    await GlobalRecorder.record_system_op(
        f"Agent模式切换为: {'开启' if new_mode else '关闭'}",
        {"agent_mode": new_mode}
    )
    await update.message.reply_text(
        f"🤖 Agent模式已{'开启' if new_mode else '关闭'}。",
        reply_markup=get_main_menu()
    )

async def cmd_toggle_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    new_mode = not normalize_bool(UserDataManager.get('stream_mode', True), True)
    await UserDataManager.save_config('stream_mode', new_mode)
    await GlobalRecorder.record_system_op(
        f"流式输出切换为: {'开启' if new_mode else '关闭'}",
        {"stream_mode": new_mode}
    )
    await update.message.reply_text(
        f"🌊 流式输出已{'开启' if new_mode else '关闭'}。",
        reply_markup=get_main_menu()
    )


async def cmd_skills_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🧩 Skill 管理菜单：列出所有 skill 及其启用/禁用状态。"""
    if not await check_authorized_user_middleware(update, context):
        return
    await UserDataManager.init()
    skill_files = list_skill_files()
    disabled = get_disabled_skills()
    if not skill_files:
        await update.message.reply_text(
            "🧩 <b>Skill 管理</b>\n\n📭 暂无 skill 文件。",
            reply_markup=get_skills_menu(),
            parse_mode=constants.ParseMode.HTML
        )
        return
    lines = ["🧩 <b>Skill 管理</b>\n"]
    for rel_path in skill_files:
        label = os.path.splitext(os.path.basename(rel_path))[0]
        status = "🔴" if rel_path in disabled else "🟢"
        source = "🔒" if rel_path.startswith("private/") else "📦"
        lines.append(f"{status}{source} {label}")
    lines.append(f"\n📦=公有 🔒=私有  共 {len(skill_files)} 个 skill，{len(disabled)} 个已禁用。")
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=get_skills_menu(),
        parse_mode=constants.ParseMode.HTML
    )

# --- ☆ 按钮回调处理 ☆ ---
