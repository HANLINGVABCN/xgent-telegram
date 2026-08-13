"""Telegram delivery boundary for the Agent ``sendfile`` protocol."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
import urllib.parse
from typing import Any, Awaitable, Callable


async def _prepare_local_api_file(
    source_path: str,
    host_data_dir: str,
    unique_name: str,
    *,
    logger: Any,
) -> str:
    """Expose a large file to the local Bot API without blocking the loop."""
    temp_host_path = os.path.join(host_data_dir, unique_name)

    def _prepare() -> None:
        os.makedirs(host_data_dir, exist_ok=True)
        try:
            if os.path.exists(temp_host_path):
                os.remove(temp_host_path)
            os.link(source_path, temp_host_path)
        except OSError as link_error:
            logger.info(
                "sendfile 硬链接失败(%s)，降级复制: %s",
                type(link_error).__name__,
                temp_host_path,
            )
            shutil.copy2(source_path, temp_host_path)

    await asyncio.to_thread(_prepare)
    return temp_host_path


async def _remove_file(path: str, *, logger: Any) -> None:
    def _remove() -> None:
        if os.path.exists(path):
            os.remove(path)

    try:
        await asyncio.to_thread(_remove)
    except OSError as error:
        logger.warning("清理 sendfile 临时文件失败: %s (%s)", path, error)


async def execute_sendfile_protocol(
    requested_path: str,
    *,
    executor: Any,
    context: Any,
    chat_id: Any,
    api_base_url: str,
    local_api_host_data_dir: str,
    local_api_container_data_dir: str,
    max_file_size: int,
    safe_send_message: Callable[..., Awaitable[Any]],
    safe_text: Callable[[Any], str],
    logger: Any,
    cancel_task_quietly: Callable[..., Awaitable[Any]],
) -> str:
    """Send one server-side file and return the legacy history notice."""
    sendfile_notice = ""
    try:
        resolved_path = executor.resolve_file_path(requested_path)
        if not os.path.exists(resolved_path):
            sendfile_notice = f"[sendfile结果] 发送失败: 文件不存在: {resolved_path}"
            await safe_send_message(
                context,
                chat_id,
                f"❌ 文件不存在: {safe_text(resolved_path)}",
            )
            return sendfile_notice

        file_size = os.path.getsize(resolved_path)
        filename = os.path.basename(resolved_path)

        if file_size > max_file_size and api_base_url:
            # 目录级隔离替代文件名篡改：每次发送生成独立随机子目录，原名文件存入
            # 该目录。物理层面规避并发同名冲突，且传输给 Telegram 的文件名保持原样，
            # 不再强制拼接 _sendfile_ + UUID 导致接收端文件名被篡改。
            unique_dir = f"sendfile_{uuid.uuid4().hex[:8]}"
            temp_host_dir = os.path.join(local_api_host_data_dir, unique_dir)
            temp_host_path = os.path.join(temp_host_dir, filename)
            os.makedirs(temp_host_dir, exist_ok=True)
            try:
                await _prepare_local_api_file(
                    resolved_path,
                    temp_host_dir,
                    filename,
                    logger=logger,
                )
                # filename 进入 file:// URL 需编码（中文/空格等特殊字符会让本地
                # Bot API server 解析路径出错）。目录内文件名保持原样。
                encoded_filename = urllib.parse.quote(filename)
                container_file_path = (
                    f"file://{local_api_container_data_dir}/{unique_dir}/{encoded_filename}"
                )
                keep_alive = {"on": True}

                async def _upload_indicator() -> None:
                    while keep_alive["on"]:
                        try:
                            await context.bot.send_chat_action(
                                chat_id=chat_id,
                                action="upload_document",
                            )
                        except Exception:
                            pass
                        await asyncio.sleep(4)

                indicator_task = asyncio.create_task(_upload_indicator())
                try:
                    read_timeout = max(
                        120,
                        min(1800, int(file_size / (50 * 1024 * 1024) * 60)),
                    )
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=container_file_path,
                        filename=filename,
                        caption=f"📄 {filename} ({file_size} bytes) [本地API直发]",
                        read_timeout=read_timeout,
                        write_timeout=read_timeout,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
                    sendfile_notice = (
                        f"[sendfile结果] 已发送服务器文件给用户: "
                        f"{resolved_path} ({file_size} bytes) [本地API直发]"
                    )
                finally:
                    keep_alive["on"] = False
                    await cancel_task_quietly(indicator_task, timeout=0.2)
            finally:
                # 级联清理：删除整个隔离目录（含原名文件），空间零残留。
                # 用 to_thread 避免大文件场景同步删除阻塞事件循环。
                try:
                    await asyncio.to_thread(shutil.rmtree, temp_host_dir, True)
                except OSError:
                    logger.warning("清理 sendfile 隔离目录失败: %s", temp_host_dir)
            return sendfile_notice

        if file_size > max_file_size:
            sendfile_notice = (
                f"[sendfile结果] 发送失败: 文件超过50MB限制({file_size} bytes)，"
                "且未启用本地 API，无法发送。"
            )
            await safe_send_message(
                context,
                chat_id,
                (
                    f"❌ 文件超过50MB限制({file_size} bytes)，官方 API 无法发送。\n"
                    "如需发送大文件，请通过 install.sh 菜单选项 8 启用本地 API 容器。\n"
                    f"路径: {safe_text(resolved_path)}"
                ),
            )
            return sendfile_notice

        try:
            await context.bot.send_chat_action(
                chat_id=chat_id,
                action="upload_document",
            )
        except Exception:
            pass
        with open(resolved_path, "rb") as sendfile:
            await context.bot.send_document(
                chat_id=chat_id,
                document=sendfile,
                filename=filename,
                caption=f"📄 {filename} ({file_size} bytes)",
                read_timeout=120,
                write_timeout=120,
            )
        sendfile_notice = (
            f"[sendfile结果] 已发送服务器文件给用户: "
            f"{resolved_path} ({file_size} bytes)"
        )
    except Exception as error:
        logger.error("Agent发送服务器文件失败: %s", error)
        sendfile_notice = (
            f"[sendfile结果] 发送失败: {requested_path}。错误: {str(error)[:200]}"
        )
        await safe_send_message(
            context,
            chat_id,
            f"❌ 发送文件失败: {safe_text(str(error)[:200])}",
        )
    return sendfile_notice
