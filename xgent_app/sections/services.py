



# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

from xgent_app.shell_output import (
    build_run_notice,
    build_shell_notice,
    format_shell_context_output,
    format_shell_display_output,
    get_shell_pause_messages,
)
from xgent_app.agent_context import (
    build_media_context_message,
    build_media_context_message_async,
)
from xgent_app.agent_search import run_search
from xgent_app import web_auth
from xgent_app.web_bridge import (
    WebOutbox,
    build_web_conversation_objects,
    build_web_mirror_objects,
    build_web_callback_objects,
    build_web_command_objects,
    MirrorBot,
    MirrorMessage,
    install_tg_to_web_mirror,
)
from xgent_app.web_server import WebChatConfig, WebChatServer
# 记录来源标记：写进每条 global_messages 的 metadata.src。
# CLI 与服务端是两个进程、只共享数据库；服务端的网页观察者（idle.py 的
# _web_external_record_watcher）靠它区分"本进程写的（SSE 已直发，跳过）"和
# "别的进程写的（CLI 的对话，要推成帧）"，否则同一句话会在网页上显示两遍。
_RECORDER_SOURCE_ID = f"pid{os.getpid()}-{uuid.uuid4().hex[:6]}"


class GlobalRecorder:
    """始终记录所有操作（无论什么模式）"""

    @staticmethod
    async def record(msg_type: str, role: str, content: str,
                     chat_id: Optional[int] = None, user_id: Optional[int] = None,
                     session_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
        """记录消息到全局表 - 始终记录。

        记录是旁路：数据库抖动、磁盘满不应该中断用户正在进行的对话。
        这里的 91 处调用点全是主流程里的裸 await，异常会直接冒到用户面前。
        """
        stamped_metadata = dict(metadata or {})
        stamped_metadata.setdefault('src', _RECORDER_SOURCE_ID)
        rowid = None
        try:
            db = await BotMemoryDB.get_instance()
            rowid = await db.record_global_message(
                chat_id=chat_id or BotConfig.AUTHORIZED_USER_ID,
                user_id=user_id or 0,
                msg_type=msg_type,
                role=role,
                content=content,
                session_id=session_id or UserDataManager.get('current_chat_id'),
                metadata=stamped_metadata
            )
        except Exception as e:
            logger.error(f"全局消息记录失败（已忽略，不中断主流程）: {e}")

        if msg_type == MessageType.AI_REPLY:
            return rowid
        try:
            write_model_trace("operation", {
                "msg_type": msg_type,
                "role": role,
                "chat_id": chat_id or BotConfig.AUTHORIZED_USER_ID,
                "user_id": user_id or 0,
                "session_id": session_id or UserDataManager.get('current_chat_id'),
                "content": content,
                "metadata": metadata,
            })
        except Exception as e:
            logger.error(f"trace 记录失败（已忽略，不中断主流程）: {e}")
        return rowid

    @staticmethod
    async def record_user_message(content: str, msg_type: str = MessageType.USER_TEXT,
                                   chat_id: Optional[int] = None,
                                   metadata: Optional[Dict[str, Any]] = None):
        """记录用户消息。metadata 可带 origin=cli-chat（CLI 对话文本）：
        服务端跨端观察者据此把这句话镜像到 Telegram，状态机输入不带
        标记、不镜像——与 Telegram 端"配置过程不进聊天流"的语义一致。"""
        await GlobalRecorder.record(
            msg_type=msg_type,
            role='user',
            content=content,
            chat_id=chat_id,
            user_id=BotConfig.AUTHORIZED_USER_ID,
            metadata=metadata
        )
    
    @staticmethod
    async def record_ai_reply(content: str, chat_id: Optional[int] = None,
                              metadata: Optional[Dict[str, Any]] = None):
        """记录AI回复。"""
        await GlobalRecorder.record(
            msg_type=MessageType.AI_REPLY,
            role='assistant',
            content=content,
            chat_id=chat_id,
            metadata=metadata
        )

    @staticmethod
    async def record_token_usage(content: str, chat_id: Optional[int] = None,
                                 usage: Optional[Dict[str, Any]] = None,
                                 model: Optional[str] = None):
        """记录 token 用量提示。须在 record_ai_reply 之后调用，保证 timestamp 晚于正文，
        刷新后顺序为「输出 + tokens」而非反序。usage/model 存进 metadata 供 token 统计。"""
        metadata = None
        if usage or model:
            metadata = {}
            if model:
                metadata['model'] = model
            if usage:
                meta_usage = {}
                for _k in ('input_tokens', 'output_tokens', 'cached_tokens',
                           'reasoning_tokens', 'visible_output_tokens', 'total_tokens'):
                    _v = usage.get(_k)
                    if _v is not None:
                        meta_usage[_k] = _v
                if meta_usage:
                    metadata['usage'] = meta_usage
        rowid = await GlobalRecorder.record(
            msg_type=MessageType.TOKEN_USAGE,
            role='assistant',
            content=content,
            chat_id=chat_id,
            metadata=metadata
        )
        # 双写：global_messages 行只作聊天历史显示；统计以独立表为准，
        # 清空对话记忆不会再丢用量数据。旁路失败只记日志，不中断对话。
        if metadata and (metadata.get('model') or metadata.get('usage')):
            try:
                db = await BotMemoryDB.get_instance()
                await db.add_token_stat(
                    model=metadata.get('model') or '',
                    usage=metadata.get('usage') or {},
                    ts=time.time(),
                    source_rowid=rowid,
                )
            except Exception as e:
                logger.error(f"token 用量统计写入失败（已忽略，不中断主流程）: {e}")

    @staticmethod
    async def record_media_reply(content: str, chat_id: Optional[int] = None):
        """记录外部媒体模块回复，避免和聊天AI混成同一个说话人。"""
        await GlobalRecorder.record(
            msg_type=MessageType.MEDIA_REPLY,
            role='media_module',
            content=content,
            chat_id=chat_id
        )
    
    @staticmethod
    async def record_system_op(operation: str, details: Optional[Dict[str, Any]] = None, chat_id: Optional[int] = None):
        """记录系统操作"""
        await GlobalRecorder.record(
            msg_type=MessageType.SYSTEM_OP,
            role='system',
            content=operation,
            chat_id=chat_id,
            metadata=details
        )

    @staticmethod
    async def record_system_message(content: str, chat_id: Optional[int] = None):
        """记录系统消息到 AI 可见的上下文（操作结果/确认信息）。

        与 record_system_op 的区别：record_system_op 记录操作本身（如"导出全部数据"），
        record_system_message 记录操作结果（如"✅ 已成功导出..."），让 AI 能看到用户已完成
        的操作结果，避免重复询问。
        """
        await GlobalRecorder.record(
            msg_type=MessageType.SYSTEM_OP,
            role='system',
            content=content,
            chat_id=chat_id,
        )
    
    @staticmethod
    async def record_button_click(button_data: str, chat_id: Optional[int] = None):
        """记录按钮点击"""
        await GlobalRecorder.record(
            msg_type=MessageType.BUTTON_CLICK,
            role='user',
            content=f"点击按钮: {button_data}",
            chat_id=chat_id,
            user_id=BotConfig.AUTHORIZED_USER_ID
        )

# --- ☆ 工具函数 ☆ ---
def safe_text(text: Any) -> str:
    return html.escape(str(text)) if text else ""

def should_apply_update_file(rel_path: str, overwrite_local_custom_files: bool = True) -> bool:
    rel_path = rel_path.replace("\\", "/").strip("/")
    if not rel_path or rel_path.startswith("../") or "/../" in rel_path:
        return False

    parts = [part for part in rel_path.split("/") if part]
    if not parts:
        return False
    # 私有 skill 永不覆盖：无论保留/覆盖模式，skill-private/ 下的文件都跳过。
    # 这些是每个部署实例的自定义 skill，更新里也不会含它们，但显式跳过更安全。
    if parts[0] == "skill-private":
        return False
    if not overwrite_local_custom_files and parts[0] in UPDATE_LOCAL_CUSTOM_DIRS:
        return False
    if parts[0] in UPDATE_SKIP_NAMES:
        return False
    if any(part in UPDATE_SKIP_NAMES for part in parts):
        return False
    if any(rel_path.endswith(suffix) for suffix in UPDATE_SKIP_SUFFIXES):
        return False
    return True

def backup_local_custom_dirs() -> Optional[str]:
    existing_dirs = [
        (dir_name, os.path.join(PROJECT_ROOT, dir_name))
        for dir_name in UPDATE_LOCAL_CUSTOM_DIRS
        if os.path.isdir(os.path.join(PROJECT_ROOT, dir_name))
    ]
    if not existing_dirs:
        return None

    os.makedirs(UPDATE_BACKUP_DIR, exist_ok=True)
    backup_dir = os.path.join(
        UPDATE_BACKUP_DIR,
        "custom_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    if os.path.exists(backup_dir):
        backup_dir = f"{backup_dir}_{uuid.uuid4().hex[:6]}"

    os.makedirs(backup_dir, exist_ok=False)
    for dir_name, source_dir in existing_dirs:
        shutil.copytree(source_dir, os.path.join(backup_dir, dir_name))
    return to_display_path(backup_dir)

def update_env_values(values: Dict[str, str]):
    env_path = os.path.join(PROJECT_ROOT, ".env")
    existing_lines: List[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
            existing_lines = f.read().splitlines()

    keys = set(values)
    next_lines = []
    for line in existing_lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            next_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key not in keys:
            next_lines.append(line)

    if next_lines and next_lines[-1].strip():
        next_lines.append("")
    for key, value in values.items():
        clean_value = str(value or "").replace("\r", "").replace("\n", "").strip()
        next_lines.append(f"{key}={clean_value}")

    tmp_path = env_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(next_lines).rstrip() + "\n")
    with contextlib.suppress(Exception):
        os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, env_path)

def set_update_source(update_url: str):
    update_url = str(update_url or "").replace("\r", "").replace("\n", "").strip()
    BotConfig.UPDATE_ZIP_URL = update_url or BotConfig.DEFAULT_UPDATE_ZIP_URL
    os.environ["UPDATE_ZIP_URL"] = BotConfig.UPDATE_ZIP_URL

def is_test_update_source(update_url: str) -> bool:
    return str(update_url or "").strip() == BotConfig.TEST_UPDATE_ZIP_URL

def get_update_source_label(update_url: str) -> str:
    if is_test_update_source(update_url):
        return "test 私有目录"
    if str(update_url or "").strip() == BotConfig.NORMAL_UPDATE_ZIP_URL:
        return "正常 bot 项目"
    return "自定义更新源"

def persist_update_github_token(token: str, update_url: Optional[str] = None):
    token = str(token or "").replace("\r", "").replace("\n", "").strip()
    if not token:
        raise ValueError("GitHub Token 不能为空")
    if update_url:
        set_update_source(update_url)
    BotConfig.UPDATE_GITHUB_TOKEN = token
    os.environ["UPDATE_GITHUB_TOKEN"] = token
    update_env_values({
        "UPDATE_GITHUB_TOKEN": token,
    })

def persist_search_api_key(api_key: str):
    """把 Tavily Key 写入 .env 并立即生效，无需重启。"""
    api_key = str(api_key or "").replace("\r", "").replace("\n", "").strip()
    if not api_key:
        raise ValueError("搜索 API Key 不能为空")
    BotConfig.TAVILY_API_KEY = api_key
    os.environ["TAVILY_API_KEY"] = api_key
    update_env_values({"TAVILY_API_KEY": api_key})


def clear_search_api_key():
    """清除已保存的搜索 Key，search-x 会回到未配置提示。"""
    BotConfig.TAVILY_API_KEY = ""
    os.environ.pop("TAVILY_API_KEY", None)
    update_env_values({"TAVILY_API_KEY": ""})


def mask_search_api_key(api_key: str) -> str:
    """只显示尾部 4 位，避免完整 Key 出现在聊天记录里。"""
    api_key = str(api_key or "").strip()
    if not api_key:
        return "未配置"
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:6]}...{api_key[-4:]}"


def clear_update_github_token():
    """清除已保存的 GitHub Token。"""
    BotConfig.UPDATE_GITHUB_TOKEN = ""
    os.environ.pop("UPDATE_GITHUB_TOKEN", None)
    update_env_values({"UPDATE_GITHUB_TOKEN": ""})


# --- ☆ Web Chat 密码 ☆ ---
# 存 PBKDF2 哈希进 SQLite config，不存 .env 明文：
# xgent_memory.db 在 UPDATE_SKIP_NAMES 里，/update 不会覆盖它。

async def persist_web_password(password: str) -> str:
    """哈希后落库，返回哈希串。"""
    password = str(password or "").strip()
    if not password:
        raise ValueError("密码不能为空")
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    digest = web_auth.hash_password(password)
    await UserDataManager.save_config(WEB_PASSWORD_CONFIG_KEY, digest)
    UserDataManager.set('_web_has_password', True)
    return digest


async def read_web_password_hash() -> str:
    """按需读库。哈希不进 UserDataManager 内存快照，避免被顺手打进日志。"""
    db = await BotMemoryDB.get_instance()
    return str(await db.get_config(WEB_PASSWORD_CONFIG_KEY, '') or '')


async def clear_web_password() -> None:
    await UserDataManager.save_config(WEB_PASSWORD_CONFIG_KEY, '')
    UserDataManager.set('_web_has_password', False)


def mask_web_password(password_hash: str) -> str:
    return web_auth.mask_password_hash(password_hash)


def mask_update_github_token(token: str) -> str:
    """只显示首尾，避免完整 Token 出现在聊天记录里。"""
    token = str(token or "").strip()
    if not token:
        return "未配置"
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:10]}...{token[-4:]}"


def _github_api_status(url: str, token: str) -> int:
    """对 GitHub API 发一次 GET，只返回状态码。"""
    request = urllib.request.Request(url, headers={
        "User-Agent": "xgent-telegram-updater",
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(response.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def verify_update_github_token(token: str, update_url: str) -> Dict[str, Any]:
    """分两步验证 Token，区分「Token 本身无效」和「没授权这个仓库」。

    GitHub 对无权限的私有仓库返回 404 而不是 403（避免泄露仓库存在），
    所以必须先验证身份再验证仓库，否则无法给出可操作的提示。
    """
    token = str(token or "").strip()
    if not token:
        return {"ok": False, "reason": "empty", "message": "Token 为空。"}

    if "\\" in token:
        return {
            "ok": False,
            "reason": "escaped",
            "message": (
                "Token 里含有反斜杠 <code>\\</code>，说明被转义过。\n"
                "请重新发送一次原始 Token。"
            ),
        }

    try:
        user_status = _github_api_status("https://api.github.com/user", token)
    except Exception as e:
        return {
            "ok": False,
            "reason": "network",
            "message": f"无法连接 GitHub：{str(e)[:150]}",
        }

    if user_status == 401:
        return {
            "ok": False,
            "reason": "invalid_token",
            "message": (
                "Token 无效或已过期（GitHub 返回 401）。\n\n"
                "请到 GitHub → Settings → Developer settings → "
                "Personal access tokens 重新生成，注意复制完整、不要漏字符。"
            ),
        }
    if user_status != 200:
        return {
            "ok": False,
            "reason": "unexpected",
            "message": f"验证身份时 GitHub 返回 HTTP {user_status}。",
        }

    repo_api = github_repo_api_url(update_url)
    if not repo_api:
        return {"ok": True, "reason": "user_only", "message": "Token 有效。"}

    try:
        repo_status = _github_api_status(repo_api, token)
    except Exception as e:
        return {
            "ok": False,
            "reason": "network",
            "message": f"无法连接 GitHub：{str(e)[:150]}",
        }

    if repo_status == 200:
        return {"ok": True, "reason": "ok", "message": "Token 有效，且可以访问该仓库。"}

    if repo_status in (403, 404):
        return {
            "ok": False,
            "reason": "no_repo_access",
            "message": (
                "Token 本身有效，但<b>访问不到这个仓库</b>。\n\n"
                "生成 Fine-grained Token 时必须同时满足：\n"
                "1️⃣ <b>Repository access</b> → 选 <code>Only select repositories</code> "
                "→ 勾上目标仓库\n"
                "2️⃣ <b>Permissions</b> → <code>Repository permissions</code> → "
                "<code>Contents</code> → 设为 <b>Read-only</b>\n\n"
                "第 2 条默认是 No access，最容易漏掉。"
            ),
        }

    return {
        "ok": False,
        "reason": "unexpected",
        "message": f"访问仓库时 GitHub 返回 HTTP {repo_status}。",
    }


def github_repo_api_url(update_url: str) -> str:
    """从 zipball 地址推出仓库 API 地址，用于权限校验。"""
    match = re.match(
        r"https://api\.github\.com/repos/([^/]+)/([^/]+)/zipball",
        str(update_url or "").strip(),
    )
    if not match:
        return ""
    return f"https://api.github.com/repos/{match.group(1)}/{match.group(2)}"


def should_send_github_update_token(update_url: str) -> bool:
    host = (urllib.parse.urlparse(update_url).hostname or "").lower()
    return host == "github.com" or host == "api.github.com" or host.endswith(".github.com")

def build_update_download_request() -> urllib.request.Request:
    headers = {
        "User-Agent": "xgent-telegram-updater",
        "Accept": "application/vnd.github+json",
    }
    if BotConfig.UPDATE_GITHUB_TOKEN and should_send_github_update_token(BotConfig.UPDATE_ZIP_URL):
        headers["Authorization"] = f"Bearer {BotConfig.UPDATE_GITHUB_TOKEN}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return urllib.request.Request(BotConfig.UPDATE_ZIP_URL, headers=headers)

def format_update_download_error(e: urllib.error.HTTPError) -> str:
    if e.code in {401, 403, 404}:
        auth_hint = (
            "已读取 UPDATE_GITHUB_TOKEN，但访问被拒绝。"
            "多半是 Token 没授权这个仓库：生成 Fine-grained Token 时要选 "
            "Only select repositories 勾上目标仓库，并把 Contents 设为 Read-only。"
            "可在「更多设置 → 凭据配置 → GitHub Token → 验证 Token」查看具体原因。"
            if BotConfig.UPDATE_GITHUB_TOKEN
            else "如果仓库是私有仓库，请在「更多设置 → 凭据配置 → GitHub Token」设置 Token。"
        )
        return f"更新源访问失败（HTTP {e.code}）。{auth_hint}"
    return f"下载更新包失败（HTTP {e.code}）。"

def download_and_apply_project_update(overwrite_local_custom_files: bool = True) -> Dict[str, Any]:
    """Download the configured zipball and overwrite tracked project files in-place."""
    copied_files: List[str] = []
    skipped_local_custom_files = 0
    backup_path = backup_local_custom_dirs() if overwrite_local_custom_files else None

    with tempfile.TemporaryDirectory(prefix="xgent-telegram-update-") as tmp_dir:
        zip_path = os.path.join(tmp_dir, "source.zip")
        request = build_update_download_request()

        # 下载时限制最大体积，防止 zip 炸弹
        MAX_UPDATE_DOWNLOAD_SIZE = 200 * 1024 * 1024  # 200 MB
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                total_downloaded = 0
                with open(zip_path, "wb") as out_file:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total_downloaded += len(chunk)
                        if total_downloaded > MAX_UPDATE_DOWNLOAD_SIZE:
                            raise RuntimeError("更新包体积超出限制（>200MB），可能为异常文件，已中止下载。")
                        out_file.write(chunk)
        except urllib.error.HTTPError as e:
            raise RuntimeError(format_update_download_error(e)) from e

        # 解压时校验总解压大小，防止 zip 炸弹
        MAX_UPDATE_DECOMPRESSED_SIZE = 500 * 1024 * 1024  # 500 MB
        total_decompressed = 0

        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue

                # 单文件大小检查
                if member.file_size > 100 * 1024 * 1024:  # 单文件 > 100MB
                    continue

                member_name = member.filename.replace("\\", "/")
                parts = [part for part in member_name.split("/") if part]
                if len(parts) < 2:
                    continue

                rel_path = "/".join(parts[1:])
                if rel_path.split("/", 1)[0] in UPDATE_LOCAL_CUSTOM_DIRS and not overwrite_local_custom_files:
                    skipped_local_custom_files += 1
                    continue
                if not should_apply_update_file(
                    rel_path,
                    overwrite_local_custom_files=overwrite_local_custom_files
                ):
                    continue

                target_path = os.path.abspath(os.path.join(PROJECT_ROOT, *rel_path.split("/")))
                if not target_path.startswith(PROJECT_ROOT + os.sep):
                    continue

                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with archive.open(member) as src, open(target_path, "wb") as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        total_decompressed += len(chunk)
                        if total_decompressed > MAX_UPDATE_DECOMPRESSED_SIZE:
                            raise RuntimeError("更新包解压后体积超出限制（>500MB），可能为 zip 炸弹，已中止。")
                        dst.write(chunk)

                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    with contextlib.suppress(Exception):
                        os.chmod(target_path, mode)
                elif rel_path.endswith(".sh"):
                    with contextlib.suppress(Exception):
                        os.chmod(target_path, os.stat(target_path).st_mode | 0o755)

                copied_files.append(rel_path)

    return {
        "source": BotConfig.UPDATE_ZIP_URL,
        "count": len(copied_files),
        "files": copied_files[:30],
        "truncated": len(copied_files) > 30,
        "overwrite_local_custom_files": overwrite_local_custom_files,
        "backup_path": backup_path,
        "skipped_local_custom_files": skipped_local_custom_files,
    }

def to_display_path(path: str) -> str:
    return os.path.abspath(path).replace('\\', '/')

def get_runtime_prompt(key: str) -> str:
    value = UserDataManager.get(key)
    if value:
        return value
    return PromptFileManager.get(key)

def format_prompt_template(key: str, **values: Any) -> str:
    content = PromptFileManager.get(key)
    for name, value in values.items():
        content = content.replace("{" + name + "}", str(value))
    return content

def get_unauthorized_reply_messages() -> List[str]:
    content = PromptFileManager.get('unauthorized_reply_messages')
    return [part.strip() for part in content.split('\n---\n') if part.strip()]

async def save_runtime_prompt(key: str, content: str):
    UserDataManager.set(key, content)
    await UserDataManager.save_config(key, content)
    PromptFileManager.set(key, content)

async def reload_runtime_prompt(key: str) -> str:
    content = PromptFileManager.get(key)
    UserDataManager.set(key, content)
    await UserDataManager.save_config(key, content)
    return content

async def reload_overwritten_custom_prompts() -> Dict[str, int]:
    """覆盖更新后，从新 prompts/ 文件重载并同步运行时提示词。"""
    PromptFileManager.reload_all()

    synced_runtime_prompts = 0
    for key in ('assistant_prompt', 'global_prompt_addon'):
        await reload_runtime_prompt(key)
        synced_runtime_prompts += 1

    AgentCommandBlacklist.reload()
    return {
        'prompt_files': len(PromptFileManager.FILES),
        'runtime_prompts': synced_runtime_prompts,
        'command_blacklist_patterns': len(AgentCommandBlacklist.get_patterns()),
    }

def is_prompt_edit_state(state: Any) -> bool:
    return state in [BotState.SET_PROMPT, BotState.SET_GLOBAL_PROMPT, BotState.SET_ANY_PROMPT]

def get_editing_prompt_key(state: Any) -> str:
    if state == BotState.SET_PROMPT:
        return 'assistant_prompt'
    if state == BotState.SET_GLOBAL_PROMPT:
        return 'global_prompt_addon'
    return UserDataManager.get('editing_prompt_key', 'assistant_prompt')

MODEL_TARGETS = {
    'chat': {
        'label': '对话模型',
        'provider_state_key': 'active_provider_key',
        'provider_config_key': 'active_provider',
        'model_state_key': 'default_model',
        'model_config_key': 'default_model',
    },
    'media': {
        'label': '媒体模型',
        'provider_state_key': 'default_media_provider_key',
        'provider_config_key': 'default_media_provider',
        'model_state_key': 'default_media_model',
        'model_config_key': 'default_media_model',
    },
}

MEDIA_CONTEXT_MAX_BYTES = 8 * 1024 * 1024


def get_model_target_meta(target: str) -> Dict[str, str]:
    return MODEL_TARGETS.get(target, MODEL_TARGETS['chat'])


def get_model_target_label(target: str) -> str:
    return get_model_target_meta(target)['label']


def get_model_target_provider_name(target: str) -> Optional[str]:
    meta = get_model_target_meta(target)
    return UserDataManager.get(meta['provider_state_key'])


def get_model_target_name(target: str) -> Optional[str]:
    meta = get_model_target_meta(target)
    return UserDataManager.get(meta['model_state_key'])


def get_model_target_provider(target: str) -> Tuple[Optional[str], Optional[Dict]]:
    providers = UserDataManager.get('providers', {})
    provider_name = get_model_target_provider_name(target)
    if provider_name and provider_name in providers:
        return provider_name, providers[provider_name]
    return None, None


def format_model_target_summary(target: str) -> str:
    provider_name = get_model_target_provider_name(target)
    model_name = get_model_target_name(target)
    if not provider_name or not model_name:
        return "未设置"
    return f"{provider_name} / {model_name}"


async def save_model_target_selection(target: str, provider_name: str, model_name: str):
    meta = get_model_target_meta(target)
    UserDataManager.set(meta['provider_state_key'], provider_name)
    UserDataManager.set(meta['model_state_key'], model_name)
    await UserDataManager.save_config(meta['provider_config_key'], provider_name)
    await UserDataManager.save_config(meta['model_config_key'], model_name)


async def sync_chat_session_model(model_name: Optional[str]) -> None:
    """把当前会话的模型绑定同步成新值。target='chat' 变更时的**必需**配套动作。

    模型取值是两层存储：发消息时优先读 chat_sessions.model，会话没绑定才
    回退全局 default_model（messages.py 的取值顺序）。手动切模型的三条按钮
    路径都做了"全局+会话"双写；历史上导入配置和 Web 设置两条路径只写了
    全局层——前台显示的是新模型，实际请求却拿着会话里残留的旧模型名、
    配上换掉后的新提供商通道发出去，上游直接 502 unknown provider。
    收进一个函数，新路径不再容易漏。

    UPDATE 对不存在的会话行是 no-op，早于首次对话调用也安全。
    """
    db = await BotMemoryDB.get_instance()
    cid = UserDataManager.get('current_chat_id') or SINGLE_MEMORY_SESSION_ID
    await db.update_session(cid, model=model_name)


def resolve_effective_chat_model(
        session_model: Optional[str], default_model: Optional[str],
        provider_data: Optional[Dict]) -> Optional[str]:
    """按发消息的取值顺序解析实际模型，并对悬空绑定做防御性回退。

    取值顺序（messages.py 的契约）：会话绑定 chat_sessions.model 优先，
    没有才回退全局 default_model。两层都可能因配置变更（导入/换线）残留
    旧模型名，模型不在当前提供商列表时：

    - 全局默认仍有效 → 回退全局默认（与前台显示一致），记 warning；
    - 全局默认也失效 → 返回 None。调用方按"未配置模型"提示用户重选，
      绝不能拿一个必然 502 的旧模型名发出去——那只会把错误藏进上游
      日志，用户看到的是一段莫名其妙的失败。

    models 为空的提供商（通配转发型）不做此校验，原样返回。
    纯函数（不读全局状态），便于直接单测。
    """
    model = session_model or default_model
    available_models = (provider_data or {}).get('models') or []
    if available_models and model and model not in available_models:
        if default_model in available_models:
            logger.warning(
                "会话绑定模型 %s 不在当前提供商的列表里，回退默认模型 %s",
                model, default_model,
            )
            return default_model
        logger.warning(
            "会话绑定模型 %s 与默认模型 %s 均不在当前提供商的列表里，按未配置模型处理",
            model, default_model,
        )
        return None
    return model


def classify_provider_mode(api_format: str, base_url: str) -> str:
    normalized_format = (api_format or 'openai').lower()
    normalized_url = (base_url or '').lower()

    if normalized_format == 'vertex':
        return 'vertex'
    if normalized_format == 'gemini':
        if 'aiplatform.googleapis.com' in normalized_url:
            return 'vertex'
        return 'gemini'
    if normalized_format == 'claude':
        return 'claude'
    if normalized_format == 'openai_compatible':
        if 'generativelanguage.googleapis.com' in normalized_url and '/openai' in normalized_url:
            return 'gemini_openai_compatible'
        return 'openai_compatible'
    if 'generativelanguage.googleapis.com' in normalized_url and '/openai' in normalized_url:
        return 'gemini_openai_compatible'
    if 'api.openai.com' in normalized_url:
        return 'openai'
    return 'openai_compatible'


def get_provider_mode_label(api_format: str, base_url: str) -> str:
    profile = classify_provider_mode(api_format, base_url)
    labels = {
        'gemini': 'Gemini 原生 (Google AI Studio)',
        'vertex': 'Vertex 原生 (Google Cloud)',
        'openai': 'OpenAI 官方',
        'openai_compatible': 'OpenAI 兼容',
        'gemini_openai_compatible': 'Gemini OpenAI兼容',
        'claude': 'Claude 原生',
    }
    return labels.get(profile, api_format or 'openai')


def get_provider_request_hint(api_format: str, base_url: str) -> str:
    profile = classify_provider_mode(api_format, base_url)
    hints = {
        'gemini': '.../models/模型名:streamGenerateContent',
        'vertex': '.../models/模型名:streamGenerateContent',
        'openai': '.../chat/completions',
        'openai_compatible': '.../chat/completions',
        'gemini_openai_compatible': '.../chat/completions',
        'claude': '.../messages',
    }
    return hints.get(profile, '.../chat/completions')


def get_provider_platform_hint(api_format: str, base_url: str) -> str:
    profile = classify_provider_mode(api_format, base_url)
    hints = {
        'gemini': 'Google AI Studio 的 Gemini 原生接口',
        'vertex': 'Google Cloud / Vertex AI 的 Gemini 原生接口',
        'openai': 'OpenAI 官方接口',
        'openai_compatible': '兼容 OpenAI 格式的第三方接口',
        'gemini_openai_compatible': 'Google AI Studio 提供的 OpenAI 兼容接口',
        'claude': 'Anthropic Claude Messages 接口',
    }
    return hints.get(profile, '兼容 OpenAI 格式的接口')


def get_provider_key_hint(api_format: str, base_url: str) -> str:
    profile = classify_provider_mode(api_format, base_url)
    hints = {
        'gemini': 'Google AI Studio API Key: https://aistudio.google.com/apikey',
        'vertex': 'Vertex AI Express Mode API Key: Google Cloud Console > APIs & Services > Credentials',
        'openai': 'OpenAI API Key: https://platform.openai.com/api-keys',
        'openai_compatible': '请填写该兼容接口对应的 API Key',
        'gemini_openai_compatible': 'Google AI Studio API Key: https://aistudio.google.com/apikey',
        'claude': 'Anthropic API Key: https://console.anthropic.com/settings/keys',
    }
    return hints.get(profile, '请填写该接口对应的 API Key')


def get_provider_usage_badges(provider_name: str) -> str:
    badges = []
    if provider_name == get_model_target_provider_name('chat'):
        badges.append('💬')
    if provider_name == get_model_target_provider_name('media'):
        badges.append('🖼️')
    return ''.join(badges) or '⚪'

SKILL_PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'skill-public')
SKILL_PRIVATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'skill-private')
# 兼容：旧部署可能还有 skill/ 目录，一并扫描避免 skill 丢失。
SKILL_LEGACY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'skill')
SKILL_FILE_EXTENSIONS = {'.md', '.markdown', '.txt'}
SKILL_SUMMARY_BLOCK_TAG = '!'
SINGLE_MEMORY_SESSION_ID = "global_memory"
SINGLE_MEMORY_SESSION_NAME = "全局记忆"

def _skill_dirs() -> list:
    """返回所有要扫描的 skill 目录：(目录路径, 前缀) 对。

    公有目录前缀为空（路径如 bot-system.md），私有目录前缀为 'private/'
    （路径如 private/my-skill.md），旧 skill/ 目录前缀为空（兼容历史部署）。
    """
    dirs = []
    for d in (SKILL_PUBLIC_DIR, SKILL_LEGACY_DIR):
        if os.path.isdir(d):
            dirs.append((d, ''))
    if os.path.isdir(SKILL_PRIVATE_DIR):
        dirs.append((SKILL_PRIVATE_DIR, 'private/'))
    return dirs

MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'memory')
MEMORY_FILE_PREFIX = 'memory_'
MEMORY_FILE_SUFFIX = '.txt'

def list_skill_files() -> List[str]:
    skill_files = []
    for skill_dir, prefix in _skill_dirs():
        for root, _, filenames in os.walk(skill_dir):
            for filename in filenames:
                if filename.startswith('.'):
                    continue

                ext = os.path.splitext(filename)[1].lower()
                if ext not in SKILL_FILE_EXTENSIONS:
                    continue

                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, skill_dir).replace('\\', '/')
                skill_files.append(prefix + rel_path)

    return sorted(skill_files, key=str.lower)

def read_skill_text(path: str) -> str:
    for encoding in ('utf-8', 'gbk'):
        try:
            with open(path, 'r', encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return ""

def extract_skill_summary_blocks(text: str) -> str:
    blocks = []
    fence_len = 0
    capture_summary = False
    body_lines = []

    for line in text.splitlines():
        stripped = line.strip()
        if fence_len:
            if re.match(rf'^`{{{fence_len},}}\s*$', stripped):
                if capture_summary:
                    body = '\n'.join(body_lines).strip()
                    if body:
                        blocks.append(body)
                fence_len = 0
                capture_summary = False
                body_lines = []
            elif capture_summary:
                body_lines.append(line)
            continue

        match = re.match(r'^(?P<fence>`{3,})(?P<info>[^`]*)$', stripped)
        if not match:
            continue

        fence_len = len(match.group('fence'))
        info = match.group('info').strip()
        tag = info.split(None, 1)[0] if info else ''
        capture_summary = tag == SKILL_SUMMARY_BLOCK_TAG
        body_lines = []

    return ' '.join('\n\n'.join(blocks).split())

def resolve_skill_abs_path(rel_path: str) -> str:
    """把带前缀的相对路径解析为绝对路径。

    rel_path 带前缀：private/xxx.md → skill-private/xxx.md，
    其余 → skill-public/（或旧 skill/）。
    """
    if rel_path.startswith("private/"):
        real_rel = rel_path[len("private/"):]
        return os.path.join(SKILL_PRIVATE_DIR, real_rel.replace('/', os.sep))
    for d in (SKILL_PUBLIC_DIR, SKILL_LEGACY_DIR):
        candidate = os.path.join(d, rel_path.replace('/', os.sep))
        if os.path.exists(candidate):
            return candidate
    # 文件不存在时仍返回公有目录拼出来的路径（保持旧行为，read_skill_text 会返回空）
    return os.path.join(SKILL_PUBLIC_DIR, rel_path.replace('/', os.sep))

def extract_skill_summary(rel_path: str) -> str:
    full_path = resolve_skill_abs_path(rel_path)
    raw_text = read_skill_text(full_path)
    if not raw_text:
        return "无简介"

    return extract_skill_summary_blocks(raw_text) or "无简介"

def get_disabled_skills() -> set:
    """返回被禁用的 skill 相对路径集合（黑名单语义）。

    默认全部启用，用户关掉不要的。空集合=全开。配置存 UserDataManager 的
    disabled_skills（JSON list），与 agent_mode/stream_mode 同路径。
    """
    raw = UserDataManager.get('disabled_skills', [])
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw}


def build_skill_prompt_section() -> str:
    skill_files = list_skill_files()
    if not skill_files:
        return ''
    disabled = get_disabled_skills()
    skill_entries = ''.join(
        f"- {skill_file}: {extract_skill_summary(skill_file)} (路径: {to_display_path(resolve_skill_abs_path(skill_file))})\n"
        for skill_file in skill_files
        if skill_file not in disabled
    )
    if not skill_entries:
        return ''
    return f"\n\n{skill_entries}"

def build_absolute_path_prompt_section() -> str:
    project_root = to_display_path(os.path.dirname(os.path.abspath(__file__)))
    skill_public_dir = to_display_path(SKILL_PUBLIC_DIR)
    skill_private_dir = to_display_path(SKILL_PRIVATE_DIR)
    upload_dir = to_display_path(os.path.join(project_root, 'xgent_storage', 'uploads'))
    return (
        "\n\n---\n"
        "【当前运行目录绝对路径】\n"
        f"- 项目根目录: {project_root}\n"
        f"- skill 公有目录: {skill_public_dir}\n"
        f"- skill 私有目录: {skill_private_dir}\n"
        f"- 上传目录: {upload_dir}\n"
        "所有协议的路径参数（read、edit、file、grep 的 path:、sendfile 等）必须使用上述绝对路径，禁止使用相对路径。需要新建文件时，在项目根目录下选择合适的子路径。\n"
        "---\n"
    )

def get_agent_runtime_prompt(agent_mode: bool) -> str:
    prompt = PromptFileManager.get('agent_prompt_addon')
    prompt += build_absolute_path_prompt_section()
    prompt += build_skill_prompt_section()
    if not agent_mode:
        prompt += PromptFileManager.get('agent_disabled_addon')
    return prompt


# --- 记忆（memory）文件管理：一条记忆 = 一个文件，按需读取拼进 system prompt ---
def list_memory_files() -> List[str]:
    """列出 memory/ 下所有记忆文件名，按文件名（即时间戳）升序排序。"""
    if not os.path.isdir(MEMORY_DIR):
        return []
    result = []
    for filename in os.listdir(MEMORY_DIR):
        if filename.startswith('.') or not filename.endswith(MEMORY_FILE_SUFFIX):
            continue
        full_path = os.path.join(MEMORY_DIR, filename)
        if not os.path.isfile(full_path):
            continue
        result.append(filename)
    return sorted(result, key=str.lower)


def read_memory_file(filename: str) -> str:
    """读取单条记忆内容，复用 skill 的 utf-8/gbk 回退解码。"""
    safe_name = os.path.basename(filename)
    full_path = os.path.join(MEMORY_DIR, safe_name)
    return read_skill_text(full_path)


def save_memory_file(content: str) -> str:
    """保存一条记忆为新文件（文件名带时间戳+随机后缀，避免重名），返回文件名。"""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    rand_suffix = uuid.uuid4().hex[:6]
    filename = f"{MEMORY_FILE_PREFIX}{timestamp}_{rand_suffix}{MEMORY_FILE_SUFFIX}"
    full_path = os.path.join(MEMORY_DIR, filename)
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return filename


def delete_memory_file(filename: str) -> bool:
    """删除指定记忆文件，成功返回 True。"""
    safe_name = os.path.basename(filename)
    full_path = os.path.join(MEMORY_DIR, safe_name)
    if not os.path.isfile(full_path):
        return False
    try:
        os.remove(full_path)
        return True
    except OSError:
        return False


def clear_all_memory() -> int:
    """清空所有记忆文件，返回删除条数。"""
    files = list_memory_files()
    count = 0
    for filename in files:
        if delete_memory_file(filename):
            count += 1
    return count


def build_memory_prompt_section() -> str:
    """拼接所有记忆到 system prompt。无记忆返回空串，不污染 prompt。"""
    files = list_memory_files()
    if not files:
        return ''
    parts = []
    for filename in files:
        text = read_memory_file(filename)
        text = text.strip()
        if text:
            parts.append(text)
    if not parts:
        return ''
    body = "\n---\n".join(parts)
    return f"\n\n【用户记忆】\n{body}\n"


def build_conversation_system_prompt(agent_mode: bool) -> str:
    """正常聊天与空闲提醒共用的 system prompt。"""
    return (
        get_runtime_prompt('assistant_prompt')
        + get_runtime_prompt('global_prompt_addon')
        + build_memory_prompt_section()
        + get_agent_runtime_prompt(agent_mode)
    )


class ArtifactManager:
    ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'xgent_storage')
    UPLOAD_DIR = os.path.join(ROOT_DIR, 'uploads')
    GENERATED_MEDIA_DIR = os.path.join(ROOT_DIR, 'generated_media')
    MAX_INLINE_TEXT_BYTES = 120 * 1024
    MAX_INLINE_TEXT_CHARS = 12000

    @staticmethod
    def _safe_name(name: str, fallback: str = "artifact") -> str:
        cleaned = os.path.basename(name or fallback).strip()
        if not cleaned:
            cleaned = fallback

        stem, ext = os.path.splitext(cleaned)
        safe_stem = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_' for ch in stem).strip('._')
        safe_ext = ''.join(ch if ch.isalnum() or ch in ('.',) else '' for ch in ext)[:16]

        if not safe_stem:
            safe_stem = fallback
        if not safe_ext and ext:
            safe_ext = ".bin"

        return f"{safe_stem[:48]}{safe_ext[:16]}"

    @classmethod
    def _build_relative_path(cls, category_root: str, original_name: str, fallback_prefix: str) -> Tuple[str, str]:
        now = datetime.now()
        dated_dir = os.path.join(category_root, now.strftime("%Y-%m-%d"))
        safe_name = cls._safe_name(original_name, fallback=fallback_prefix)
        stem, ext = os.path.splitext(safe_name)
        unique_name = f"{now.strftime('%H%M%S')}_{uuid.uuid4().hex[:8]}_{stem}{ext or '.txt'}"
        abs_dir = os.path.join(cls.ROOT_DIR, dated_dir)
        os.makedirs(abs_dir, exist_ok=True)
        abs_path = os.path.join(abs_dir, unique_name)
        display_path = to_display_path(abs_path)
        return abs_path, display_path

    @classmethod
    def save_binary_upload(cls, original_name: str, content: bytes) -> Dict[str, Any]:
        abs_path, rel_path = cls._build_relative_path('uploads', original_name, 'upload')
        with open(abs_path, 'wb') as f:
            f.write(content)

        mime_type, _ = mimetypes.guess_type(original_name or "")
        return {
            'abs_path': abs_path,
            'rel_path': rel_path,
            'mime_type': mime_type or 'application/octet-stream',
            'size': len(content),
        }

    @classmethod
    def save_export(cls, original_name: str, content: bytes) -> Dict[str, Any]:
        """导出产物（/export 的记忆 zip 等）落盘。

        与 uploads/generated_media 分开存放：导出文件是"服务器生成、要长期
        留在磁盘上给用户回取"的产物，路径会写进系统消息让 AI 也看得到，
        不和用户上传的临时附件混在一个目录里。
        """
        abs_path, rel_path = cls._build_relative_path('exports', original_name, 'export')
        with open(abs_path, 'wb') as f:
            f.write(content)

        mime_type, _ = mimetypes.guess_type(original_name or "")
        return {
            'abs_path': abs_path,
            'rel_path': rel_path,
            'mime_type': mime_type or 'application/octet-stream',
            'size': len(content),
        }

    @classmethod
    def save_generated_media(cls, original_name: str, content: bytes, mime_type: str) -> Dict[str, Any]:
        abs_path, rel_path = cls._build_relative_path('generated_media', original_name, 'generated_media')
        with open(abs_path, 'wb') as f:
            f.write(content)

        return {
            'abs_path': abs_path,
            'rel_path': rel_path,
            'mime_type': mime_type or 'application/octet-stream',
            'size': len(content),
        }

    @classmethod
    def get_generated_media_root(cls) -> str:
        os.makedirs(cls.GENERATED_MEDIA_DIR, exist_ok=True)
        return cls.GENERATED_MEDIA_DIR

    @classmethod
    def try_decode_text(cls, content: bytes) -> Optional[str]:
        if len(content) > cls.MAX_INLINE_TEXT_BYTES:
            return None

        for encoding in ('utf-8', 'gbk'):
            try:
                text = content.decode(encoding)
                return text
            except UnicodeDecodeError:
                continue
        return None

    @classmethod
    def clip_inline_text(cls, text: str) -> Tuple[str, bool]:
        if len(text) <= cls.MAX_INLINE_TEXT_CHARS:
            return text, False
        return text[:cls.MAX_INLINE_TEXT_CHARS], True

    @staticmethod
    def shorten_text(text: str, limit: int = 120) -> str:
        compact = ' '.join((text or '').split())
        if len(compact) <= limit:
            return compact
        return compact[:limit].rstrip() + "..."

    @staticmethod
    def build_saved_notice(kind: str, rel_path: str, extra: str = "") -> str:
        base = f"{kind}已保存到 {rel_path}"
        if extra:
            base += f"。{extra}"
        return base

    @staticmethod
    def build_index_message(kind: str, name: str, rel_path: str, note: str = "") -> str:
        message = f"[{kind}] {name}，已保存到 {rel_path}"
        if note:
            message += f"。说明：{note}"
        return message

EXTERNAL_MEDIA_SPEAKER = "外部媒体模块"
MEDIA_GENERATION_TIMEOUT = 180


def build_external_media_prompt(kind: str, prompt: str) -> str:
    return (
        f"你是{EXTERNAL_MEDIA_SPEAKER}，不是当前聊天助手本人。"
        f"你的任务是根据用户提示直接生成{kind}，可以附带简短中文说明。"
        "你和聊天模块使用同一套模型接口；如果模型原生支持媒体输出，请直接输出媒体。"
        "如果需要用文本承载媒体，请返回 markdown data URL 或直接 data URL。"
        "不要只返回提示词，不要要求用户再调用其他工具。\n\n"
        f"请根据下面提示直接生成{kind}。\n\n{prompt}"
    )


async def generate_media_with_provider(provider_name: str, provider_data: Dict[str, Any],
                                       model_name: str, prompt: str,
                                       kind: str = "图片") -> Dict[str, Any]:
    history = [{
        'role': 'user',
        'content': build_external_media_prompt(kind, prompt)
    }]

    try:
        response, error = await asyncio.wait_for(
            ModelClient.think_and_reply(
                provider_name,
                get_next_api_key(provider_name, str(provider_data.get('api_key', ''))),
                str(provider_data.get('base_url', '')),
                model_name,
                "",
                history,
                api_format=provider_data.get('api_format', 'openai')
            ),
            timeout=MEDIA_GENERATION_TIMEOUT
        )
    except asyncio.TimeoutError:
        return {
            'success': False,
            'error': f'{EXTERNAL_MEDIA_SPEAKER}执行超时 ({MEDIA_GENERATION_TIMEOUT}秒)',
        }

    if error:
        return {
            'success': False,
            'error': error,
            'text': response or '',
        }

    if not response:
        return {
            'success': False,
            'error': f'{EXTERNAL_MEDIA_SPEAKER}没有返回内容',
        }

    processed_text, artifacts = extract_inline_generated_media(response, append_notices=False)
    for artifact in artifacts:
        artifact['source'] = 'external_media_module'
        artifact['provider_name'] = provider_name
        artifact['model_name'] = model_name
        artifact['prompt'] = prompt

    result: Dict[str, Any] = {
        'success': bool(artifacts),
        'text': processed_text,
        'raw_response': response,
        'artifacts': artifacts,
    }
    if artifacts:
        first_artifact = artifacts[0]
        result['file_path'] = first_artifact.get('path')
        result['mime_type'] = first_artifact.get('mime_type')
    else:
        result['error'] = f'{EXTERNAL_MEDIA_SPEAKER}没有返回可保存的{kind}内容'

    return result


async def run_default_media_generation(prompt: str) -> Dict[str, Any]:
    provider_name, provider_data = get_model_target_provider('media')
    model_name = get_model_target_name('media')

    if not provider_name or not provider_data or not model_name:
        return {
            'success': False,
            'error': '还没有设置默认媒体模型，请先到【默认模型】里选择媒体模型。',
        }

    result = await generate_media_with_provider(provider_name, provider_data, model_name, prompt, kind="媒体")
    result['provider_name'] = provider_name
    result['model_name'] = model_name
    result['api_format'] = provider_data.get('api_format', 'openai')
    return result


async def send_generated_media_file_to_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                            media_path: str, mime_type: str, caption: Optional[str]):
    caption = fit_media_caption(caption)
    send_as_photo = (
        mime_type in {'image/png', 'image/jpeg', 'image/jpg'} and
        os.path.getsize(media_path) <= 10 * 1024 * 1024
    )
    with open(media_path, 'rb') as f:
        if send_as_photo:
            await context.bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
        else:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=os.path.basename(media_path),
                caption=caption
            )


DATA_MEDIA_MARKDOWN_RE = re.compile(
    r'!\[[^\]]*]\(\s*data:((?:image|video|audio)/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/_=-]+)\s*\)',
    re.IGNORECASE
)
DATA_MEDIA_URL_RE = re.compile(
    r'data:((?:image|video|audio)/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/_=-]+)',
    re.IGNORECASE
)
INLINE_MEDIA_PREFIXES = ('data:image/', 'data:video/', 'data:audio/')


def contains_inline_generated_media(text: str) -> bool:
    lowered = (text or '').lower()
    return any(prefix in lowered for prefix in INLINE_MEDIA_PREFIXES)


def media_kind_from_mime(mime_type: str) -> str:
    if (mime_type or '').startswith('image/'):
        return "图片"
    if (mime_type or '').startswith('video/'):
        return "视频"
    if (mime_type or '').startswith('audio/'):
        return "音频"
    return "媒体"


def build_media_autosave_notice(kind: str, display_path: str) -> str:
    capability_hint = {
        "图片": "无识图能力时请勿read以免报错",
        "视频": "无识视频能力时请勿read以免报错",
        "音频": "无识音频能力时请勿read以免报错",
    }.get(kind, "无识别该媒体能力时请勿read以免报错")
    return f"【系统自动生成：本{kind}已自动存入 {display_path}，需要时请read以返回上下文，{capability_hint}】"


def build_media_autosave_notice_text(artifacts: List[Dict[str, Any]], existing_text: str = "") -> str:
    notices = [
        build_media_autosave_notice(
            str(artifact.get('kind') or media_kind_from_mime(str(artifact.get('mime_type') or ''))),
            to_display_path(str(artifact.get('path') or artifact.get('rel_path') or ''))
        )
        for artifact in artifacts
        if artifact.get('path') or artifact.get('rel_path')
    ]
    if existing_text:
        notices = [notice for notice in notices if notice not in existing_text]
    return "\n".join(notices)


def append_media_autosave_notices(text: str, artifacts: List[Dict[str, Any]]) -> str:
    suffix = build_media_autosave_notice_text(artifacts, text or '')
    if not suffix:
        return text
    base = (text or '').rstrip()
    return f"{base}\n\n{suffix}" if base else suffix


def build_generated_media_reply_text(text: str, artifacts: List[Dict[str, Any]],
                                     fallback: str = "") -> str:
    base = (text or '').strip() or fallback
    return append_media_autosave_notices(base, artifacts)


def append_external_media_notices_to_response(response: str,
                                              artifacts: Optional[List[Dict[str, Any]]] = None) -> str:
    if not artifacts:
        return response
    return build_generated_media_reply_text(response, artifacts)


def has_media_artifacts(artifacts: Optional[List[Dict[str, Any]]]) -> bool:
    return bool(artifacts)


def fit_media_caption(caption: Optional[str], limit: int = 1000) -> Optional[str]:
    if not caption:
        return caption
    text = caption.strip()
    if len(text) <= limit:
        return text

    notice_matches = list(re.finditer(r'【系统自动生成：本(?:图片|视频|音频|媒体).*?】', text))
    if notice_matches:
        notice = notice_matches[-1].group(0)
        if len(notice) + 8 < limit:
            head_limit = limit - len(notice) - 6
            return text[:head_limit].rstrip() + "\n...\n" + notice

    return text[:limit - 3].rstrip() + "..."


def _extension_for_mime(mime_type: str) -> str:
    ext = mimetypes.guess_extension(mime_type or '')
    if ext == '.jpe':
        ext = '.jpg'
    return ext or '.bin'


def _save_inline_generated_media(mime_type: str, data_b64: str) -> Dict[str, Any]:
    compact_b64 = ''.join((data_b64 or '').split())
    padding = '=' * (-len(compact_b64) % 4)
    media_bytes = base64.b64decode(compact_b64 + padding)
    kind = media_kind_from_mime(mime_type)
    filename_prefix = {
        "图片": "assistant_image",
        "视频": "assistant_video",
        "音频": "assistant_audio",
    }.get(kind, "assistant_media")
    saved = ArtifactManager.save_generated_media(
        f"{filename_prefix}{_extension_for_mime(mime_type)}",
        media_bytes,
        mime_type
    )
    return {
        'kind': kind,
        'path': saved['abs_path'],
        'rel_path': saved['rel_path'],
        'mime_type': saved['mime_type'],
        'size': saved['size'],
        'source': 'chat_native_media',
    }


def extract_inline_generated_media(response: str, append_notices: bool = True) -> Tuple[str, List[Dict[str, Any]]]:
    """Save inline data-url media and remove raw media payloads from the text reply."""
    if not response or not contains_inline_generated_media(response):
        return response, []

    artifacts: List[Dict[str, Any]] = []

    def replace_match(match: re.Match) -> str:
        mime_type = match.group(1)
        data_b64 = match.group(2)
        try:
            artifact = _save_inline_generated_media(mime_type, data_b64)
            artifacts.append(artifact)
            return ""
        except Exception as e:
            logger.error(f"保存模型内联媒体失败: {e}")
            return "[模型返回了内联图片数据，但保存失败；原始base64已阻止直发以避免刷屏]"

    processed = DATA_MEDIA_MARKDOWN_RE.sub(replace_match, response)
    processed = DATA_MEDIA_URL_RE.sub(replace_match, processed)
    processed = re.sub(r'\n{3,}', '\n\n', processed).strip()
    if append_notices:
        processed = build_generated_media_reply_text(processed, artifacts)
    return processed.strip(), artifacts


async def send_generated_media_artifacts(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                         artifacts: List[Dict[str, Any]],
                                         caption: Optional[str] = None):
    for artifact in artifacts:
        path = str(artifact.get('path') or '')
        mime_type = str(artifact.get('mime_type') or 'application/octet-stream')
        if not path or not os.path.exists(path):
            continue
        media_caption = fit_media_caption(caption)
        if mime_type.startswith('image/'):
            await send_generated_media_file_to_user(context, chat_id, path, mime_type, media_caption)
        else:
            with open(path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=os.path.basename(path),
                    caption=media_caption
                )


def build_media_artifact(kind: str, path: str, mime_type: str,
                         source: str, provider_name: str = "", model_name: str = "",
                         prompt: str = "") -> Dict[str, Any]:
    return {
        'kind': kind or media_kind_from_mime(mime_type),
        'path': path,
        'mime_type': mime_type or 'application/octet-stream',
        'source': source,
        'provider_name': provider_name,
        'model_name': model_name,
        'prompt': prompt,
    }


def build_media_reply_text(speaker: str, body: str, artifacts: List[Dict[str, Any]]) -> str:
    speaker_line = f"说话人: {speaker}" if speaker else ""
    text = "\n".join(part for part in (speaker_line, (body or '').strip()) if part)
    return build_generated_media_reply_text(text, artifacts)


def build_external_media_output(result: Dict[str, Any], prompt: str) -> Tuple[str, List[Dict[str, Any]]]:
    provider_name = str(result.get('provider_name') or '未设置')
    model_name = str(result.get('model_name') or '未设置')
    if result.get('success'):
        raw_artifacts = result.get('artifacts')
        artifacts = [
            dict(artifact)
            for artifact in raw_artifacts
            if isinstance(artifact, dict)
        ] if isinstance(raw_artifacts, list) else []
        media_path = str(result.get('file_path') or '')
        mime_type = str(result.get('mime_type') or 'image/png')
        if not artifacts and media_path:
            artifacts = [
                build_media_artifact(
                    media_kind_from_mime(mime_type),
                    media_path,
                    mime_type,
                    source="external_media_module",
                    provider_name=provider_name,
                    model_name=model_name,
                    prompt=prompt
                )
            ]
        for artifact in artifacts:
            artifact.setdefault('kind', media_kind_from_mime(str(artifact.get('mime_type') or 'image/png')))
            artifact.setdefault('source', 'external_media_module')
            artifact.setdefault('provider_name', provider_name)
            artifact.setdefault('model_name', model_name)
            artifact.setdefault('prompt', prompt)
        module_text = str(result.get('text') or '').strip()
        return build_generated_media_reply_text(module_text, artifacts, fallback="已生成媒体"), artifacts

    error_text = result.get('error') or '未知错误'
    module_text = str(result.get('text') or '').strip()
    module_reply_text = f"\n媒体模块回复:\n{module_text}" if module_text else ""
    body = (
        f"状态: 媒体生成失败\n"
        f"提供商: {provider_name}\n"
        f"模型: {model_name}\n"
        f"原始提示词: {prompt}\n"
        f"错误: {error_text}"
        f"{module_reply_text}"
    )
    return build_media_reply_text(EXTERNAL_MEDIA_SPEAKER, body, []), []


def build_media_result_notice(result: Dict[str, Any], prompt: str) -> str:
    text, _ = build_external_media_output(result, prompt)
    return text


async def build_media_continuation_message(result: Dict[str, Any], prompt: str) -> Dict[str, Any]:
    """Compatibility wrapper; media context construction lives in agent_context.

    异步版本：读取并 base64 编码最多 8MB 的媒体本体会阻塞事件循环，
    而这条路径持有全局对话锁。
    """
    notice = build_media_result_notice(result, prompt)
    return await build_media_context_message_async(
        result,
        notice,
        max_inline_bytes=MEDIA_CONTEXT_MAX_BYTES,
    )


def get_current_provider() -> Tuple[Optional[str], Optional[Dict]]:
    providers = UserDataManager.get('providers', {})
    key = UserDataManager.get('active_provider_key')
    if key and key in providers:
        return key, providers[key]
    return None, None

async def get_or_create_chat_session() -> Tuple[str, Dict]:
    db = await BotMemoryDB.get_instance()
    cid = SINGLE_MEMORY_SESSION_ID
    session = await db.get_session(cid)

    if not session:
        await db.create_session(cid, UserDataManager.get('default_model'))
        await db.update_session(cid, name=SINGLE_MEMORY_SESSION_NAME)
        session = await db.get_session(cid)

    UserDataManager.set('current_chat_id', cid)
    await UserDataManager.save_config('current_chat_id', cid)

    messages = await db.get_chat_messages(cid)
    return cid, {
        'name': session['name'] if session else SINGLE_MEMORY_SESSION_NAME,
        'model': (session['model'] if session else None) or UserDataManager.get('default_model'),
        'last_active': session['last_active'] if session else time.time(),
        'history': messages
    }

def format_chat_name(cid: str, chat_data: dict) -> str:
    name = chat_data.get('name')
    if name:
        return name[:30]
    ts = chat_data.get('last_active', 0)
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts > 0 else cid

def pretty_model_name(name: str) -> str:
    return name

def parse_manual_model_names(text: str) -> List[str]:
    """Parse one or more manually entered model ids separated by English commas."""
    names: List[str] = []
    seen = set()
    for raw_name in text.split(','):
        name = raw_name.strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names

def short_hash(s: str) -> str:
    """生成短哈希，用于callback_data"""
    return hashlib.md5(s.encode()).hexdigest()[:8]

# --- ☆ Callback Data 管理（解决64字节限制）☆ ---
