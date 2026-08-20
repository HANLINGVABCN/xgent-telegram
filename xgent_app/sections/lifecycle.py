# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

# 命令名 -> 一句话说明。Telegram 侧用它同步 /命令 菜单，CLI（xgent_cli.py 的
# `/` 提示与 /help）直接读同一张表——命令的说明文字只能有一处来源，抄第二份
# 的结果一定是加了新命令后两边不一致，而用户看到的是哪一份完全取决于他用的
# 是哪个客户端。
TELEGRAM_COMMAND_DESCRIPTIONS = (
    ("start", "打开主菜单"),
    ("config", "打开设置面板"),
    ("update", "更新代码并重启"),
    ("providers", "管理提供商与模型列表"),
    ("provider_config", "导入导出提供商配置"),
    ("models", "选择默认模型"),
    ("chat_model", "选择默认对话模型"),
    ("media_model", "选择默认媒体模型"),
    ("prompts", "管理提示词"),
    ("clear_memory", "清空上下文"),
    ("depth", "设置记忆深度"),
    ("params", "参数设置"),
    ("thinking", "设置思考深度"),
    ("web", "配置网页版聊天"),
    ("agent", "开关 Agent 模式"),
    ("blacklist", "管理 Agent 命令黑名单"),
    ("stream", "开关流式输出"),
    ("status", "查看状态"),
    ("export", "导出全部记忆"),
    ("stats", "Token统计报表"),
    ("restart", "重启 Bot"),
    ("show_chat_info", "查看状态与记忆统计"),
)

# 只在某个客户端存在的命令说明。skills 没注册进 Telegram 命令菜单
# （main.py 没给它建 CommandHandler，只有 _WEB_COMMAND_MAP 里有），
# 但 Web/CLI 都能敲，说明文字同样需要一处来源。
EXTRA_COMMAND_DESCRIPTIONS = (
    ("skills", "管理技能库"),
)


def command_description(name: str) -> str:
    """命令的一句话说明；没有登记过就返回空串。"""
    for command_name, description in TELEGRAM_COMMAND_DESCRIPTIONS + EXTRA_COMMAND_DESCRIPTIONS:
        if command_name == name:
            return description
    return ""


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("全局错误处理捕获异常", exc_info=context.error)

    # 尝试记录错误。必须脱敏：SYSTEM_OP 记录会被重新注入模型上下文
    # （见 get_conversation_messages），异常串里的密钥会被持续外发给
    # 第三方 provider。
    try:
        await GlobalRecorder.record_system_op(
            redact_sensitive_text(f"错误: {str(context.error)[:200]}"),
            {"traceback": redact_sensitive_text(traceback.format_exc()[:500])}
        )
    except Exception as record_err:
        logger.warning(f"记录错误信息失败: {record_err}")

    # 尝试通知用户。异常原文可能带文件路径、provider 名、URL 里的密钥，
    # 这里只给固定文案，详情去日志看。
    try:
        if update and hasattr(update, 'effective_chat') and update.effective_chat:
            await safe_send_message(
                context,
                update.effective_chat.id,
                "系统处理时出现异常，请稍后重试。详细错误已写入日志。",
            )
    except Exception:
        pass

# --- ☆ 应用关闭处理 ☆ ---
async def setup_bot_commands(app):
    """同步 Telegram 命令菜单，并在启动后发送完整主菜单。"""
    global _startup_commands_synced, _startup_menu_sent

    await UserDataManager.init()
    await BotMemoryDB.get_instance()
    await SelfTriggerManager.startup(app)
    # Web 服务按开关启动；失败只记日志并通知用户，不影响 bot 主流程。
    await start_web_chat_if_enabled(app)

    if not _startup_commands_synced:
        try:
            commands = [
                BotCommand(name, description)
                for name, description in TELEGRAM_COMMAND_DESCRIPTIONS
            ]
            private_scope = BotCommandScopeAllPrivateChats()
            await app.bot.delete_my_commands(scope=private_scope)
            await app.bot.set_my_commands(commands, scope=private_scope)
            with contextlib.suppress(Exception):
                await app.bot.delete_my_commands(scope=private_scope, language_code="zh")
                await app.bot.set_my_commands(commands, scope=private_scope, language_code="zh")
            _startup_commands_synced = True
            logger.info("✅ Telegram 命令菜单已同步")
        except Exception as e:
            logger.warning(f"同步 Telegram 命令菜单失败: {e}")

    # 跨进程时间窗口去重：即使 PM2 重启拉起新进程（flag 会重置），
    # 只要距上次发送不到 5 分钟，就不再发——彻底防止"重启后短时间内收到多条 start"。
    STARTUP_MENU_COOLDOWN = 300  # 5 分钟

    # 加锁：防止 post_init 与并发任务同时进入
    async with _startup_menu_lock:
        # 进程内去重：拿到锁后可能已被另一个协程发过
        if _startup_menu_sent:
            return
        try:
            await UserDataManager.init()
            db = await BotMemoryDB.get_instance()

            # === 重启/更新校验：判断本次启动是否真的换了新进程 ===
            # restart_current_process 退出前会写入 restart_expected_pid（旧进程 PID）+ 时间戳。
            # 新进程启动时对比当前 PID：不同=重启成功（新代码已加载），相同=没换进程（重启未生效）。
            notify_chat_id = await db.get_config('restart_notify_chat_id', BotConfig.AUTHORIZED_USER_ID) or BotConfig.AUTHORIZED_USER_ID
            expected_pid = await db.get_config('restart_expected_pid', None)
            expected_ts = await db.get_config('restart_expected_ts', 0)
            restart_notice_sent = False
            try:
                expected_ts_f = float(expected_ts or 0)
            except (TypeError, ValueError):
                expected_ts_f = 0.0
            # 标记存活窗口：5 分钟内的标记才认为是“刚刚请求的重启”，更早的视为陈旧残留
            if expected_pid is not None and (time.time() - expected_ts_f) < 300:
                current_pid = os.getpid()
                try:
                    expected_pid_int = int(expected_pid)
                except (TypeError, ValueError):
                    expected_pid_int = -1
                if current_pid != expected_pid_int:
                    # PID 变了 → 新进程被拉起，代码确实重新从磁盘加载
                    logger.info(f"✅ 重启校验通过：新进程 PID={current_pid}（旧 PID={expected_pid_int}），新代码已加载")
                    with contextlib.suppress(Exception):
                        await app.bot.send_message(
                            chat_id=notify_chat_id,
                            text=(
                                f"✅ 已成功重启，新代码已加载。\n"
                                f"新进程 PID: <code>{current_pid}</code>（原 {expected_pid_int}）"
                            ),
                            parse_mode=constants.ParseMode.HTML
                        )
                        restart_notice_sent = True
                else:
                    # PID 没变 → sys.exit 没生效或重启脚本没拉起，仍是旧进程/旧代码
                    logger.warning(f"⚠️ 重启校验失败：当前 PID={current_pid} 与重启前相同，进程未真正重启，可能是旧代码")
                    with contextlib.suppress(Exception):
                        await app.bot.send_message(
                            chat_id=notify_chat_id,
                            text=(
                                "⚠️ 重启可能未生效：当前仍是重启前的同一个进程（PID 未变）。\n"
                                "代码可能没有更新，请到服务器手动运行 install.sh restart 确认。"
                            ),
                            parse_mode=constants.ParseMode.HTML
                        )
                        restart_notice_sent = True
                # 消费标记，避免下次普通启动重复发校验通知
                await db.set_config('restart_expected_pid', None)
                await db.set_config('restart_expected_ts', None)

            # 跨进程去重：检查上次发送时间戳
            last_sent_ts = await db.get_config('last_startup_menu_sent_ts', 0)
            elapsed = time.time() - float(last_sent_ts or 0)
            if elapsed < STARTUP_MENU_COOLDOWN:
                logger.info(f"⏭️ 启动菜单跳过：距上次发送仅 {int(elapsed)}s（冷却 {STARTUP_MENU_COOLDOWN}s 内）")
                _startup_menu_sent = True
                return
            # 标记置位（不再回滚）：宁可启动菜单漏发，也绝不能重复发送。
            # 漏发时用户随时可手动 /start；重复发送才是真正困扰用户的问题。
            _startup_menu_sent = True
            await app.bot.send_message(
                chat_id=notify_chat_id or BotConfig.AUTHORIZED_USER_ID,
                text=build_start_menu_text(),
                reply_markup=get_main_menu(),
                parse_mode=constants.ParseMode.HTML
            )
            # 记录发送时间戳到数据库（跨进程有效）
            await db.set_config('last_startup_menu_sent_ts', time.time())
            await GlobalRecorder.record_system_op("启动后发送完整主菜单", {"chat_id": notify_chat_id, "restart_notice_sent": restart_notice_sent})
            logger.info("✅ 启动主菜单已发送给用户")
        except Exception as e:
            # 发送失败也不回滚 flag：避免并发/重连再次触发导致重复发送
            logger.warning(f"启动主菜单发送失败（不再重试，用户可手动 /start）: {e}")

async def on_shutdown_web_only():
    """纯 Web 模式（无 BOT_TOKEN、无 PTB Application）下的关闭清理。

    与 on_shutdown(app) 做同样的资源清理，但跳过所有 app.bot.* 调用——纯
    Web 模式下没有 PTB Application，也没有 SelfTriggerManager（其调度器
    挂在 application.job_queue.scheduler 上，见 run_web_only_main 的说明）。
    """
    logger.info("🛑 服务正在关闭...")
    try:
        await asyncio.to_thread(flush_model_trace)
    except Exception as e:
        logger.debug(f"刷新 trace 队列失败: {e}")
    try:
        await stop_web_chat()
    except Exception as e:
        logger.error(f"关闭 Web Chat 失败: {e}")
    try:
        AgentShellSessionManager.kill_all()
        logger.info("✅ Agent shell 会话已关闭")
    except Exception as e:
        logger.error(f"关闭 Agent shell 会话失败: {e}")
    try:
        db = await BotMemoryDB.get_instance()
        await db.close()
        logger.info("✅ 数据库连接已关闭")
    except Exception as e:
        logger.error(f"关闭数据库失败: {e}")
    try:
        await PortalManager.close_all()
        logger.info("✅ OpenAI SDK 客户端池已关闭")
    except Exception as e:
        logger.error(f"关闭 OpenAI SDK 客户端池失败: {e}")
    try:
        await ModelClient.close_http_client()
        logger.info("✅ 模型 HTTP 连接池已关闭")
    except Exception as e:
        logger.error(f"关闭模型 HTTP 连接池失败: {e}")


async def run_web_only_main():
    """纯 Web 模式主循环：未配置 BOT_TOKEN 时的入口，不建 PTB Application。

    对照 setup_bot_commands + app.run_polling 的职责，纯 Web 模式下：
    - 不跑 run_polling（没有 Telegram 连接）
    - 不注册 Telegram 命令菜单 / 不发启动菜单消息（没有 chat 可发）
    - 跳过 SelfTriggerManager（后台定时/长驻触发任务）：其调度器依赖 PTB
      的 application.job_queue.scheduler（APScheduler），纯 Web 模式没有
      这个对象。这是 v1 的已知限制，已在 README 中说明；后续如需支持，
      可以单独起一个 AsyncIOScheduler 并给 SelfTriggerManager 一个 shim。
    - Web 服务是唯一入口，强制启动（force=True，不受 web_enabled 开关影响）
    """
    await UserDataManager.init()
    await BotMemoryDB.get_instance()

    await start_web_chat_if_enabled(None, force=True)
    if not is_web_chat_running():
        logger.critical("❌ 纯 Web 模式启动失败：请确认已设置 Web 访问密码后重试。")
        # sys.exit() 只抛 SystemExit，不会关掉 aiosqlite 内部起的非 daemon
        # 工作线程——不先清理就退出，进程会卡成僵尸（日志打完却真的退不
        # 出去，PM2 也看不出异常，仍显示"online"）。必须先走一遍关闭清理。
        await on_shutdown_web_only()
        sys.exit(1)

    port = normalize_web_port(UserDataManager.get('web_port', DEFAULT_WEB_PORT))
    logger.info("=" * 50)
    logger.info("XGent Web (standalone) ready.")
    logger.info(f"监听地址：http://{DEFAULT_WEB_HOST}:{port}")
    logger.info("=" * 50)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            # Windows 的 ProactorEventLoop 不支持 add_signal_handler；
            # 退化为 KeyboardInterrupt（Ctrl+C 仍能正常触发下面的 except 分支）。
            loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await on_shutdown_web_only()


async def on_shutdown(app):
    """应用关闭时清理资源"""
    logger.info("🛑 服务正在关闭...")
    try:
        # trace 现在是后台线程异步落盘，退出前把队列里剩下的写完。
        await asyncio.to_thread(flush_model_trace)
    except Exception as e:
        logger.debug(f"刷新 trace 队列失败: {e}")
    try:
        await SelfTriggerManager.shutdown()
        logger.info("✅ 后台触发任务已关闭")
    except Exception as e:
        logger.error(f"关闭后台触发任务失败: {e}")
    try:
        await stop_web_chat()
    except Exception as e:
        logger.error(f"关闭 Web Chat 失败: {e}")
    try:
        AgentShellSessionManager.kill_all()
        logger.info("✅ Agent shell 会话已关闭")
    except Exception as e:
        logger.error(f"关闭 Agent shell 会话失败: {e}")
    try:
        db = await BotMemoryDB.get_instance()
        await db.close()
        logger.info("✅ 数据库连接已关闭")
    except Exception as e:
        logger.error(f"关闭数据库失败: {e}")
    try:
        await PortalManager.close_all()
        logger.info("✅ OpenAI SDK 客户端池已关闭")
    except Exception as e:
        logger.error(f"关闭 OpenAI SDK 客户端池失败: {e}")
    try:
        await ModelClient.close_http_client()
        logger.info("✅ 模型 HTTP 连接池已关闭")
    except Exception as e:
        logger.error(f"关闭模型 HTTP 连接池失败: {e}")
    try:
        if TelegramRichAPI._client and not TelegramRichAPI._client.is_closed:
            await TelegramRichAPI._client.aclose()
            logger.info("✅ Rich API httpx 客户端已关闭")
    except Exception as e:
        logger.error(f"关闭 Rich API httpx 客户端失败: {e}")

# --- ☆ 主程序入口 ☆ ---
