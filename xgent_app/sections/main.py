# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

import os as _os
import sys as _sys
import time as _time
import traceback as _traceback

_CRASH_LOG = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'xgent_crash.log')


def _write_crash(reason: str, exc_type=None, exc_value=None, exc_tb=None) -> None:
    """把进程崩溃原因写进 xgent_crash.log，下次「无声无息死掉」就有证据。"""
    try:
        ts = _time.strftime('%Y-%m-%d %H:%M:%S')
        pid = _os.getpid()
        line = f"\n{'='*60}\n[{ts}] pid={pid} {reason}\n"
        if exc_type is not None and exc_tb is not None:
            line += ''.join(_traceback.format_exception(exc_type, exc_value, exc_tb))
        else:
            line += f"（无异常栈，{reason}）\n"
        with open(_CRASH_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


_orig_excepthook = _sys.excepthook


def _crash_excepthook(exc_type, exc_value, exc_tb):
    # KeyboardInterrupt 是正常 Ctrl+C 退出，不算崩溃
    if not issubclass(exc_type, KeyboardInterrupt):
        _write_crash(f"未捕获异常: {exc_type.__name__}", exc_type, exc_value, exc_tb)
    return _orig_excepthook(exc_type, exc_value, exc_tb)


_sys.excepthook = _crash_excepthook


def _crash_asyncio_handler(loop, context):
    # 游离 task 抛的未捕获异常（PTB 的 add_error_handler 接不到这类）
    exc = context.get('exception')
    msg = context.get('message', '')
    reason = f"asyncio 游离任务异常: {msg}"
    if exc is not None:
        reason += f"\n  异常类型: {type(exc).__name__}: {exc}"
    _write_crash(reason)


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
        _loop = asyncio.run(run_app())
        # run_app 返回后注册 asyncio 异常处理器已晚，得在 run_app 内部；
        # 但游离 task 异常大多发生在 run_app 运行期间，下面那行兜住退出后残留。
    except KeyboardInterrupt:
        _sys.exit(0)

    except SystemExit:
        # 显式 sys.exit（如 InvalidToken 的 exit 78），正常停机，不算崩溃
        raise

    except Exception as e:
        safe_error = redact_sensitive_text(str(e))
        logger.critical(f"Fatal Error: {safe_error}")
        print(redact_sensitive_text(_traceback.format_exc()), file=_sys.stderr)
        _write_crash(f"main.py except Exception: {type(e).__name__}: {safe_error}",
                     type(e), e, e.__traceback__)
        _sys.exit(1)
