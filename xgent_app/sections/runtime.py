# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

# --- ☆ 组件化启动：三端互不拖累 ☆ ---
# 进程自己拿着事件循环，Telegram / Web / CLI 回放 / trigger 调度各自是一个
# 独立组件，任何一个起不来都不影响其余。
#
# **为什么不再用 app.run_polling()。** PTB 的 Application.__run 把顺序写死成
# _bootstrap_initialize → post_init → updater.start_polling → app.start
# （telegram/ext/_application.py:1052-1062）。而 _bootstrap_initialize 就是
# network_retry_loop(action_cb=self.initialize, max_retries=bootstrap_retries)，
# Bot.initialize() 内含一次真实的 get_me() 网络请求，bootstrap_retries 默认 0
# ——按 PTB 自己的文档，0 就是"不重试"。
#
# Web 服务偏偏是挂在 post_init（setup_bot_commands）里启动的。于是 Telegram
# 不通时：get_me 抛 NetworkError → __run 的 except 只接 KeyboardInterrupt /
# SystemExit → 异常穿出 run_polling → main 打一行 Fatal Error 后 sys.exit(1)
# → **Web 的监听端口从来没有 bind 过** → nginx 502 → PM2 无限重启，每次都死
# 在同一处。CLI 是独立进程、从不碰 Telegram，所以只有它还能用。这就是
# "bot 连不上、网页也打不开、只有 CLI 能用"的完整因果链。
#
# 改成本模块持有循环之后：Web / CLI 回放 / trigger 先起来，Telegram 降级成一个
# 无限重试的受监督组件——它连不上只是它自己连不上。顺带把 BOT_TOKEN 分叉出的
# 两条启动路径（run_web_only_main vs PTB 模式）合成一条，部署 web+bot+cli /
# web+cli / 只有 cli 等任意组合都走同一段代码。

# 组件状态机：disabled（没配置/关掉）→ starting → up；运行中出问题转
# degraded（能力受限但还在重试），彻底起不来是 down。
COMPONENT_DISABLED = "disabled"
COMPONENT_STARTING = "starting"
COMPONENT_UP = "up"
COMPONENT_DEGRADED = "degraded"
COMPONENT_DOWN = "down"


class ComponentState:
    """单个组件的当前状态。供 /api/health 与 /status 读取，不含任何密钥。"""

    __slots__ = ("name", "state", "since", "last_error", "detail")

    def __init__(self, name: str):
        self.name = name
        self.state = COMPONENT_DISABLED
        self.since = time.time()
        self.last_error: Optional[str] = None
        self.detail: Dict[str, Any] = {}

    def set(self, state: str, *, error: Any = None, **detail: Any) -> None:
        if state != self.state:
            self.state = state
            self.since = time.time()
        if state == COMPONENT_UP:
            # 恢复即清错：/api/health 上留着一条几小时前的旧错误，只会让人
            # 误判当前还在坏着。
            self.last_error = None
        if error is not None:
            # 组件错误会被 /api/health 外发，也会进日志：异常串里可能带
            # provider URL 里的 key、文件路径，一律先脱敏（同 lifecycle.py
            # 的 global_error_handler）。
            self.last_error = redact_sensitive_text(str(error))[:200]
        self.detail.update(detail)

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "state": self.state,
            "since": round(self.since, 3),
            "uptime": round(max(0.0, time.time() - self.since), 1),
        }
        if self.last_error:
            data["last_error"] = self.last_error
        if self.detail:
            data.update(self.detail)
        return data


_COMPONENT_STATES: "OrderedDict[str, ComponentState]" = OrderedDict()


def component_state(name: str) -> ComponentState:
    state = _COMPONENT_STATES.get(name)
    if state is None:
        state = ComponentState(name)
        _COMPONENT_STATES[name] = state
    return state


def component_health() -> Dict[str, Dict[str, Any]]:
    """所有组件的状态快照。任一端挂了都能在这里看出来是哪一端。"""
    return {name: state.as_dict() for name, state in _COMPONENT_STATES.items()}


def any_component_degraded() -> bool:
    return any(
        state.state in (COMPONENT_DEGRADED, COMPONENT_DOWN)
        for state in _COMPONENT_STATES.values()
    )


# 进程级停机信号。信号处理器、致命错误、组件内部的 sys.exit 需求全部收敛到
# 这一个 Event 上——只有一条停机路径，就只有一份关闭清理代码。
_app_stop_event: Optional[asyncio.Event] = None
# 停机时进程该用的退出码。InvalidToken 走 78：PM2 的 --stop-exit-codes 78
# 认这个码后不再无限重启（install.sh:1896），换成别的码会变成刷屏重启。
_app_exit_code = 0


def get_app_stop_event() -> asyncio.Event:
    global _app_stop_event
    if _app_stop_event is None:
        _app_stop_event = asyncio.Event()
    return _app_stop_event


def request_app_stop(exit_code: int = 0) -> None:
    """请求整个进程有序停机。可从任意协程调用，重复调用只保留第一个非零退出码。"""
    global _app_exit_code
    if exit_code and not _app_exit_code:
        _app_exit_code = int(exit_code)
    get_app_stop_event().set()


async def _sleep_or_stop(delay: float) -> bool:
    """睡 delay 秒，中途收到停机信号就立刻返回 True。"""
    stop_event = get_app_stop_event()
    if delay <= 0:
        return stop_event.is_set()
    try:
        await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=delay)
    except asyncio.TimeoutError:
        return False
    return True


async def retry_forever(action: Callable[[], Awaitable[Any]], *, description: str,
                        interval: float = 1.0, cap: float = 30.0,
                        state: Optional[ComponentState] = None) -> bool:
    """无限重试一个网络动作，直到成功、或收到停机信号。成功返回 True。

    刻意不复用 telegram.ext._utils.networkloop.network_retry_loop：那是私有
    模块（升级 PTB 就可能改签名），而这里真正需要的语义只有三条——指数退避、
    可被停机信号立刻打断、InvalidToken 直接上抛（让 main 走 exit 78）。

    日志刻意节流：TG 断连可能持续几小时，每秒一行会把 pm2 日志刷爆，也会掩盖
    真正的错误。前 3 次每次都打，之后每 10 次打一次。
    """
    attempt = 0
    while not get_app_stop_event().is_set():
        attempt += 1
        try:
            await action()
            if attempt > 1:
                logger.info("✅ %s 第 %d 次尝试成功", description, attempt)
            return True
        except (asyncio.CancelledError, InvalidToken):
            raise
        except Exception as exc:  # noqa: BLE001 —— 网络类异常五花八门，一律退避重试
            delay = min(cap, interval * (1.5 ** min(attempt - 1, 12)))
            if state is not None:
                state.set(COMPONENT_DEGRADED, error=exc, attempts=attempt)
            if attempt <= 3 or attempt % 10 == 0:
                logger.warning(
                    "%s 失败（第 %d 次，%.0fs 后重试）: %s",
                    description, attempt, delay, redact_sensitive_text(str(exc))[:200],
                )
            if await _sleep_or_stop(delay):
                return False
    return False


def build_application() -> Any:
    """建 PTB Application 并注册所有 handler。不设 post_init / post_shutdown。

    刻意**不**用 .post_init()/.post_shutdown：这两个钩子只有 run_polling /
    run_webhook 的 __run 会调用，而本进程自己管生命周期。留着它们会变成
    "看起来在工作、实际永不执行"的死代码——启动菜单不发、数据库不关，而且
    没有任何报错。要跑的东西一律在 telegram_supervisor / shutdown_components
    里显式 await。
    """
    builder = (
        Application.builder()
        .token(BotConfig.TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(15)
        .pool_timeout(10)
        .concurrent_updates(True)
    )
    if BotConfig.API_BASE_URL:
        # 走本地 Telegram Bot API server：base_url / base_file_url 同源，开 local_mode
        # 注意：PTB v20 会自动在 base_url 后追加 token，这里只给前缀，不要带 token
        builder = builder.base_url(f"{BotConfig.API_BASE_URL}/bot")
        builder = builder.base_file_url(f"{BotConfig.API_BASE_URL}/file/bot")
        builder = builder.local_mode(True)
    app = builder.build()

    job_queue = app.job_queue
    if job_queue is None:
        raise RuntimeError(
            "JobQueue 不可用：请确认安装了 python-telegram-bot[job-queue]"
        )
    # job_queue.start() 由 app.start() 触发（需要 bot.id，即需要 get_me 成功），
    # 所以这条定时任务天然只在 Telegram 就绪后才真正开始跑。
    job_queue.run_repeating(check_and_send_idle_message, interval=3600, first=60)

    # 注册命令。CommandHandler 必须放在兜底 MessageHandler 前面，否则命令会被普通消息处理器吃掉。
    # mirror_to_web 让 TG 端命令/按钮的输出（菜单切换、按钮变化）同步到 web 端。
    app.add_handler(CommandHandler("start", mirror_to_web(cmd_start)))
    for cmd, handler in (
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
        ("params", cmd_timeout_menu),
        ("thinking", cmd_thinking_menu),
        ("web", cmd_web_menu),
        ("agent", cmd_toggle_agent),
        ("blacklist", cmd_blacklist_menu),
        ("stream", cmd_toggle_stream),
        ("skills", cmd_skills_menu),
        ("status", cmd_show_info),
        ("export", cmd_export_all),
        ("stats", cmd_token_stats),
        ("show_chat_info", cmd_show_info),
    ):
        app.add_handler(CommandHandler(cmd, mirror_to_web(handler)))

    app.add_handler(CallbackQueryHandler(mirror_to_web(handle_button_click)))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/(?:黑名单|blacklist)(?:@\w+)?(?:\s|$)"),
        mirror_to_web(cmd_blacklist_menu)
    ))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker_message))
    # 文本处理器（含转发消息——转发消息和普通消息完全一样处理）
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND, handle_text_message))
    # 其他类型消息（排除转发、文本、文件/图片/贴纸）
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & ~filters.TEXT & ~filters.FORWARDED
        & ~filters.Document.ALL & ~filters.PHOTO & ~filters.Sticker.ALL,
        handle_other_message
    ))
    app.add_error_handler(global_error_handler)
    return app


async def _teardown_telegram(app: Any) -> None:
    """把 PTB 拆回未启动状态，供监督循环重试。每一步都独立容错。"""
    for label, action in (
        ("updater.stop", getattr(getattr(app, "updater", None), "stop", None)),
        ("app.stop", getattr(app, "stop", None)),
        ("app.shutdown", getattr(app, "shutdown", None)),
    ):
        if action is None:
            continue
        try:
            await action()
        except Exception as exc:  # noqa: BLE001 —— 未启动就 stop 会抛，属预期
            logger.debug("Telegram %s 清理时报错（可忽略）: %s", label, exc)


async def telegram_supervisor(app: Any) -> None:
    """把 Telegram 变成"可以起不来、也可以起来后再断"的受监督组件。

    与 run_polling 的关键差别：这里的失败只写进 component_state，不抛给进程。
    Web / CLI 回放 / trigger 已经在跑了，Telegram 连不上就一直退避重试，等网络
    恢复自己接上——不再是"连不上就整个进程退出"。

    InvalidToken 例外：那不是网络问题，重试一万次也不会好，直接请求停机并带上
    退出码 78（PM2 的 --stop-exit-codes 78 认这个码后不再重启）。
    """
    state = component_state("telegram")
    stop_event = get_app_stop_event()
    restarts = 0
    while not stop_event.is_set():
        state.set(COMPONENT_STARTING, restarts=restarts)
        try:
            # 第一处网络请求：Bot.initialize() 里的 get_me()。原先它失败就
            # 直接带走整个进程，现在只让 telegram 这一个组件停在 degraded。
            if not await retry_forever(
                app.initialize, description="Telegram 初始化（get_me）", state=state,
            ):
                return
            # 第二处：Updater._bootstrap 里的 delete_webhook。bootstrap_retries=-1
            # 才是"无限重试"；漏了这里就等于留下第二个一次失败就放弃的点。
            await app.updater.start_polling(bootstrap_retries=-1)
            # app.start() 会用 bot.id 拼任务名（需要 get_me 已成功），并在内部
            # 启动 job_queue。
            await app.start()
        except InvalidToken:
            logger.critical("Telegram Bot Token 无效或已失效，请检查 .env 中的 BOT_TOKEN。")
            logger.critical("请到 BotFather 重新生成 Token，更新 .env 后再启动。")
            state.set(COMPONENT_DOWN, error="Bot Token 无效")
            await _teardown_telegram(app)
            request_app_stop(78)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.set(COMPONENT_DEGRADED, error=exc)
            logger.warning("Telegram 通道启动失败，15s 后重试: %s",
                           redact_sensitive_text(str(exc))[:200])
            await _teardown_telegram(app)
            if await _sleep_or_stop(15.0):
                return
            restarts += 1
            continue

        # --- 起来了 ---
        set_telegram_channel(app.bot)
        # trigger 调度器早就在跑了（组件启动时以 application=None 起的）；这里
        # 只把真实 bot 的引用补进去，startup 对重复调用是幂等的，不会重跑恢复。
        with contextlib.suppress(Exception):
            await SelfTriggerManager.startup(app)
        state.set(COMPONENT_UP, restarts=restarts)
        logger.info("✅ Telegram 通道已就绪（长轮询运行中）")
        # 命令菜单同步 + 启动主菜单：这些都是真实网络调用，必须等通道就绪，
        # 而且失败不能影响别的组件（函数内部已各自 try 住）。
        with contextlib.suppress(Exception):
            await telegram_ready(app)

        # 看着它。PTB 自己会重试 get_updates 的网络错误，但 polling 任务真的
        # 死掉（非网络异常）时没人会告诉我们——那样 bot 会静默失联，日志上
        # 看不出任何异常。这里每 30s 探一次，死了就整段重启 Telegram 组件。
        while not stop_event.is_set():
            if await _sleep_or_stop(30.0):
                return
            if not getattr(app.updater, "running", False):
                state.set(COMPONENT_DEGRADED, error="长轮询已停止，正在重启 Telegram 通道")
                logger.warning("⚠️ Telegram 长轮询已停止，重启 Telegram 组件")
                break
        else:
            return
        await _teardown_telegram(app)
        restarts += 1


async def _start_component(name: str, starter: Callable[[], Awaitable[Any]], *,
                           label: str) -> bool:
    """起一个组件；失败只标记自己的状态并记日志，绝不往上抛。

    这是"不许互相干扰"的落点：谁起不来就只有谁是 down，进程照常往下走。
    """
    state = component_state(name)
    state.set(COMPONENT_STARTING)
    try:
        await starter()
    except Exception as exc:  # noqa: BLE001 —— 组件失败不能带走别的组件
        state.set(COMPONENT_DOWN, error=exc)
        logger.error("%s 启动失败（其余组件不受影响）: %s",
                     label, redact_sensitive_text(str(exc))[:200])
        return False
    return True


async def _start_web_component(app: Any) -> None:
    # 纯 Web 部署（没有 BOT_TOKEN）时 Web 是唯一入口，强制启动、不受
    # web_enabled 开关影响——否则用户会把自己锁在外面。
    force = bool(BotConfig.WEB_ONLY)
    state = component_state("web")
    ok = await _start_component(
        "web", lambda: start_web_chat_if_enabled(app, force=force), label="Web 服务",
    )
    if not ok:
        return
    if is_web_chat_running():
        server = _web_chat_server
        state.set(COMPONENT_UP,
                  host=getattr(server.config, "host", None),
                  port=getattr(server.config, "port", None))
    else:
        # 开关关着、或没设密码：这不是故障，是没开。
        state.set(COMPONENT_DISABLED, reason="未开启或未设置访问密码")


async def _start_cli_relay_component() -> None:
    """CLI→(TG+网页) 的跨进程回放器。

    刻意与 Web 开关解耦（见 start_external_sync_watcher 的说明），也与 Telegram
    解耦：纯 Web 模式下它把 CLI 的操作流变成网页帧，Telegram 关着时同样有用。
    start_web_chat_if_enabled 内部已经起过一次，这里是兜底——Web 组件抛异常时
    回放器仍然要在。
    """
    state = component_state("cli_relay")
    if not _cli_relay_enabled():
        state.set(COMPONENT_DISABLED, reason="XGENT_CLI_NO_TG_MIRROR=1")
        return
    ok = await _start_component(
        "cli_relay", start_external_sync_watcher, label="CLI 跨端回放器",
    )
    if ok:
        state.set(COMPONENT_UP)


async def _start_triggers_component() -> None:
    """trigger 调度器。以 application=None 起——调度走自持 AsyncIOScheduler，
    不依赖 PTB；Telegram 就绪后 telegram_supervisor 会把真实 bot 补进来
    （startup 对重复调用幂等，不会重跑恢复流程）。

    这一步原先在 setup_bot_commands 里，也就是 PTB 的 post_init 里——TG 连不上
    时它跟着一起没跑，用户的定时任务静默全停，日志上一个字都看不到。
    """
    ok = await _start_component(
        "triggers", lambda: SelfTriggerManager.startup(None), label="trigger 调度器",
    )
    if ok:
        component_state("triggers").set(COMPONENT_UP)


def _install_signal_handlers() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows 的 ProactorEventLoop 不支持 add_signal_handler；退化为
        # KeyboardInterrupt（Ctrl+C 仍能走到 run_app 的 except 分支）。
        with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, request_app_stop)


def _log_ready_banner() -> None:
    logger.info("=" * 50)
    logger.info("XGent ready.")
    for name, snapshot in component_health().items():
        logger.info("  · %-10s %s", name, snapshot.get("state"))
    if is_web_chat_running() and _web_chat_server is not None:
        logger.info("  网页地址：http://%s:%s",
                    _web_chat_server.config.host, _web_chat_server.config.port)
    logger.info("=" * 50)


async def shutdown_components(app: Any = None) -> None:
    """唯一的停机路径。曾经有两份（on_shutdown / on_shutdown_web_only），
    差别只有一个 TelegramRichAPI 客户端要不要关——那个关闭本身是幂等的，
    于是两份代码里必然有一份会在改动时被忘掉。现在只有这一条。"""
    for state in _COMPONENT_STATES.values():
        if state.state in (COMPONENT_UP, COMPONENT_STARTING, COMPONENT_DEGRADED):
            state.set(COMPONENT_DOWN, reason="进程停机")
    if app is not None:
        await _teardown_telegram(app)
    await on_shutdown(app)


async def run_app() -> int:
    """唯一进程入口，返回给 sys.exit 的退出码。

    组件顺序是有讲究的：先 boot_core（数据库/配置，谁都要用），再起所有不碰
    Telegram 的组件，最后才把 Telegram 作为受监督任务放出去。这样"TG 连不上"
    这件事发生时，网页和 CLI 同步早就在服务了。
    """
    stop_event = get_app_stop_event()

    # 1) 公共地基：配置缓存 + 数据库。原先埋在 PTB 的 post_init 里，TG 连不上
    #    时连它都不会执行。
    await boot_core()

    # 2) 先把 Application 建出来（纯本地操作，不发任何网络请求），这样 Web 组件
    #    能拿到 application 引用（网页里设密码/端口要用 context.application）。
    app = None
    tg_state = component_state("telegram")
    if BotConfig.TOKEN:
        try:
            app = build_application()
        except Exception as exc:  # noqa: BLE001
            tg_state.set(COMPONENT_DOWN, error=exc)
            logger.critical("Telegram Application 构建失败，Telegram 通道不可用: %s",
                            redact_sensitive_text(str(exc))[:200])
    else:
        tg_state.set(COMPONENT_DISABLED, reason="未配置 BOT_TOKEN")
        logger.info("ℹ️ 未配置 BOT_TOKEN，Telegram 通道关闭（Web / CLI 照常）。")

    # 3) 非 Telegram 组件。
    await _start_web_component(app)
    await _start_cli_relay_component()
    await _start_triggers_component()

    # 4) 一个入口都没有时别硬撑：PM2 会把这种进程显示成 online，用户以为在跑。
    if app is None and not is_web_chat_running():
        logger.critical(
            "❌ 启动失败：没有 BOT_TOKEN，Web 服务也没起来"
            "（请先在 /start → 🌐 Web 里设置访问密码，或配置 BOT_TOKEN）。"
        )
        await shutdown_components(None)
        return 1

    # 5) Telegram 交给监督任务。
    tg_task = None
    if app is not None:
        tg_task = asyncio.create_task(telegram_supervisor(app), name="xgent-telegram")

    _install_signal_handlers()
    _log_ready_banner()

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if tg_task is not None:
            tg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tg_task
        await shutdown_components(app)
    return _app_exit_code
