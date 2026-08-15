# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

# pyright: reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
# xgent_server.py
# XGent for Telegram - 私有 Telegram AI 助手服务端。
#
# 核心职责：
# - 单用户访问控制与未授权用户处理。
# - 异步 SQLite 记忆、提供商/模型配置、提示词管理。
# - 支持流式/非流式聊天，并处理文件、图片、贴纸与媒体上下文。
# - Agent 模式支持 shell 命令、可管理 Shell 会话、文件读写、
#   自定义命令黑名单、停止控制与媒体生成。
# - 外部媒体模块负责生成、保存、发送、记录，并把结果回灌给对话，
#   同时避免重复拼接路径提示。

import logging
from logging.handlers import RotatingFileHandler
import sys
import os
import time
import zipfile
import io
import html
import math
import base64
import mimetypes
import asyncio
import traceback
import random
import uuid
import subprocess
import re
import queue
import threading
import contextlib
import copy
import shutil
import tempfile
import fnmatch
import urllib.error
import urllib.parse
import urllib.request
import aiosqlite
import json
import httpx
import hashlib
import codecs
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional, Tuple, List, Dict, Any, Deque, cast
from collections import OrderedDict, deque

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update, constants, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, WebAppInfo
from telegram.error import BadRequest, InvalidToken, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

load_dotenv()

# --- ☆ 全局控制状态 ☆ ---
_stop_generation_event: Optional[asyncio.Event] = None   # 停止生成事件
_is_processing = False                                    # 处理中锁
_conversation_processing_lock = asyncio.Lock()
_startup_commands_synced = False
_startup_menu_sent = False
_startup_menu_lock = asyncio.Lock()                        # 启动菜单发送锁，防止并发双发


def get_or_create_stop_event() -> asyncio.Event:
    global _stop_generation_event
    if _stop_generation_event is None:
        _stop_generation_event = asyncio.Event()
    return _stop_generation_event


def is_stop_requested() -> bool:
    return bool(_stop_generation_event and _stop_generation_event.is_set())


def build_stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 停止回答", callback_data="act_stop_generation")]
    ])

# --- ☆ 访问配置 ☆ ---
class BotConfig:
    TOKEN = os.getenv("BOT_TOKEN", "")
    try:
        AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
    except ValueError:
        print(f"[CRITICAL] AUTHORIZED_USER_ID 环境变量必须是整数，当前值 '{os.getenv('AUTHORIZED_USER_ID')}' 无效，已默认设为 0。请检查 .env 配置。", file=sys.stderr)
        AUTHORIZED_USER_ID = 0
    # 本地 Telegram Bot API server 支持（不配置则用官方 api.telegram.org）
    API_BASE_URL = (os.getenv("TELEGRAM_API_URL") or "").strip().rstrip("/")
    DB_FILE = "xgent_memory.db"
    NORMAL_UPDATE_ZIP_URL = "https://api.github.com/repos/HANLINGVABCN/xgent-telegram/zipball/main"
    TEST_UPDATE_ZIP_URL = "https://api.github.com/repos/HANLINGVABCN/xgent-telegram-test/zipball/main"
    DEFAULT_UPDATE_ZIP_URL = NORMAL_UPDATE_ZIP_URL
    _ENV_UPDATE_ZIP_URL = (os.getenv("UPDATE_ZIP_URL") or "").strip()
    UPDATE_ZIP_URL = _ENV_UPDATE_ZIP_URL or DEFAULT_UPDATE_ZIP_URL
    UPDATE_GITHUB_TOKEN = (
        os.getenv("UPDATE_GITHUB_TOKEN")
        or os.getenv("GITHUB_TOKEN")
        or ""
    ).strip()
    # 联网搜索（search-x / fetch-x）。未配置时协议返回配置指引而不是报错。
    TAVILY_API_KEY = (os.getenv("TAVILY_API_KEY") or "").strip()


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 本地 Bot API server 容器内的数据根路径（卷映射的另一端）
_LOCAL_API_CONTAINER_DATA_DIR = "/var/lib/telegram-bot-api"
# 宿主机上对应的本地 API 数据目录
_LOCAL_API_HOST_DATA_DIR = os.path.join(PROJECT_ROOT, ".local-api-data")


async def download_telegram_file(telegram_obj) -> bytes:
    """统一下载 Telegram 文件（PhotoSize/Document/Audio/Voice/Video 等），返回字节。

    本地 Bot API server（local_mode）模式下，PTB 拿到的 file_path 是容器内路径
    （/var/lib/telegram-bot-api/...），宿主机上不存在。这里把容器路径翻译成
    宿主机实际数据目录后直接读文件。

    官方 api.telegram.org 模式下，走 PTB 原生的 HTTP 下载，行为完全不变。
    """
    file_obj = await telegram_obj.get_file()

    if BotConfig.API_BASE_URL:
        # 本地模式：PTB 把 base_file_url + token + 容器内绝对路径拼成混合 URL，
        # 例如 http://localhost:8081/file/bot<TOKEN>//var/lib/telegram-bot-api/<TOKEN>/documents/file_X.zip
        # 从中提取 /var/lib/telegram-bot-api/... 这段容器路径，翻译到宿主机数据目录直接读取。
        raw_path = getattr(file_obj, 'file_path', '') or ''
        marker = _LOCAL_API_CONTAINER_DATA_DIR + '/'
        idx = raw_path.find(marker)
        if idx != -1:
            container_path = raw_path[idx:]
            host_path = _LOCAL_API_HOST_DATA_DIR + container_path[len(_LOCAL_API_CONTAINER_DATA_DIR):]
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, lambda: open(host_path, 'rb').read())
            except FileNotFoundError:
                logger.warning(f"本地 API 文件宿主机路径不存在，回退原生下载: {host_path}")
        # 路径不符合预期或宿主机文件不存在，回退到 PTB 原生下载
        return bytes(await file_obj.download_as_bytearray())

    # 官方模式：PTB 原生 HTTP 下载
    return bytes(await file_obj.download_as_bytearray())
UPDATE_SKIP_NAMES = {
    ".env",
    ".git",
    "xgent_memory.db",
    "xgent_output.log",
    "xgent_server.log",
    "xgent.pid",
    "xgent_storage",
    "venv",
    "__pycache__",
}
UPDATE_SKIP_SUFFIXES = (
    ".log",
    ".pid",
    ".pyc",
)
UPDATE_LOCAL_CUSTOM_DIRS = ("prompts", "skill-public", "skill")
UPDATE_BACKUP_DIR = os.path.join(PROJECT_ROOT, "xgent_storage", "update_backups")
COMMAND_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "xgent_storage", "command_outputs")
_TRACE_LOG_OVERRIDE = (
    os.environ.get("XGENT_TRACE_LOG_FILE")
    or os.environ.get("TELEGRAM_AI_BOT_TRACE_LOG_FILE")
    or ""
).strip()
FULL_TRACE_LOG_FILE = os.path.abspath(
    os.path.expanduser(_TRACE_LOG_OVERRIDE or os.path.join(PROJECT_ROOT, "xgent_full_trace.log"))
)
FULL_TRACE_LOCK = threading.Lock()
DEFAULT_AGENT_COMMAND_TIMEOUT = 30
MIN_AGENT_COMMAND_TIMEOUT = 5
MAX_AGENT_COMMAND_TIMEOUT = 3600
DEFAULT_AGENT_MAX_ITERATIONS = 10
MIN_AGENT_MAX_ITERATIONS = 1
MAX_AGENT_MAX_ITERATIONS = 50
AGENT_TURN_ITERATION_CONFIG_KEY = 'agent_turn_iteration'
DEFAULT_IDLE_MESSAGE_INTERVAL = 24 * 3600
MIN_IDLE_MESSAGE_INTERVAL = 60
# AI 回复超时设为“不限”时，非流式请求的兜底上限。非流式没有增量输出，
# 连接静默挂起会让界面永远停在“非流式输出中...”，必须有个硬上限。
NONSTREAM_FALLBACK_TIMEOUT_SECONDS = 600.0
# 流式请求的“两个分片之间”最长静默时间。流式已经产出的增量可以保留，
# 所以这里限制的是单次等待而不是整轮总时长——长回复只要持续出字就不会被打断，
# 但提供商建连后不发数据时不会永久挂住全局对话锁。
STREAM_CHUNK_IDLE_TIMEOUT_SECONDS = 180.0
# typing 状态任务的防泄漏兜底。调用方异常退出而没有 set stop_event 时，
# typing 任务会每 4 秒发一次状态直到进程结束，这里给一个远大于正常回复
# 时长的上限，只用于兜底，不作为功能限制。
TYPING_MAX_DURATION_SECONDS = 1800.0
# 共享 HTTP 客户端的默认读超时。调用方通常会显式传入自己的 timeout，
# 这个值只用于兜底，避免漏传的调用点变成永不超时。
DEFAULT_HTTP_READ_TIMEOUT_SECONDS = 300.0
# 拉取模型列表是交互式操作，用户在等结果，超时要短。
MODEL_LIST_TIMEOUT_SECONDS = 20.0
# 空闲提醒是后台任务，没人盯着，必须有硬上限，否则提供商挂起会让它永久卡住。
IDLE_MESSAGE_TIMEOUT_SECONDS = 300.0
# 命令被黑名单拦截时回给模型的统一文案。刻意不包含命中的具体规则——
# 把匹配到哪一条告诉模型，等于直接指导它改写命令绕过。规则详情只写日志。
BLACKLIST_BLOCKED_NOTICE = (
    '⛔ 命令被安全策略拦截，未执行。'
    '如果确认这条命令是必要的，请让用户在「Agent 命令黑名单」菜单里调整规则。'
)
def validate_provider_base_url(url: str) -> Tuple[str, Optional[str]]:
    """校验 provider 的 base_url。

    返回 (清理后的 url, 警告文案或 None)；不合法时抛 ValueError。

    之前有三个入口三套规则：JSON 导入严格校验、交互式添加只判 startswith("http")
    （`httpevil.com` 都能过），编辑 URL 完全不校验。这里统一。
    """
    cleaned = str(url or '').strip()
    if not cleaned:
        raise ValueError('URL 不能为空')
    lowered = cleaned.lower()
    if not lowered.startswith(('http://', 'https://')):
        raise ValueError('URL 必须以 http:// 或 https:// 开头')
    parsed = urllib.parse.urlparse(cleaned)
    if not parsed.netloc:
        raise ValueError('URL 缺少主机名')

    warning = None
    if lowered.startswith('http://'):
        host = (parsed.hostname or '').lower()
        is_local = host in ('localhost', '127.0.0.1', '::1') or host.endswith('.localhost')
        if not is_local:
            warning = (
                '⚠️ 这个地址用的是 http:// 明文传输，API Key 会以明文经过网络。'
                '除非是内网自建服务，建议改用 https://。'
            )
    return cleaned, warning


PROVIDER_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def redact_sensitive_text(text: str) -> str:
    if not text:
        return text

    secrets = {
        BotConfig.TOKEN: "[REDACTED_BOT_TOKEN]",
        BotConfig.UPDATE_GITHUB_TOKEN: "[REDACTED_UPDATE_GITHUB_TOKEN]",
        BotConfig.TAVILY_API_KEY: "[REDACTED_TAVILY_API_KEY]",
    }
    for secret, replacement in secrets.items():
        if secret:
            text = text.replace(secret, replacement)
    # provider 的 api_key 存在数据库里，不是环境变量常量，但它同样会出现在
    # 错误响应体、trace 日志里，必须一起脱敏。
    for secret in _runtime_secrets():
        text = text.replace(secret, "[REDACTED_API_KEY]")
    return text


# provider 的 api_key 在运行时才知道，这里维护一份供脱敏使用的快照。
# 用 set 而不是每次去查数据库：脱敏在错误处理和日志热路径上，不能是 async。
_RUNTIME_SECRETS: set = set()


def _runtime_secrets() -> set:
    return _RUNTIME_SECRETS


def register_runtime_secret(value: Any) -> None:
    """登记一个需要脱敏的运行时密钥（provider api_key 等）。"""
    for key in parse_api_keys(str(value or '')):
        # 太短的值容易在正常文本里误伤，跳过。
        if len(key) >= 8:
            _RUNTIME_SECRETS.add(key)


def register_provider_secrets(providers: Optional[Dict[str, Any]]) -> None:
    """把所有 provider 的 api_key 登记进脱敏名单。"""
    if not providers:
        return
    for provider in providers.values():
        if isinstance(provider, dict):
            register_runtime_secret(provider.get('api_key'))

def format_provider_exception(e: Exception) -> str:
    response = getattr(e, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        try:
            body = response.text
        except Exception:
            body = None
        if body:
            prefix = f"{type(e).__name__}"
            if status_code is not None:
                prefix += f" ({status_code})"
            return f"{prefix}: {redact_sensitive_text(body)}"

    return f"{type(e).__name__}: {redact_sensitive_text(str(e))}"

def normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "open", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "closed", "disabled"}:
            return False
    return default

def normalize_stream_timeout(value: Any, default: float = 0) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = float(default)
    if seconds <= 0:
        return 0
    return seconds

def normalize_command_timeout(value: Any, default: int = DEFAULT_AGENT_COMMAND_TIMEOUT) -> int:
    if isinstance(value, str) and value.strip().lower() in {"∞", "inf", "infinite", "none", "no", "unlimited", "无限"}:
        return MAX_AGENT_COMMAND_TIMEOUT
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = int(default)
    if seconds <= 0:
        seconds = int(default)
    if seconds < MIN_AGENT_COMMAND_TIMEOUT:
        return MIN_AGENT_COMMAND_TIMEOUT
    if seconds > MAX_AGENT_COMMAND_TIMEOUT:
        return MAX_AGENT_COMMAND_TIMEOUT
    return seconds


def normalize_agent_max_iterations(value: Any, default: int = DEFAULT_AGENT_MAX_ITERATIONS) -> int:
    try:
        iterations = int(float(value))
    except (TypeError, ValueError):
        iterations = int(default)
    if iterations <= 0:
        iterations = int(default)
    if iterations < MIN_AGENT_MAX_ITERATIONS:
        return MIN_AGENT_MAX_ITERATIONS
    if iterations > MAX_AGENT_MAX_ITERATIONS:
        return MAX_AGENT_MAX_ITERATIONS
    return iterations


def normalize_idle_message_interval(value: Any, default: int = DEFAULT_IDLE_MESSAGE_INTERVAL) -> int:
    if isinstance(value, str) and value.strip().lower() in {
        "0", "∞", "inf", "infinite", "none", "no", "off", "disabled", "unlimited",
        "无限", "关闭", "关", "不触发", "停用",
    }:
        return 0
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = int(default)
    if seconds <= 0:
        return 0
    if seconds < MIN_IDLE_MESSAGE_INTERVAL:
        return MIN_IDLE_MESSAGE_INTERVAL
    return seconds


def parse_agent_max_iterations(text: str) -> int:
    cleaned = str(text or "").strip().lower()
    cleaned = re.sub(r"(轮|次|rounds?|iterations?|iters?)$", "", cleaned).strip()
    try:
        iterations = int(float(cleaned))
    except (TypeError, ValueError):
        raise ValueError("iterations must be a number")
    if iterations < MIN_AGENT_MAX_ITERATIONS:
        raise ValueError(f"iterations must be at least {MIN_AGENT_MAX_ITERATIONS}")
    if iterations > MAX_AGENT_MAX_ITERATIONS:
        raise ValueError(f"iterations must be at most {MAX_AGENT_MAX_ITERATIONS}")
    return iterations


def parse_idle_message_interval(text: str) -> int:
    cleaned = str(text or "").strip().lower()
    if cleaned in {
        "0", "∞", "inf", "infinite", "none", "no", "off", "disabled", "unlimited",
        "无限", "关闭", "关", "不触发", "停用",
    }:
        return 0

    multiplier = 1
    unit_patterns = [
        (r"(hours?|hrs?|h|小时|时)$", 3600),
        (r"(days?|d|天|日)$", 86400),
        (r"(minutes?|mins?|m|分钟|分)$", 60),
        (r"(seconds?|secs?|sec|s|秒)$", 1),
    ]
    for pattern, unit_multiplier in unit_patterns:
        if re.search(pattern, cleaned):
            multiplier = unit_multiplier
            cleaned = re.sub(pattern, "", cleaned).strip()
            break

    try:
        seconds = int(float(cleaned) * multiplier)
    except (TypeError, ValueError):
        raise ValueError("idle interval must be a number")

    if seconds <= 0:
        return 0
    if seconds < MIN_IDLE_MESSAGE_INTERVAL:
        raise ValueError(f"idle interval must be at least {MIN_IDLE_MESSAGE_INTERVAL} seconds")
    return seconds


def parse_timeout_seconds(text: str, minimum: int = 1, maximum: Optional[int] = None,
                          allow_infinite: bool = False) -> int:
    cleaned = str(text or "").strip().lower()
    if cleaned in {"∞", "inf", "infinite", "none", "no", "unlimited", "无限", "不限制"}:
        if allow_infinite:
            return 0
        raise ValueError("timeout must be a number of seconds")
    cleaned = cleaned.removesuffix("seconds").removesuffix("second").removesuffix("secs").removesuffix("sec")
    cleaned = cleaned.removesuffix("秒").removesuffix("s").strip()
    try:
        seconds = int(float(cleaned))
    except (TypeError, ValueError):
        raise ValueError("timeout must be a number of seconds")
    if seconds <= 0:
        if allow_infinite:
            return 0
        raise ValueError(f"timeout must be at least {minimum} seconds")
    if seconds < minimum:
        raise ValueError(f"timeout must be at least {minimum} seconds")
    if maximum is not None and seconds > maximum:
        raise ValueError(f"timeout must be at most {maximum} seconds")
    return seconds


def _value_from_obj(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _nested_value_from_obj(obj: Any, path: List[str], default: Any = None) -> Any:
    current = obj
    for key in path:
        current = _value_from_obj(current, key, None)
        if current is None:
            return default
    return current


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def make_trace_id(prefix: str = "model") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def trace_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    if isinstance(value, dict):
        return {str(k): trace_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [trace_json_safe(v) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return trace_json_safe(model_dump())
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return trace_json_safe(to_dict())
        except Exception:
            pass
    return repr(value)


_TRACE_QUEUE: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=2000)
_TRACE_WRITER_THREAD: Optional[threading.Thread] = None
_TRACE_WRITER_LOCK = threading.Lock()
_TRACE_DROPPED = 0


def _trace_writer_loop():
    """后台线程：串行地把 trace 行落盘。"""
    while True:
        line = _TRACE_QUEUE.get()
        try:
            if line is None:
                return
            with FULL_TRACE_LOCK:
                with open(FULL_TRACE_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            logger.debug(f"模型全量日志写入失败: {e}")
        finally:
            _TRACE_QUEUE.task_done()


def _ensure_trace_writer():
    global _TRACE_WRITER_THREAD
    thread = _TRACE_WRITER_THREAD
    if thread is not None and thread.is_alive():
        return
    with _TRACE_WRITER_LOCK:
        thread = _TRACE_WRITER_THREAD
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=_trace_writer_loop, name="model-trace-writer", daemon=True
        )
        thread.start()
        _TRACE_WRITER_THREAD = thread


def write_model_trace(event: str, payload: Dict[str, Any]):
    """Append every full-fidelity action/model event to one chronological JSONL log.

    序列化在调用线程做，落盘丢给后台线程：这个函数在对话热路径上被同步调用，
    而 FULL_TRACE_LOCK 是 threading.Lock——直接在事件循环里持锁写文件会把
    整个 bot 卡住。队列满时丢弃并计数，日志不能反过来拖垮主流程。
    """
    global _TRACE_DROPPED
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            **trace_json_safe(payload),
        }
        line = json.dumps(record, ensure_ascii=False, default=repr)
        # 落盘前脱敏：trace 里包含完整 prompt 和响应体，provider 的 api_key
        # 会随请求头/错误体一起被记进去。
        line = redact_sensitive_text(line)
        _ensure_trace_writer()
        try:
            _TRACE_QUEUE.put_nowait(line)
        except queue.Full:
            _TRACE_DROPPED += 1
            if _TRACE_DROPPED % 100 == 1:
                logger.warning(f"trace 队列已满，累计丢弃 {_TRACE_DROPPED} 条")
    except Exception as e:
        logger.debug(f"模型全量日志写入失败: {e}")


def flush_model_trace(timeout: float = 3.0) -> None:
    """等待后台 trace 线程把队列写完（关闭时调用）。"""
    thread = _TRACE_WRITER_THREAD
    if thread is None or not thread.is_alive():
        return
    deadline = time.monotonic() + timeout
    while not _TRACE_QUEUE.empty() and time.monotonic() < deadline:
        time.sleep(0.05)


def extract_token_usage(raw_usage: Any) -> Optional[Dict[str, int]]:
    """Normalize usage objects from OpenAI-compatible, Gemini, and Claude APIs."""
    if not raw_usage:
        return None

    input_tokens = _int_or_none(_value_from_obj(raw_usage, 'prompt_tokens'))
    if input_tokens is None:
        input_tokens = _int_or_none(_value_from_obj(raw_usage, 'input_tokens'))
    if input_tokens is None:
        input_tokens = _int_or_none(_value_from_obj(raw_usage, 'promptTokenCount'))

    output_tokens = _int_or_none(_value_from_obj(raw_usage, 'completion_tokens'))
    if output_tokens is None:
        output_tokens = _int_or_none(_value_from_obj(raw_usage, 'output_tokens'))
    if output_tokens is None:
        output_tokens = _int_or_none(_value_from_obj(raw_usage, 'candidatesTokenCount'))

    reasoning_tokens = _int_or_none(_nested_value_from_obj(raw_usage, ['completion_tokens_details', 'reasoning_tokens']))
    if reasoning_tokens is None:
        reasoning_tokens = _int_or_none(_nested_value_from_obj(raw_usage, ['output_token_details', 'reasoning_tokens']))
    if reasoning_tokens is None:
        reasoning_tokens = _int_or_none(_value_from_obj(raw_usage, 'thoughtsTokenCount'))

    cached_tokens = _int_or_none(_nested_value_from_obj(raw_usage, ['prompt_tokens_details', 'cached_tokens']))
    if cached_tokens is None:
        cached_tokens = _int_or_none(_nested_value_from_obj(raw_usage, ['input_token_details', 'cached_tokens']))
    if cached_tokens is None:
        cached_tokens = _int_or_none(_value_from_obj(raw_usage, 'cached_tokens'))
    if cached_tokens is None:
        cached_tokens = _int_or_none(_value_from_obj(raw_usage, 'cache_read_input_tokens'))
    if cached_tokens is None:
        cached_tokens = _int_or_none(_value_from_obj(raw_usage, 'cachedContentTokenCount'))

    total_tokens = _int_or_none(_value_from_obj(raw_usage, 'total_tokens'))
    if total_tokens is None:
        total_tokens = _int_or_none(_value_from_obj(raw_usage, 'totalTokenCount'))

    usage: Dict[str, int] = {}
    if input_tokens is not None:
        usage['input_tokens'] = input_tokens
    if output_tokens is not None:
        usage['output_tokens'] = output_tokens
    if (
        input_tokens is not None
        and output_tokens is not None
        and reasoning_tokens is not None
        and total_tokens is not None
        and input_tokens + output_tokens + reasoning_tokens == total_tokens
    ):
        usage['visible_output_tokens'] = output_tokens
        usage['reasoning_tokens'] = reasoning_tokens
        usage['output_tokens'] = output_tokens + reasoning_tokens
    elif reasoning_tokens is not None:
        usage['reasoning_tokens'] = reasoning_tokens
    if cached_tokens is not None:
        usage['cached_tokens'] = cached_tokens
    if total_tokens is not None:
        usage['total_tokens'] = total_tokens

    if 'total_tokens' not in usage and input_tokens is not None and output_tokens is not None:
        usage['total_tokens'] = input_tokens + output_tokens

    return usage or None


def record_token_usage(usage_sink: Optional[List[Dict[str, int]]], raw_usage: Any):
    if usage_sink is None:
        return
    usage = extract_token_usage(raw_usage)
    if not usage:
        return
    usage['raw_usage'] = trace_json_safe(raw_usage)

    if usage_sink:
        current = usage_sink[0]
        for key, value in usage.items():
            current[key] = value
    else:
        usage_sink.append(usage)


def format_token_rate(output_tokens: int, elapsed_seconds: float) -> str:
    if elapsed_seconds <= 0:
        return "0"
    rate = output_tokens / elapsed_seconds
    if rate >= 10:
        return str(int(round(rate)))
    if rate >= 1:
        return f"{rate:.1f}"
    return f"{rate:.2f}"


def build_token_usage_message(usage: Optional[Dict[str, int]], elapsed_seconds: float) -> Optional[str]:
    if not usage:
        return None

    input_tokens = usage.get('input_tokens')
    output_tokens = usage.get('output_tokens')
    if input_tokens is None and output_tokens is None:
        return None

    cached_tokens = usage.get('cached_tokens', 0)
    input_display = input_tokens if input_tokens is not None else 0
    output_display = output_tokens if output_tokens is not None else 0
    rate_display = format_token_rate(output_display, elapsed_seconds)
    visible_output_tokens = usage.get('visible_output_tokens')
    reasoning_tokens = usage.get('reasoning_tokens')
    output_detail = ""
    if visible_output_tokens is not None and reasoning_tokens is not None:
        output_detail = f" ({visible_output_tokens} text + {reasoning_tokens} thoughts)"
    body = (
        f"↑ {input_display} tokens ({cached_tokens} cached) · "
        f"↓ {output_display} tokens{output_detail} · "
        f"⚡ {rate_display} tokens/s"
    )
    return f"<i>{html.escape(body)}</i>"


async def send_token_usage_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                   usage: Optional[Dict[str, int]], elapsed_seconds: float,
                                   token_text_sink: Optional[List[str]] = None) -> None:
    """发送 token 用量提示消息。

    落库不在本函数做——本函数在正文落库之前被调用，此时落库会让 token 记录的
    timestamp 早于正文，刷新后顺序反成「tokens + 输出」。改为把 token 文本写进
    token_text_sink，由调用方在正文落库之后再落库，保证顺序为「输出 + tokens」。
    """
    text = build_token_usage_message(usage, elapsed_seconds)
    if not text:
        return
    if token_text_sink is not None:
        token_text_sink.append(text)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=constants.ParseMode.HTML,
            disable_notification=True
        )
    except Exception as e:
        logger.debug(f"token 使用量消息发送失败: {e}")
        write_model_trace("telegram_token_usage_error", {
            "chat_id": chat_id,
            "message": text,
            "elapsed_seconds": elapsed_seconds,
            "error": format_provider_exception(e),
        })

# --- ☆ 日志系统 ☆ ---
def setup_logging():
    _logger = logging.getLogger()
    _logger.setLevel(logging.INFO)
    if _logger.hasHandlers(): 
        _logger.handlers.clear()
    
    log_format = logging.Formatter('%(asctime)s - [Bot] - %(levelname)s - %(message)s')
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_format)
    _logger.addHandler(stream_handler)
    
    log_file = os.path.join(os.getcwd(), "xgent_server.log")
    file_handler = RotatingFileHandler(
        log_file, encoding='utf-8',
        maxBytes=10 * 1024 * 1024,  # 10MB per file
        backupCount=5
    )
    file_handler.setFormatter(log_format)
    _logger.addHandler(file_handler)
    
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    return logging.getLogger("TelegramAIBot")

logger = setup_logging()

if not BotConfig.TOKEN or BotConfig.AUTHORIZED_USER_ID == 0:
    logger.critical("❌ 配置错误！请检查 .env 文件里的 BOT_TOKEN 和 AUTHORIZED_USER_ID！")
    sys.exit(1)

write_model_trace("bot_process_start", {
    "pid": os.getpid(),
    "cwd": os.getcwd(),
    "project_root": PROJECT_ROOT,
    "full_trace_log": FULL_TRACE_LOG_FILE,
})

# --- ☆ 状态定义 ☆ ---
class BotState:
    IDLE = None
    ADD_PROV_NAME = 'add_prov_name'
    ADD_PROV_URL = 'add_prov_url'
    ADD_PROV_KEY = 'add_prov_key'
    EDIT_PROV_NAME = 'edit_prov_name'
    EDIT_PROV_KEY = 'edit_prov_key'
    EDIT_PROV_URL = 'edit_prov_url'
    ADD_MODEL_MANUAL = 'add_model_manual'
    SEARCH_FETCHED = 'search_fetched_models'
    SEARCH_SAVED = 'search_saved_models'
    RENAME_CHAT = 'rename_chat'
    SET_PROMPT = 'set_prompt'
    SET_GLOBAL_PROMPT = 'set_global_prompt'
    SET_ANY_PROMPT = 'set_any_prompt'
    SET_GLOBAL_DEPTH = 'set_global_depth'
    SET_AI_TIMEOUT = 'set_ai_timeout'
    SET_COMMAND_TIMEOUT = 'set_command_timeout'
    SET_AGENT_MAX_ITERATIONS = 'set_agent_max_iterations'
    SET_IDLE_MESSAGE_INTERVAL = 'set_idle_message_interval'
    SET_SMART_MATCH = 'set_smart_match'
    SET_COMMAND_BLACKLIST = 'set_command_blacklist'
    SET_UPDATE_TOKEN = 'set_update_token'
    SET_SEARCH_KEY = 'set_search_key'
    SET_MEMORY = 'set_memory'
    SET_WEB_PASSWORD = 'set_web_password'
    SET_WEB_PORT = 'set_web_port'
    SET_WEB_PUBLIC_URL = 'set_web_public_url'
    IMPORT_PROVIDER_CONFIG = 'import_provider_config'

PROVIDER_CONFIG_FORMAT = 'xgent-telegram-provider-config'
LEGACY_PROVIDER_CONFIG_FORMATS = {'telegram-ai-bot-provider-config'}
PROVIDER_CONFIG_VERSION = 1
PROVIDER_CONFIG_MAX_BYTES = 2 * 1024 * 1024
PROVIDER_CONFIG_MAX_PROVIDERS = 100
VALID_PROVIDER_API_FORMATS = {'openai', 'openai_compatible', 'gemini', 'vertex', 'claude'}

# --- ☆ 消息类型定义（用于全局记录）☆ ---
class MessageType:
    USER_TEXT = 'user_text'
    USER_FILE = 'user_file'
    USER_PHOTO = 'user_photo'
    USER_STICKER = 'user_sticker'
    AI_REPLY = 'ai_reply'
    MEDIA_REPLY = 'media_reply'       # 外部媒体模块回复，和聊天AI分开记
    SYSTEM_OP = 'system_op'
    BUTTON_CLICK = 'button_click'
    COMMAND = 'command'
    AGENT_CMD = 'agent_cmd'           # Agent 请求的工具动作
    AGENT_RESULT = 'agent_result'     # Agent 工具结果
    TOKEN_USAGE = 'token_usage'       # 每轮回复末尾的 token 用量提示（独立消息，常驻历史）

def _read_int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)

TEXT_STITCH_MODE_AUTO = "auto"
TEXT_STITCH_MODE_FORCE = "force"
TEXT_STITCH_MODE_OFF = "off"
TEXT_STITCH_MODES = {TEXT_STITCH_MODE_AUTO, TEXT_STITCH_MODE_FORCE, TEXT_STITCH_MODE_OFF}
DEFAULT_TEXT_STITCH_MODE = TEXT_STITCH_MODE_AUTO
TELEGRAM_TEXT_LIMIT_CHARS = 4096
TEXT_STITCH_SPLIT_HINT_CHARS = _read_int_env("TEXT_STITCH_SPLIT_HINT_CHARS", 3800)


class PendingTextConversation:
    """Collect Telegram text fragments until the user taps Done."""

    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        self.update = update
        self.context = context
        self.parts: List[str] = [text]
        self.first_at = time.monotonic()
        self.last_at = self.first_at
        self.prompt_message: Optional[Any] = None

    def append(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        self.update = update
        self.context = context
        self.parts.append(text)
        self.last_at = time.monotonic()

    def total_chars(self) -> int:
        return sum(len(part) for part in self.parts)


_pending_text_conversations: Dict[Tuple[int, int], PendingTextConversation] = {}
_pending_text_conversations_lock = threading.RLock()


def merge_text_conversation_parts(parts: List[str]) -> str:
    """Merge text parts while treating near-limit chunks as Telegram auto-splits."""
    cleaned_parts = [str(part or "") for part in parts if str(part or "").strip()]
    if not cleaned_parts:
        return ""

    merged = cleaned_parts[0]
    previous = cleaned_parts[0]
    for part in cleaned_parts[1:]:
        if len(previous) >= TEXT_STITCH_SPLIT_HINT_CHARS:
            merged += part
        elif merged.endswith("\n") or part.startswith("\n"):
            merged += part
        else:
            merged += "\n\n" + part
        previous = part

    return merged.strip()


def normalize_text_stitch_mode(value: Any) -> str:
    mode = str(value or DEFAULT_TEXT_STITCH_MODE).strip().lower()
    if mode in {"on", "true", "1", "always", "force_on", "强制", "开启"}:
        return TEXT_STITCH_MODE_FORCE
    if mode in {"none", "false", "0", "disabled", "close", "closed", "关闭"}:
        return TEXT_STITCH_MODE_OFF
    if mode not in TEXT_STITCH_MODES:
        return DEFAULT_TEXT_STITCH_MODE
    return mode


# 流式风格：仅当 stream_mode=True 时有意义
STREAM_STYLE_FOREGROUND = "foreground"   # 前台流式：实时推送到 Telegram（draft / edit）
STREAM_STYLE_BACKGROUND = "background"   # 后台流式：累积后一次性发送，避开限流
STREAM_STYLES = {STREAM_STYLE_FOREGROUND, STREAM_STYLE_BACKGROUND}
DEFAULT_STREAM_STYLE = STREAM_STYLE_FOREGROUND


def normalize_stream_style(value: Any) -> str:
    """归一化流式风格字段。任何无法识别的值都回退到默认（前台流式）。"""
    style = str(value or DEFAULT_STREAM_STYLE).strip().lower()
    if style in {"bg", "back", "后台", "后台流式"}:
        return STREAM_STYLE_BACKGROUND
    if style in {"fg", "front", "前台", "前台流式"}:
        return STREAM_STYLE_FOREGROUND
    if style not in STREAM_STYLES:
        return DEFAULT_STREAM_STYLE
    return style


def get_stream_style_label(style: Optional[str] = None) -> str:
    style = normalize_stream_style(style if style is not None else UserDataManager.get('stream_style'))
    if style == STREAM_STYLE_BACKGROUND:
        return "后台流式"
    return "前台流式"


def get_text_stitch_mode_label(mode: Optional[str] = None) -> str:
    mode = normalize_text_stitch_mode(mode if mode is not None else UserDataManager.get('text_stitch_mode'))
    if mode == TEXT_STITCH_MODE_FORCE:
        return "强制开"
    if mode == TEXT_STITCH_MODE_OFF:
        return "关闭"
    return "自动"


def should_stitch_text_message(text: str, mode: Optional[str] = None) -> bool:
    mode = normalize_text_stitch_mode(mode if mode is not None else UserDataManager.get('text_stitch_mode'))
    if mode == TEXT_STITCH_MODE_OFF:
        return False
    if mode == TEXT_STITCH_MODE_FORCE:
        return True
    return len(text or "") >= TEXT_STITCH_SPLIT_HINT_CHARS


def get_text_conversation_buffer_key(update: Update) -> Tuple[int, int]:
    chat_id = update.effective_chat.id if update.effective_chat else BotConfig.AUTHORIZED_USER_ID
    user_id = update.effective_user.id if update.effective_user else BotConfig.AUTHORIZED_USER_ID
    return chat_id, user_id


# ---------------------------------------------------------------------------
# 思考深度（reasoning / extended thinking）
#
# 各家的字段名和取值完全不同，这里统一成一组档位，发请求时再翻译：
#   Claude  -> thinking.budget_tokens（且 max_tokens 必须大于它）
#   Gemini  -> generationConfig.thinkingConfig.thinkingBudget
#   OpenAI  -> reasoning_effort
#   OpenRouter -> reasoning.effort
#
# AUTO 表示「一个思考字段都不发」，让提供商用自己的默认值。这是默认档位，
# 因为不支持思考的模型（gpt-4o、claude-3-5-haiku 等）收到未知字段会直接 400。
# ---------------------------------------------------------------------------

THINKING_LEVEL_OFF = "off"
THINKING_LEVEL_AUTO = "auto"
THINKING_LEVEL_LOW = "low"
THINKING_LEVEL_MEDIUM = "medium"
THINKING_LEVEL_HIGH = "high"
THINKING_LEVEL_XHIGH = "xhigh"
THINKING_LEVEL_ULTRA = "ultra"
THINKING_LEVEL_MAX = "max"

# 有序，菜单按这个顺序渲染
THINKING_LEVEL_ORDER = (
    THINKING_LEVEL_OFF,
    THINKING_LEVEL_AUTO,
    THINKING_LEVEL_LOW,
    THINKING_LEVEL_MEDIUM,
    THINKING_LEVEL_HIGH,
    THINKING_LEVEL_XHIGH,
    THINKING_LEVEL_ULTRA,
    THINKING_LEVEL_MAX,
)
THINKING_LEVELS = set(THINKING_LEVEL_ORDER)
DEFAULT_THINKING_LEVEL = THINKING_LEVEL_AUTO

THINKING_LEVEL_LABELS = {
    THINKING_LEVEL_OFF: "关闭",
    THINKING_LEVEL_AUTO: "自动",
    THINKING_LEVEL_LOW: "低",
    THINKING_LEVEL_MEDIUM: "中",
    THINKING_LEVEL_HIGH: "高",
    THINKING_LEVEL_XHIGH: "很高",
    THINKING_LEVEL_ULTRA: "超高",
    THINKING_LEVEL_MAX: "最高",
}

# budget: Claude / Gemini 的思考 token 预算；-1 表示交给模型动态决定（Gemini 语义）。
# effort: OpenAI 系的档位名。两家档位数量不一样，高档位会重复映射。
THINKING_LEVEL_SPECS: Dict[str, Dict[str, Any]] = {
    THINKING_LEVEL_LOW: {"budget": 2048, "effort": "low"},
    THINKING_LEVEL_MEDIUM: {"budget": 8192, "effort": "medium"},
    THINKING_LEVEL_HIGH: {"budget": 16384, "effort": "high"},
    THINKING_LEVEL_XHIGH: {"budget": 24576, "effort": "high"},
    THINKING_LEVEL_ULTRA: {"budget": 32768, "effort": "xhigh"},
    THINKING_LEVEL_MAX: {"budget": -1, "effort": "max"},
}

# Claude 要求 max_tokens > budget_tokens，否则直接 400。budget 为 -1（动态）时
# 没有具体数值可参照，用这个上限兜底。
CLAUDE_THINKING_DYNAMIC_BUDGET = 32768
CLAUDE_THINKING_ANSWER_HEADROOM = 4096
CLAUDE_DEFAULT_MAX_TOKENS = 4096


def normalize_thinking_level(value: Any, default: str = DEFAULT_THINKING_LEVEL) -> str:
    level = str(value if value is not None else default).strip().lower()
    if level in {"none", "false", "0", "disabled", "close", "closed", "关闭", "关"}:
        return THINKING_LEVEL_OFF
    if level in {"default", "auto", "自动"}:
        return THINKING_LEVEL_AUTO
    if level not in THINKING_LEVELS:
        return default
    return level


def get_thinking_level_label(level: Optional[str] = None) -> str:
    level = normalize_thinking_level(
        level if level is not None else UserDataManager.get('thinking_level')
    )
    return THINKING_LEVEL_LABELS.get(level, THINKING_LEVEL_LABELS[DEFAULT_THINKING_LEVEL])


# ---------------------------------------------------------------------------
# Web Chat
#
# 默认只绑 127.0.0.1：这个界面能驱动 Agent 在服务器上执行真实命令，直接
# 对公网监听等于把控制台暴露出去。要远程访问就自己配反向代理。
#
# Telegram 的 WebApp 按钮只接受 HTTPS 地址，所以公开地址单独配一项；没配
# 时按钮降级成普通链接，用外部浏览器打开 127.0.0.1。
# ---------------------------------------------------------------------------

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8790
MIN_WEB_PORT = 1024
MAX_WEB_PORT = 65535
WEB_PASSWORD_CONFIG_KEY = 'web_password_hash'


def normalize_web_port(value: Any, default: int = DEFAULT_WEB_PORT) -> int:
    try:
        port = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    if port < MIN_WEB_PORT or port > MAX_WEB_PORT:
        return int(default)
    return port


def parse_web_port(text: str) -> int:
    """解析用户输入的端口，越界抛 ValueError 交给调用方提示。"""
    cleaned = str(text or "").strip()
    try:
        port = int(cleaned)
    except (TypeError, ValueError):
        raise ValueError("port must be a number")
    if port < MIN_WEB_PORT or port > MAX_WEB_PORT:
        raise ValueError(f"port must be between {MIN_WEB_PORT} and {MAX_WEB_PORT}")
    return port


def normalize_web_public_url(value: Any) -> str:
    """公开地址必须是 HTTPS——Telegram 服务器会拒绝非 HTTPS 的 web_app URL。"""
    url = str(value or "").strip().rstrip('/')
    if not url:
        return ""
    if not url.startswith("https://"):
        raise ValueError("public url must start with https://")
    return url


def has_pending_text_conversation(update: Update) -> bool:
    key = get_text_conversation_buffer_key(update)
    with _pending_text_conversations_lock:
        return key in _pending_text_conversations


# ---------------------------------------------------------------------------
# Album (media_group) photo buffering
#
# Telegram delivers each photo of an album as a separate update that all
# share the same ``media_group_id``. Buffer them per ``(chat_id, media_group_id)``
# and flush once the quiet window elapses so the whole album reaches the AI as
# a single multimodal message instead of one conversation per photo.
# ---------------------------------------------------------------------------

ALBUM_FLUSH_QUIET_SECONDS = 3.0
ALBUM_MAX_PHOTOS = 10  # Telegram album hard cap; defensive truncation.


class PendingAlbumConversation:
    """Collect photos sharing one Telegram ``media_group_id`` until the quiet window elapses."""

    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, media_group_id: str):
        self.update = update  # representative update (caption-bearing, falls back to first)
        self.context = context
        self.media_group_id = media_group_id
        self.photos: List[Dict[str, str]] = []  # each: {image_b64, saved_notice, index_text}
        self.caption: str = ""
        self.flush_task: Optional[Any] = None
        self.closed: bool = False

    def add_photo(self, image_b64: str, saved_notice: str, index_text: str,
                  caption: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.photos.append({
            "image_b64": image_b64,
            "saved_notice": saved_notice,
            "index_text": index_text,
        })
        if caption and not self.caption:
            self.caption = caption
            self.update = update
            self.context = context


_pending_album_conversations: Dict[Tuple[int, str], "PendingAlbumConversation"] = {}
_pending_album_conversations_lock = threading.RLock()


REDUNDANT_AGENT_COMMAND_PREFIXES: Tuple[str, ...] = ()

def is_redundant_agent_command_record(msg_type: str, content: Any) -> bool:
    text = str(content or "")
    return msg_type == MessageType.AGENT_CMD and any(
        text.startswith(prefix) for prefix in REDUNDANT_AGENT_COMMAND_PREFIXES
    )


# --- ☆ 提示词文件管理器 ☆ ---
class PromptFileManager:
    """管理提示词文件：从文件加载、写入文件、自动创建默认文件"""
    
    PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
    
    FILES = {
        'assistant_prompt': 'main.txt',
        'global_prompt_addon': 'global_addon.txt',
        'agent_prompt_addon': 'agent_addon.txt',
        'agent_disabled_addon': 'agent_disabled_addon.txt',
        'idle_message_prompt': os.path.join('extras', 'idle_message.txt'),
        'unauthorized_reply_messages': os.path.join('extras', 'unauthorized_reply_messages.txt'),
    }

    LABELS = {
        'assistant_prompt': '助手提示词',
        'global_prompt_addon': '全局追加提示词',
        'agent_prompt_addon': 'Agent 模式提示词',
        'agent_disabled_addon': 'Agent 关闭提示词',
        'idle_message_prompt': '空闲提醒提示词',
        'unauthorized_reply_messages': '未授权用户拦截回复语录',
    }
    
    _cache: Dict[str, str] = {}
    
    @classmethod
    def init(cls):
        """初始化：确保目录和文件存在，加载到缓存"""
        os.makedirs(cls.PROMPTS_DIR, exist_ok=True)
        for key, filename in cls.FILES.items():
            filepath = os.path.join(cls.PROMPTS_DIR, filename)
            if not os.path.exists(filepath):
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write('')
                logger.warning(f"提示词文件不存在，已创建空文件: {filename}")
        cls.reload_all()
    
    @classmethod
    def reload_all(cls):
        """从文件重新加载所有提示词到缓存"""
        for key, filename in cls.FILES.items():
            filepath = os.path.join(cls.PROMPTS_DIR, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    cls._cache[key] = f.read()
                logger.info(f"加载提示词: {filename} ({len(cls._cache[key])}字)")
            except Exception as e:
                logger.error(f"加载提示词文件失败 {filename}: {e}")
                cls._cache[key] = ''
    
    @classmethod
    def get(cls, key: str) -> str:
        """获取提示词内容"""
        return cls._cache.get(key, '')
    
    @classmethod
    def set(cls, key: str, content: str):
        """设置提示词并同步写入文件"""
        cls._cache[key] = content
        filename = cls.FILES.get(key)
        if filename:
            filepath = os.path.join(cls.PROMPTS_DIR, filename)
            try:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                logger.info(f"提示词已写入文件: {filename}")
            except Exception as e:
                logger.error(f"写入提示词文件失败 {filename}: {e}")

    @classmethod
    def get_path(cls, key: str) -> str:
        filename = cls.FILES.get(key, '')
        return to_display_path(os.path.join(cls.PROMPTS_DIR, filename))

    @classmethod
    def get_abs_path(cls, key: str) -> str:
        filename = cls.FILES.get(key, '')
        return os.path.join(cls.PROMPTS_DIR, filename)

    @classmethod
    def get_label(cls, key: str) -> str:
        return cls.LABELS.get(key, key)

# 初始化提示词文件
PromptFileManager.init()


# --- ☆ Agent 命令黑名单管理器 ☆ ---
class AgentCommandBlacklist:
    """管理用户自定义的 Agent 命令黑名单。"""

    FILE_PATH = os.path.join(PromptFileManager.PROMPTS_DIR, 'extras', 'agent_command_blacklist.txt')
    HEADER = (
        "# Agent 命令黑名单\n"
        "# 每行一个禁止片段，命令中包含该片段就会被拦截。\n"
        "# 空行、独立一行的 ---、以及 # 开头的注释会被忽略。修改文件后请在菜单里点“重载黑名单”。\n"
    )
    RECOMMENDED_PATTERNS = [
        'rm -rf /', 'rm -rf /*', 'rm -rf ~',
        'mkfs', 'dd if=', 'dd of=/dev',
        'shutdown', 'reboot', 'poweroff', 'halt', 'init 0', 'init 6',
        ':(){ :|:& };:', 'fork bomb',
        '> /dev/sd', '> /dev/nvme',
        'chmod -R 777 /', 'chown -R',
        'mv /* ', 'mv / ',
        'wget -O- | sh', 'curl | sh', 'curl | bash',
        'passwd', 'userdel', 'usermod',
        'iptables -F', 'iptables -X',
        'kill -9 1', 'kill -9 -1', 'killall',
        'systemctl disable', 'systemctl mask',
        'echo > /etc/passwd', 'echo > /etc/shadow',
        '/dev/null > ', '> /etc/',
    ]
    _patterns: List[str] = []

    @classmethod
    def init(cls):
        os.makedirs(os.path.dirname(cls.FILE_PATH), exist_ok=True)
        if not os.path.exists(cls.FILE_PATH):
            # 只写注释头，不预填规则：拦哪些命令由用户自己决定。
            # 运维场景下 shutdown / killall / systemctl disable 都是正常操作，
            # 默认拦下来会挡住真实用途。推荐名单放在 RECOMMENDED_PATTERNS，
            # 用户可在菜单「⭐ 查看推荐名单」/「➕ 追加推荐名单」按需启用。
            with open(cls.FILE_PATH, 'w', encoding='utf-8') as f:
                f.write(cls.HEADER)
            logger.info("已创建 Agent 命令黑名单文件（默认为空，可在菜单里追加推荐名单）")
        cls.reload()

    @classmethod
    def parse(cls, content: str) -> List[str]:
        patterns: List[str] = []
        seen = set()
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line == '---' or line.startswith('#'):
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            patterns.append(line)
        return patterns

    @classmethod
    def parse_user_input(cls, content: str) -> List[str]:
        """Parse Telegram batch input. Supports newline entries and standalone --- separators."""
        normalized = re.sub(r'(?m)^\s*---\s*$', '\n', content)
        # Legacy compatibility for older UI copy that suggested a standalone dot separator.
        normalized = re.sub(r'(?m)^\s*\.\s*$', '\n', normalized)
        return cls.parse(normalized)

    @classmethod
    def reload(cls) -> List[str]:
        try:
            with open(cls.FILE_PATH, 'r', encoding='utf-8') as f:
                cls._patterns = cls.parse(f.read())
            logger.info(f"加载 Agent 命令黑名单: {len(cls._patterns)} 条")
        except Exception as e:
            # 保留上一次成功加载的规则。以前这里会清空成 []，等于读文件一失败
            # 就把所有命令全部放行——安全控制失效时必须保守，不能 fail-open。
            logger.error(
                f"加载 Agent 命令黑名单失败，继续沿用已加载的 {len(cls._patterns)} 条规则: {e}"
            )
        return cls._patterns

    @classmethod
    def save(cls, patterns: List[str]):
        cleaned = cls.parse("\n".join(patterns))
        content = cls.HEADER + "\n" + "\n".join(cleaned) + ("\n" if cleaned else "")
        os.makedirs(os.path.dirname(cls.FILE_PATH), exist_ok=True)
        with open(cls.FILE_PATH, 'w', encoding='utf-8') as f:
            f.write(content)
        cls._patterns = cleaned
        logger.info(f"Agent 命令黑名单已保存: {len(cleaned)} 条")

    @classmethod
    def add(cls, patterns: List[str]) -> int:
        existing = cls.get_patterns()
        before = len(existing)
        cls.save(existing + patterns)
        return len(cls._patterns) - before

    @classmethod
    def clear(cls):
        cls.save([])

    @classmethod
    def get_patterns(cls) -> List[str]:
        return list(cls._patterns)

    @classmethod
    def get_display_path(cls) -> str:
        return os.path.abspath(cls.FILE_PATH).replace('\\', '/')

    # shell 里能把同一条命令写成很多形态：多个空格、制表符、${IFS}、
    # 反斜杠转义（\rm）、把命令名拆开的引号（'r'm）。朴素的子串匹配
    # 只要碰上任意一种就失效，所以比较前先把两边都规范化。
    _IFS_RE = re.compile(r'\$\{IFS\}|\$IFS', re.IGNORECASE)
    _ESCAPE_RE = re.compile(r'\\(?=[A-Za-z0-9])')
    _QUOTE_RE = re.compile(r'[\'"`]')
    _WHITESPACE_RE = re.compile(r'\s+')

    @classmethod
    def normalize_for_match(cls, text: str) -> str:
        """把命令/规则压成可比较的规范形态。"""
        normalized = cls._IFS_RE.sub(' ', text or '')
        normalized = cls._ESCAPE_RE.sub('', normalized)
        normalized = cls._QUOTE_RE.sub('', normalized)
        normalized = cls._WHITESPACE_RE.sub(' ', normalized)
        return normalized.strip().lower()

    @classmethod
    def check(cls, command: str) -> Tuple[bool, str]:
        normalized_cmd = cls.normalize_for_match(command)
        # 再准备一份去掉全部空白的形态，用来识别 `curl|sh` 这种
        # 把规则里的空格直接删掉的写法。
        squeezed_cmd = normalized_cmd.replace(' ', '')
        for pattern in cls._patterns:
            normalized_pattern = cls.normalize_for_match(pattern)
            if not normalized_pattern:
                continue
            if normalized_pattern in normalized_cmd:
                return True, pattern
            squeezed_pattern = normalized_pattern.replace(' ', '')
            if squeezed_pattern and squeezed_pattern in squeezed_cmd:
                return True, pattern
            if cls._matches_piped_pattern(normalized_pattern, normalized_cmd):
                return True, pattern
        return False, ""

    @classmethod
    def _matches_piped_pattern(cls, normalized_pattern: str, normalized_cmd: str) -> bool:
        """处理 `curl | sh` 这类跨管道的规则。

        规则里两段之间通常什么都没有，而真实命令中间还有 URL、参数等内容
        （`curl -fsSL https://x | bash`）。所以对含管道的规则改为按顺序匹配
        各段，而不是要求整串连续出现。
        """
        if '|' not in normalized_pattern:
            return False
        segments = [seg.strip() for seg in normalized_pattern.split('|')]
        if not all(segments):
            return False
        cmd_segments = [seg.strip() for seg in normalized_cmd.split('|')]
        if len(cmd_segments) < len(segments):
            return False
        # 在命令的管道分段里按顺序找齐规则的每一段。
        search_from = 0
        for index, segment in enumerate(segments):
            for cmd_index in range(search_from, len(cmd_segments)):
                cmd_segment = cmd_segments[cmd_index]
                # 规则的第一段允许作为前缀出现（curl 后面可以跟参数和 URL），
                # 后续段要求整段就是它，避免 `| shasum` 命中 `| sh`。
                if index == 0:
                    hit = cmd_segment == segment or cmd_segment.startswith(segment + ' ')
                else:
                    hit = cmd_segment == segment
                if hit:
                    search_from = cmd_index + 1
                    break
            else:
                return False
        return True


AgentCommandBlacklist.init()

# 向后兼容的默认值引用（实际内容从文件加载）
def get_default_prompt():
    return PromptFileManager.get('assistant_prompt')

def get_default_global_addon():
    return PromptFileManager.get('global_prompt_addon')

def get_default_agent_addon():
    return PromptFileManager.get('agent_prompt_addon')

# --- ☆ 异步 SQLite 数据库管理（优化版）☆ ---
