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
import threading
import contextlib
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
from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update, constants, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
UPDATE_LOCAL_CUSTOM_DIRS = ("prompts", "skill")
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
    }
    for secret, replacement in secrets.items():
        if secret:
            text = text.replace(secret, replacement)
    return text

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


def write_model_trace(event: str, payload: Dict[str, Any]):
    """Append every full-fidelity action/model event to one chronological JSONL log."""
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            **trace_json_safe(payload),
        }
        line = json.dumps(record, ensure_ascii=False, default=repr)
        with FULL_TRACE_LOCK:
            with open(FULL_TRACE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        logger.debug(f"模型全量日志写入失败: {e}")


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
                                   usage: Optional[Dict[str, int]], elapsed_seconds: float):
    text = build_token_usage_message(usage, elapsed_seconds)
    if not text:
        return
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
    SET_COMMAND_BLACKLIST = 'set_command_blacklist'
    SET_UPDATE_TOKEN = 'set_update_token'
    SET_MEMORY = 'set_memory'
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


def has_pending_text_conversation(update: Update) -> bool:
    key = get_text_conversation_buffer_key(update)
    with _pending_text_conversations_lock:
        return key in _pending_text_conversations


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
            with open(cls.FILE_PATH, 'w', encoding='utf-8') as f:
                f.write(cls.HEADER)
            logger.info("已创建 Agent 命令黑名单文件")
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
            logger.error(f"加载 Agent 命令黑名单失败: {e}")
            cls._patterns = []
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

    @classmethod
    def check(cls, command: str) -> Tuple[bool, str]:
        cmd_lower = command.lower().strip()
        for pattern in cls._patterns:
            if pattern.lower() in cmd_lower:
                return True, pattern
        return False, ""


AgentCommandBlacklist.init()

# 向后兼容的默认值引用（实际内容从文件加载）
def get_default_prompt():
    return PromptFileManager.get('assistant_prompt')

def get_default_global_addon():
    return PromptFileManager.get('global_prompt_addon')

def get_default_agent_addon():
    return PromptFileManager.get('agent_prompt_addon')

# --- ☆ 异步 SQLite 数据库管理（优化版）☆ ---
