# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

if __name__ == '__main__':
    try:
        print("=" * 60)
        print("XGent starting...")
        if BotConfig.API_BASE_URL:
            print(f"Using LOCAL Telegram Bot API: {BotConfig.API_BASE_URL}")
        print("=" * 60)
        # 启动即报版本：pm2 logs 里一眼对出进程加载的是哪个提交
        log_runtime_code_version()

        # 唯一入口。Telegram / Web / CLI 回放 / trigger 各自是一个独立组件，
        # 谁起不来都只有谁起不来——不再有"TG 连不上就整个进程退出、连网页端口
        # 都没 bind 过"这条路径（完整因果链见 runtime.py 顶部注释）。
        # 退出码由 run_app 返回：Token 无效是 78，PM2 的 --stop-exit-codes 78
        # 认这个码后不再无限重启。
        sys.exit(asyncio.run(run_app()))

    except KeyboardInterrupt:
        sys.exit(0)

    except Exception as e:
        safe_error = redact_sensitive_text(str(e))
        logger.critical(f"Fatal Error: {safe_error}")
        print(redact_sensitive_text(traceback.format_exc()), file=sys.stderr)
        sys.exit(1)
