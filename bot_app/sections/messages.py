# This file is executed by bot_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

from bot_app.agent_coordinator import plan_agent_round_transition
from bot_app.agent_context import (
    build_file_context_message,
    build_sendfile_context_message,
    build_shell_context_message,
    build_trigger_context_message,
)
from bot_app.agent_history import (
    persist_agent_result,
    persist_media_result,
    persist_standard_operation_result,
)
from bot_app.agent_dispatch import dispatch_standard_protocol
from bot_app.agent_shell import execute_shell_protocol
from bot_app.agent_trigger import execute_trigger_protocol
from bot_app.agent_file_delivery import send_written_agent_file
from bot_app.agent_files import (
    write_base64_protocol_file,
    write_text_protocol_file,
)
from bot_app.agent_loop_state import AgentRoundState
from bot_app.agent_media import execute_media_generation
from bot_app.agent_media_delivery import send_media_generation_result
from bot_app.agent_sendfile import execute_sendfile_protocol
from bot_app.agent_presenter import (
    build_shell_presentation,
    build_standard_operation_presentation,
)

async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return

    # 处理中锁（仅对非提示词编辑状态生效）
    await UserDataManager.init()
    state = UserDataManager.get('state')
    if (not is_prompt_edit_state(state)
            and state != BotState.SET_COMMAND_BLACKLIST
            and state != BotState.SET_MEMORY
            and state != BotState.IMPORT_PROVIDER_CONFIG):
        if _conversation_processing_lock.locked():
            await update.message.reply_text(
                "⏳ 系统仍在处理上一个请求... 请稍等。"
            )
            return

    doc = update.message.document
    doc_name = doc.file_name or f"document_{uuid.uuid4().hex[:8]}.bin"
    # 使用 caption 保留格式
    caption = ""
    if update.message.caption:
        try:
            caption = (update.message.caption_markdown or update.message.caption or "").strip()
        except Exception:
            caption = (update.message.caption or "").strip()
    # 转发的富文本消息：caption 可能为空，文字在 rich_message.blocks 里
    if not caption:
        caption = _extract_rich_message_text(update.message).strip()
        if caption:
            logger.warning(f"handle_document_message: extracted caption via rich_message, len={len(caption)}: {caption[:200]}")

    if state == BotState.IMPORT_PROVIDER_CONFIG:
        await GlobalRecorder.record_user_message(
            f"[提供商配置文件] {doc_name}",
            MessageType.USER_FILE,
            update.effective_chat.id
        )
        if not doc_name.lower().endswith('.json'):
            await update.message.reply_text("⚠️ 请发送 JSON 配置文件，或发送 cancel 取消。")
            return
        if doc.file_size and doc.file_size > PROVIDER_CONFIG_MAX_BYTES:
            await update.message.reply_text(
                f"⚠️ 配置文件不能超过 {PROVIDER_CONFIG_MAX_BYTES // 1024 // 1024} MB。"
            )
            return
        status_msg = await update.message.reply_text("📥 正在校验并导入提供商配置...")
        try:
            content_bytes = bytes(await download_telegram_file(doc))
            providers, defaults = parse_provider_config_import(content_bytes)
            import_mode = UserDataManager.get('provider_import_mode')
            if import_mode not in {'merge', 'replace'}:
                raise ValueError('请先选择合并导入或覆盖导入')
            result = await apply_provider_config_import(providers, defaults, import_mode)
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('provider_import_mode', None)
            restored = '、'.join(result['restored_defaults']) or '无'
            skipped = (
                f"\n⚠️ 未恢复：{'、'.join(result['skipped_defaults'])}（提供商或模型不存在）"
                if result['skipped_defaults'] else ''
            )
            await GlobalRecorder.record_system_op(
                "导入提供商配置",
                {
                    'count': result['count'],
                    'added': result['added'],
                    'overwritten': result['overwritten'],
                    'removed': result['removed'],
                    'mode': result['mode'],
                    'file_name': doc_name
                }
            )
            mode_label = '覆盖导入' if result['mode'] == 'replace' else '合并导入'
            removed_line = f"删除旧提供商：{result['removed']} 个\n" if result['mode'] == 'replace' else ''
            await status_msg.edit_text(
                f"✅ 提供商配置导入完成。\n"
                f"方式：{mode_label}\n"
                f"新增：{result['added']} 个\n"
                f"更新同名：{result['overwritten']} 个\n"
                f"{removed_line}"
                f"默认项：{safe_text(restored)}{safe_text(skipped)}",
                reply_markup=get_providers_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        except ValueError as e:
            await status_msg.edit_text(
                f"❌ 导入失败：{safe_text(str(e))}\n\n请修正文件后重试，或发送 cancel 取消。",
                parse_mode=constants.ParseMode.HTML
            )
        except Exception as e:
            logger.exception("Provider config import failed")
            await status_msg.edit_text(
                f"❌ 导入失败：{safe_text(format_provider_exception(e))}\n\n请检查日志后重试。",
                parse_mode=constants.ParseMode.HTML
            )
        return

    if state == BotState.SET_COMMAND_BLACKLIST:
        await GlobalRecorder.record_user_message(
            f"[黑名单文件] {doc_name}",
            MessageType.USER_FILE,
            update.effective_chat.id
        )
        if not (doc_name.endswith('.txt') or doc_name.endswith('.md') or
                (doc.mime_type and 'text' in doc.mime_type)):
            await update.message.reply_text("🫠 黑名单批量导入只接受 txt / md / text 文件。")
            return
        try:
            content_bytes = await download_telegram_file(doc)
            text_content = ArtifactManager.try_decode_text(bytes(content_bytes))
            if text_content is None:
                raise ValueError("unsupported blacklist file encoding")
            current_buffer = UserDataManager.get('command_blacklist_buffer', "")
            current_buffer = current_buffer + "\n" + text_content if current_buffer else text_content
            UserDataManager.set('command_blacklist_buffer', current_buffer)
            parsed_count = len(AgentCommandBlacklist.parse_user_input(current_buffer))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 完成添加", callback_data="act_confirm_command_blacklist")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_command_blacklist")]
            ])
            await update.message.reply_text(
                f"📥 已读入 {safe_text(doc_name)}，当前累计 {parsed_count} 条可用黑名单。\n"
                "可以继续发送；批量内容每条一行，或用独立一行三个横杠 --- 分隔。最后点完成。",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Blacklist file read error: {e}")
            await update.message.reply_text("黑名单文件读取失败。")
        return

    if state == BotState.SET_MEMORY:
        await GlobalRecorder.record_user_message(
            f"[记忆文件] {doc_name}",
            MessageType.USER_FILE,
            update.effective_chat.id
        )
        if not (doc_name.endswith('.txt') or doc_name.endswith('.md') or
                (doc.mime_type and 'text' in doc.mime_type)):
            await update.message.reply_text("🫠 记忆导入只接受 txt / md / text 文件。")
            return
        try:
            content_bytes = await download_telegram_file(doc)
            text_content = ArtifactManager.try_decode_text(bytes(content_bytes))
            if text_content is None:
                raise ValueError("unsupported memory file encoding")
            current_buffer = UserDataManager.get('memory_buffer', "")
            current_buffer = current_buffer + "\n" + text_content if current_buffer else text_content
            UserDataManager.set('memory_buffer', current_buffer)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 完成并保存", callback_data="act_confirm_memory")],
                [InlineKeyboardButton("🔙 返回", callback_data="menu_memory")]
            ])
            await update.message.reply_text(
                f"📥 已读入 {safe_text(doc_name)}，当前累计 {len(current_buffer)} 字。\n"
                "可继续发送文字或文件，会自动拼接为一条；全部发完后点完成并保存。",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Memory file read error: {e}")
            await update.message.reply_text("记忆文件读取失败。")
        return

    if is_prompt_edit_state(state):
        await GlobalRecorder.record_user_message(
            f"[文件] {doc_name}",
            MessageType.USER_FILE,
            update.effective_chat.id
        )

        if not (doc_name.endswith('.txt') or doc_name.endswith('.md') or
                (doc.mime_type and 'text' in doc.mime_type)):
            await update.message.reply_text("🫠 该文件类型仅可用于更新 txt / md 提示词。")
            return

        status_msg = await update.message.reply_text("📝 正在读取用户提供的提示词文件...")
        try:
            content_bytes = await download_telegram_file(doc)
            text_content = ArtifactManager.try_decode_text(bytes(content_bytes))
            if text_content is None:
                raise ValueError("unsupported prompt file encoding")

            prompt_key = get_editing_prompt_key(state)
            if prompt_key in {'assistant_prompt', 'global_prompt_addon'}:
                await save_runtime_prompt(prompt_key, text_content)
            else:
                PromptFileManager.set(prompt_key, text_content)

            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('editing_prompt_key', "")
            UserDataManager.set('prompt_buffer', "")
            prompt_type = PromptFileManager.get_label(prompt_key)

            await GlobalRecorder.record_system_op(
                f"通过文件更新{prompt_type}提示词",
                {"length": len(text_content), "file_name": doc_name}
            )
            await status_msg.edit_text(
                f"✅ {prompt_type}提示词已更新，共 {len(text_content)} 字。",
                reply_markup=get_main_menu()
            )
        except Exception as e:
            logger.error(f"Prompt file read error: {e}")
            await status_msg.edit_text("提示词文件读取失败。")
        return

    try:
        content_bytes = await download_telegram_file(doc)
        saved_file = ArtifactManager.save_binary_upload(doc_name, content_bytes)
        note = ArtifactManager.shorten_text(caption, 80) if caption else ""
        memory_text = ArtifactManager.build_index_message("文件", doc_name, saved_file['rel_path'], note)

        await GlobalRecorder.record_user_message(
            memory_text,
            MessageType.USER_FILE,
            update.effective_chat.id
        )

        turn_parts: List[Dict[str, str]] = []
        if caption:
            turn_parts.append({"type": "text", "text": f"用户附言：{caption}"})
        turn_parts.append({
            "type": "text",
            "text": ArtifactManager.build_saved_notice("文件", saved_file['rel_path'], f"原文件名：{doc_name}")
        })

        inline_text = ArtifactManager.try_decode_text(content_bytes)
        if inline_text is not None:
            clipped_text, was_clipped = ArtifactManager.clip_inline_text(inline_text)
            clip_note = (
                "\n[系统提示] 文件内容过长，本轮只内联了前半部分，完整内容仍可通过保存路径重新读取。"
                if was_clipped else ""
            )
            turn_parts.append({
                "type": "text",
                "text": (
                    f"[文件内容开始]\n{clipped_text}{clip_note}\n[文件内容结束]\n"
                    "请直接基于文件内容回答，并说明文件保存路径。"
                )
            })
        else:
            turn_parts.append({
                "type": "text",
                "text": (
                    "这份文件已经保存到路径里了，但不会把全文长期塞在上下文里。"
                    "如果后面还要继续分析，请优先按保存路径重新读取。"
                )
            })

        forward_prefix = build_forward_origin_prefix(update.message)
        if forward_prefix:
            memory_text = f"{forward_prefix}\n{memory_text}"

        await process_conversation(
            update,
            context,
            memory_text,
            content_override=turn_parts
        )
    except Exception as e:
        logger.error(f"File save/process error: {e}")
        await update.message.reply_text(f"文件 {safe_text(doc_name)} 已收到，但保存或转交模型失败。")


def _rich_part_to_markdown(part):
    """Convert a rich text part to markdown. Handles string, dict (formatted), and list."""
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        t = part.get('text')
        if t is None:
            return ''
        if isinstance(t, (dict, list)):
            inner = _rich_part_to_markdown(t)
        else:
            inner = str(t)
        fmt = part.get('type', '')
        if fmt == 'bold':
            return '**' + inner + '**'
        elif fmt == 'italic':
            return '*' + inner + '*'
        elif fmt == 'code':
            return '`' + inner + '`'
        elif fmt == 'pre':
            return '```' + chr(10) + inner + chr(10) + '```'
        elif fmt == 'strikethrough':
            return '~~' + inner + '~~'
        elif fmt == 'underline':
            return '__' + inner + '__'
        elif fmt == 'spoiler':
            return '||' + inner + '||'
        elif fmt in ('link', 'text_link'):
            url = part.get('url', '')
            return '[' + inner + '](' + url + ')' if url else inner
        else:
            return inner
    if isinstance(part, list):
        return ''.join(_rich_part_to_markdown(p) for p in part)
    return ''


def _rich_block_to_text(block):
    """Convert a rich_message block to text. Handles paragraphs, blockquotes, tables, lists."""
    if not isinstance(block, dict):
        return ''
    btype = block.get('type', '')
    text = block.get('text')
    if text is not None:
        return _rich_part_to_markdown(text)
    if 'blocks' in block:
        nested = [_rich_block_to_text(b) for b in block['blocks'] if isinstance(b, dict)]
        result = chr(10).join(nested)
        if btype == 'blockquote':
            result = chr(10).join('> ' + line for line in result.split(chr(10)) if line)
        return result
    if 'cells' in block:
        cells = block['cells']
        if not isinstance(cells, list):
            return ''
        rows = []
        for row in cells:
            if not isinstance(row, list):
                continue
            cell_texts = []
            for cell in row:
                if isinstance(cell, dict):
                    ct = cell.get('text', '')
                    cell_texts.append(_rich_part_to_markdown(ct))
                else:
                    cell_texts.append(str(cell))
            rows.append(cell_texts)
        if not rows:
            return ''
        lines = []
        for i, row in enumerate(rows):
            lines.append('| ' + ' | '.join(row) + ' |')
            if i == 0:
                lines.append('| ' + ' | '.join(['---'] * len(row)) + ' |')
        return chr(10).join(lines)
    if 'items' in block:
        items = block['items']
        if not isinstance(items, list):
            return ''
        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = item.get('label', '-')
            item_blocks = item.get('blocks', [])
            item_text = chr(10).join(_rich_block_to_text(b) for b in item_blocks if isinstance(b, dict))
            lines.append(label + ' ' + item_text)
        return chr(10).join(lines)
    # Fallback for unknown block types: recursively search all fields for text
    logger.warning(f"_rich_block_to_text: unknown block type={btype}, keys={list(block.keys())}")
    parts = []
    for key, value in block.items():
        if key == 'type':
            continue
        if isinstance(value, str) and value.strip():
            parts.append(value)
        elif isinstance(value, dict):
            t = _rich_part_to_markdown(value)
            if not t:
                t = _rich_block_to_text(value)
            if t:
                parts.append(t)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    parts.append(item)
                elif isinstance(item, dict):
                    t = _rich_part_to_markdown(item)
                    if not t:
                        t = _rich_block_to_text(item)
                    if t:
                        parts.append(t)
    if parts:
        result = chr(10).join(parts)
        logger.warning(f"_rich_block_to_text: fallback extracted len={len(result)} from type={btype}")
        return result
    return ''


def _rich_part_to_plain_text(part):
    """提取富文本的原始文字，不添加 Markdown 标记。配置值必须走这个路径。"""
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        value = part.get('text')
        if value is not None:
            return _rich_part_to_plain_text(value)
        return ''
    if isinstance(part, list):
        return ''.join(_rich_part_to_plain_text(item) for item in part)
    return ''


def _rich_block_to_plain_text(block):
    if not isinstance(block, dict):
        return ''
    if block.get('text') is not None:
        return _rich_part_to_plain_text(block.get('text'))
    if isinstance(block.get('blocks'), list):
        return chr(10).join(
            _rich_block_to_plain_text(item)
            for item in block['blocks']
            if isinstance(item, dict)
        )
    if isinstance(block.get('cells'), list):
        rows = []
        for row in block['cells']:
            if not isinstance(row, list):
                continue
            rows.append(' | '.join(
                _rich_part_to_plain_text(cell.get('text', '') if isinstance(cell, dict) else cell)
                for cell in row
            ))
        return chr(10).join(rows)
    if isinstance(block.get('items'), list):
        rows = []
        for item in block['items']:
            if not isinstance(item, dict):
                continue
            label = str(item.get('label', '') or '')
            content = chr(10).join(
                _rich_block_to_plain_text(child)
                for child in item.get('blocks', [])
                if isinstance(child, dict)
            )
            rows.append((label + ' ' + content).strip())
        return chr(10).join(rows)
    return ''


def _extract_rich_message_plain_text(msg):
    """提取 rich_message 的纯文本，避免把配置值变成 Markdown。"""
    extra = getattr(msg, 'api_kwargs', None) or {}
    rich = extra.get('rich_message')
    if not isinstance(rich, dict):
        try:
            rich = (msg.to_dict() if hasattr(msg, 'to_dict') else {}).get('rich_message')
        except Exception:
            rich = None
    if not isinstance(rich, dict) or not isinstance(rich.get('blocks'), list):
        return ''
    return chr(10).join(
        _rich_block_to_plain_text(block)
        for block in rich['blocks']
        if isinstance(block, dict)
    )


def _extract_rich_message_text(msg):
    """Extract text from rich_message (Telegram rich text format), preserving formatting as markdown."""
    extra = getattr(msg, 'api_kwargs', None) or {}
    rich = extra.get('rich_message')
    rich_source = "api_kwargs"
    if not rich or not isinstance(rich, dict):
        try:
            msg_dict = msg.to_dict() if hasattr(msg, 'to_dict') else {}
            rich = msg_dict.get('rich_message')
            rich_source = "to_dict"
        except Exception:
            rich = None
    if not rich or not isinstance(rich, dict):
        return ""
    try:
        rich_json = json.dumps(rich, ensure_ascii=False, default=str)
        logger.warning(f"_extract_rich_message_text: source={rich_source}, keys={list(rich.keys())}, full={rich_json[:3000]}")
    except Exception:
        logger.warning(f"_extract_rich_message_text: source={rich_source}, keys={list(rich.keys()) if isinstance(rich, dict) else type(rich)}")
    md = rich.get('markdown')
    if md and isinstance(md, str) and md.strip():
        logger.warning(f"_extract_rich_message_text: extracted via markdown, len={len(md)}: {md[:200]}")
        return md
    blocks = rich.get('blocks')
    if not blocks or not isinstance(blocks, list):
        return ""
    logger.warning(f"_extract_rich_message_text: found {len(blocks)} blocks")
    paragraphs = []
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        btype = block.get('type', '')
        t = _rich_block_to_text(block)
        if t:
            logger.warning(f"_extract_rich_message_text: block[{i}] ({btype}) len={len(t)}")
            paragraphs.append(t)
        else:
            logger.warning(f"_extract_rich_message_text: block[{i}] ({btype}) empty, keys={list(block.keys())}")
    result = chr(10).join(paragraphs)
    logger.warning(f"_extract_rich_message_text: total len={len(result)}, paragraphs={len(paragraphs)}")
    return result

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    
    # 处理中锁：防止媒体生成/长回复期间发消息冲突
    if _conversation_processing_lock.locked():
        await update.message.reply_text(
            "⏳ 系统仍在处理上一个请求... 请稍后再发送新请求。"
        )
        return
    
    await UserDataManager.init()
    state = UserDataManager.get('state')
    # 普通聊天使用 text_markdown 保留粗体/斜体/代码块等格式。
    # 但 Key、URL、模型 ID、提供商名称等配置值必须使用 Telegram 原始文本：
    # Markdown 序列化会给特殊字符插入转义符（例如 \, -, _, `），导致凭据或模型 ID 被改写。
    exact_value_states = {
        BotState.ADD_PROV_NAME,
        BotState.ADD_PROV_URL,
        BotState.ADD_PROV_KEY,
        BotState.EDIT_PROV_NAME,
        BotState.EDIT_PROV_KEY,
        BotState.EDIT_PROV_URL,
        BotState.ADD_MODEL_MANUAL,
        BotState.SEARCH_FETCHED,
        BotState.SEARCH_SAVED,
        BotState.IMPORT_PROVIDER_CONFIG,
    }
    text = ""
    if update.message.text:
        if state in exact_value_states:
            text = update.message.text.strip()
        else:
            try:
                text = (update.message.text_markdown or update.message.text or "").strip()
            except Exception:
                text = (update.message.text or "").strip()
    else:
        text = (update.message.caption or "").strip()

    # 转发消息 text 可能为 None（python-telegram-bot 解析问题），
    # 用 forwardMessage API 重新拉取完整消息内容
    if not text:
        try:
            msg_dict_debug = update.message.to_dict() if hasattr(update.message, 'to_dict') else {}
            logger.warning(f"handle_text_message: text is None, raw dict keys: {list(msg_dict_debug.keys())}")
            extra = getattr(update.message, 'api_kwargs', None) or {}
            if extra:
                logger.warning(f"handle_text_message: api_kwargs keys: {list(extra.keys())}")
        except Exception:
            pass
        try:
            fwd_msg = await context.bot.forward_message(
                chat_id=update.effective_chat.id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                disable_notification=True
            )
            text = (getattr(fwd_msg, 'text', None) or getattr(fwd_msg, 'caption', None) or "").strip()
            # 立即删除转发的副本
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=fwd_msg.message_id
                )
            except Exception:
                pass
            if text:
                logger.warning(f"handle_text_message: extracted via forwardMessage, len={len(text)}: {text[:200]}")
            else:
                logger.warning("handle_text_message: forwardMessage returned but no text/caption found")
        except Exception as e:
            logger.warning(f"handle_text_message: forwardMessage fallback failed: {e}")

    # 富文本消息：text 在 rich_message.blocks 结构里，不在 text 字段。
    # 配置值取纯文本；普通聊天才保留 Markdown 标记。
    if not text:
        if state in exact_value_states:
            text = _extract_rich_message_plain_text(update.message).strip()
        else:
            text = _extract_rich_message_text(update.message).strip()
        if text:
            logger.warning(f"handle_text_message: extracted via rich_message.blocks, len={len(text)}: {text[:200]}")

    # 仍然没有文字：不是文字消息（如转发的语音/视频/位置等），交给 handle_other_message
    if not text:
        await handle_other_message(update, context)
        return

    # 导入 JSON 时必须使用 Telegram 原始文本，避免 text_markdown 自动转义下划线等字符。
    if state == BotState.IMPORT_PROVIDER_CONFIG and update.message.text:
        text = update.message.text.strip()

    # 转发消息添加来源信息
    forward_prefix = build_forward_origin_prefix(update.message)
    if forward_prefix:
        text = f"{forward_prefix}\n{text}"

    # 普通聊天按拼接模式决定：直接发送，或累计到“完成”按钮后再写入记忆。
    if state != BotState.IDLE:
        recorded_text = (
            "[已填入 UPDATE_GITHUB_TOKEN，内容已隐藏]"
            if state == BotState.SET_UPDATE_TOKEN
            else "[已提交提供商配置 JSON，内容已隐藏]"
            if state == BotState.IMPORT_PROVIDER_CONFIG
            else "[已填入 API Key，内容已隐藏]"
            if state in (BotState.EDIT_PROV_KEY, BotState.ADD_PROV_KEY)
            else text
        )
        await GlobalRecorder.record_user_message(recorded_text, MessageType.USER_TEXT, update.effective_chat.id)
    
    # 取消操作
    if text.lower() == 'cancel' and state != BotState.IDLE:
        UserDataManager.set('state', BotState.IDLE)
        UserDataManager.set('pending_update_zip_url', "")
        UserDataManager.set('editing_prompt_key', "")
        UserDataManager.set('prompt_buffer', "")
        UserDataManager.set('command_blacklist_buffer', "")
        UserDataManager.set('memory_buffer', "")
        UserDataManager.set('provider_import_mode', None)
        UserDataManager.set('editing_provider', None)
        await update.message.reply_text(
            "🚫 操作已取消。",
            reply_markup=get_main_menu()
        )
        return

    # --- 状态机处理 ---
    if state == BotState.IMPORT_PROVIDER_CONFIG:
        status_msg = await update.message.reply_text("📥 正在校验并导入提供商配置...")
        try:
            providers, defaults = parse_provider_config_import(text.encode('utf-8'))
            import_mode = UserDataManager.get('provider_import_mode')
            if import_mode not in {'merge', 'replace'}:
                raise ValueError('请先选择合并导入或覆盖导入')
            result = await apply_provider_config_import(providers, defaults, import_mode)
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('provider_import_mode', None)
            restored = '、'.join(result['restored_defaults']) or '无'
            skipped = (
                f"\n⚠️ 未恢复：{'、'.join(result['skipped_defaults'])}（提供商或模型不存在）"
                if result['skipped_defaults'] else ''
            )
            await GlobalRecorder.record_system_op(
                "通过文本导入提供商配置",
                {
                    'count': result['count'],
                    'added': result['added'],
                    'overwritten': result['overwritten'],
                    'removed': result['removed'],
                    'mode': result['mode']
                }
            )
            mode_label = '覆盖导入' if result['mode'] == 'replace' else '合并导入'
            removed_line = f"删除旧提供商：{result['removed']} 个\n" if result['mode'] == 'replace' else ''
            await status_msg.edit_text(
                f"✅ 提供商配置导入完成。\n"
                f"方式：{mode_label}\n"
                f"新增：{result['added']} 个\n"
                f"更新同名：{result['overwritten']} 个\n"
                f"{removed_line}"
                f"默认项：{safe_text(restored)}{safe_text(skipped)}",
                reply_markup=get_providers_menu(),
                parse_mode=constants.ParseMode.HTML
            )
        except ValueError as e:
            await status_msg.edit_text(
                f"❌ 导入失败：{safe_text(str(e))}\n\n请重新发送完整 JSON，或发送 cancel 取消。",
                parse_mode=constants.ParseMode.HTML
            )
        except Exception as e:
            logger.exception("Provider config text import failed")
            await status_msg.edit_text(
                f"❌ 导入失败：{safe_text(format_provider_exception(e))}",
                parse_mode=constants.ParseMode.HTML
            )
        return

    if state == BotState.SET_UPDATE_TOKEN:
        token = text.strip()
        if not token:
            await update.message.reply_text("⚠️ GitHub Token 不能为空。请重新发送，或发送 cancel 取消。")
            return
        pending_update_url = UserDataManager.get('pending_update_zip_url', "") or BotConfig.TEST_UPDATE_ZIP_URL
        try:
            await asyncio.to_thread(persist_update_github_token, token, pending_update_url)
        except Exception as e:
            logger.exception("保存更新 token 失败")
            await update.message.reply_text(
                f"❌ 保存 GitHub Token 失败：<code>{safe_text(format_provider_exception(e))}</code>",
                parse_mode=constants.ParseMode.HTML
            )
            return

        UserDataManager.set('state', BotState.IDLE)
        UserDataManager.set('pending_update_zip_url', "")
        await GlobalRecorder.record_system_op(
            "保存 UPDATE_GITHUB_TOKEN 并继续更新确认",
            {"update_source": BotConfig.UPDATE_ZIP_URL},
            update.effective_chat.id
        )
        status_msg = await update.message.reply_text(
            "✅ Token 已保存，信息已加密",
            parse_mode=constants.ParseMode.HTML
        )
        await send_update_confirmation_message(status_msg)
        return

    if state == BotState.SET_PROMPT:
        current_buffer = UserDataManager.get('prompt_buffer', "")
        current_buffer = current_buffer + "\n" + text if current_buffer else text
        UserDataManager.set('prompt_buffer', current_buffer)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 完成输入", callback_data="act_confirm_normal_prompt")]
        ])
        await update.message.reply_text(
            f"📥 收到！(当前累计 {len(current_buffer)} 字)\n继续发送或点完成。",
            reply_markup=kb
        )
        return
    
    if state == BotState.SET_GLOBAL_PROMPT:
        current_buffer = UserDataManager.get('prompt_buffer', "")
        current_buffer = current_buffer + "\n" + text if current_buffer else text
        UserDataManager.set('prompt_buffer', current_buffer)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 完成输入", callback_data="act_confirm_global_prompt")]
        ])
        await update.message.reply_text(
            f"📥 收到！(当前累计 {len(current_buffer)} 字)\n继续发送或点完成。",
            reply_markup=kb
        )
        return

    if state == BotState.SET_ANY_PROMPT:
        prompt_key = get_editing_prompt_key(state)
        current_buffer = UserDataManager.get('prompt_buffer', "")
        separator = "\n---\n" if prompt_key == 'unauthorized_reply_messages' else "\n"
        current_buffer = current_buffer + separator + text if current_buffer else text
        UserDataManager.set('prompt_buffer', current_buffer)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 完成输入", callback_data=f"act_confirm_prompt:{prompt_key}")]
        ])
        if prompt_key == 'unauthorized_reply_messages':
            reply_text = (
                f"📥 收到！当前累计 {len(current_buffer)} 字。\n"
                "未授权回复语录可以一次发送一条并多次发送；也可以一次发送多条，"
                "条目之间用独立一行三个横杠 --- 分隔。最后点完成。"
            )
        else:
            reply_text = f"📥 收到！(当前累计 {len(current_buffer)} 字)\n继续发送或点完成。"
        await update.message.reply_text(reply_text, reply_markup=kb)
        return

    if state == BotState.SET_COMMAND_BLACKLIST:
        current_buffer = UserDataManager.get('command_blacklist_buffer', "")
        current_buffer = current_buffer + "\n" + text if current_buffer else text
        UserDataManager.set('command_blacklist_buffer', current_buffer)
        parsed_count = len(AgentCommandBlacklist.parse_user_input(current_buffer))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 完成添加", callback_data="act_confirm_command_blacklist")],
            [InlineKeyboardButton("🔙 返回", callback_data="menu_command_blacklist")]
        ])
        await update.message.reply_text(
            f"📥 收到！当前累计 {parsed_count} 条可用黑名单。\n"
            "可以继续发送；批量内容每条一行，或用独立一行三个横杠 --- 分隔。最后点完成。",
            reply_markup=kb
        )
        return

    if state == BotState.SET_MEMORY:
        current_buffer = UserDataManager.get('memory_buffer', "")
        current_buffer = current_buffer + "\n" + text if current_buffer else text
        UserDataManager.set('memory_buffer', current_buffer)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 完成并保存", callback_data="act_confirm_memory")],
            [InlineKeyboardButton("🔙 返回", callback_data="menu_memory")]
        ])
        await update.message.reply_text(
            f"📥 收到！当前累计 {len(current_buffer)} 字。\n"
            "可继续发送下一段，会自动拼接为一条；全部发完后点完成并保存。",
            reply_markup=kb
        )
        return

    if state == BotState.SET_AI_TIMEOUT:
        try:
            timeout_val = parse_timeout_seconds(text, minimum=1)
        except ValueError:
            await update.message.reply_text("⚠️ 请输入秒数，例如 45、180、300s。")
            return
        UserDataManager.set('stream_timeout', timeout_val)
        UserDataManager.set('state', BotState.IDLE)
        await UserDataManager.save_config('stream_timeout', timeout_val)
        PortalManager._portals.clear()
        await GlobalRecorder.record_system_op(f"设置 AI 回复超时: {_fmt_timeout(timeout_val)}")
        await update.message.reply_text(
            f"✅ AI回复超时已设为 {_fmt_timeout(timeout_val)}。",
            reply_markup=get_timeout_settings_menu()
        )
        return

    if state == BotState.SET_COMMAND_TIMEOUT:
        try:
            timeout_val = parse_timeout_seconds(
                text,
                minimum=MIN_AGENT_COMMAND_TIMEOUT,
                maximum=MAX_AGENT_COMMAND_TIMEOUT
            )
        except ValueError:
            await update.message.reply_text(
                f"⚠️ 请输入 {MIN_AGENT_COMMAND_TIMEOUT}-{MAX_AGENT_COMMAND_TIMEOUT} 之间的秒数，"
                "例如 90、300、600s。"
            )
            return
        UserDataManager.set('agent_command_timeout', timeout_val)
        UserDataManager.set('state', BotState.IDLE)
        await UserDataManager.save_config('agent_command_timeout', timeout_val)
        await GlobalRecorder.record_system_op(f"设置命令等待窗口: {_fmt_command_timeout(timeout_val)}")
        await update.message.reply_text(
            f"✅ 命令等待窗口已设为 {_fmt_command_timeout(timeout_val)}。",
            reply_markup=get_timeout_settings_menu()
        )
        return

    if state == BotState.SET_AGENT_MAX_ITERATIONS:
        try:
            iterations = parse_agent_max_iterations(text)
        except ValueError:
            await update.message.reply_text(
                f"⚠️ 请输入 {MIN_AGENT_MAX_ITERATIONS}-{MAX_AGENT_MAX_ITERATIONS} 之间的轮数，"
                "例如 8、15、25轮。"
            )
            return
        UserDataManager.set('agent_max_iterations', iterations)
        UserDataManager.set('state', BotState.IDLE)
        await UserDataManager.save_config('agent_max_iterations', iterations)
        await GlobalRecorder.record_system_op(f"设置 Agent 最大轮数: {_fmt_agent_max_iterations(iterations)}")
        await update.message.reply_text(
            f"✅ Agent最大轮数已设为 {_fmt_agent_max_iterations(iterations)}。",
            reply_markup=get_timeout_settings_menu()
        )
        return

    if state == BotState.SET_IDLE_MESSAGE_INTERVAL:
        try:
            interval = parse_idle_message_interval(text)
        except ValueError:
            await update.message.reply_text(
                "⚠️ 请输入有效的时间，例如 90m、2h、3天、7200s；发送 0、∞ 或 关闭 可停用。"
            )
            return
        UserDataManager.set('idle_message_interval', interval)
        UserDataManager.set('state', BotState.IDLE)
        await UserDataManager.save_config('idle_message_interval', interval)
        await GlobalRecorder.record_system_op(f"设置空闲提醒间隔: {_fmt_idle_message_interval(interval)}")
        await update.message.reply_text(
            f"✅ 空闲提醒间隔已设为 {_fmt_idle_message_interval(interval)}。",
            reply_markup=get_timeout_settings_menu()
        )
        return
    
    if state == BotState.SET_GLOBAL_DEPTH:
        if text.isdigit() and 1 <= int(text) <= 500:
            depth = int(text)
            UserDataManager.set('global_depth', depth)
            UserDataManager.set('state', BotState.IDLE)
            await UserDataManager.save_config('global_depth', depth)
            await GlobalRecorder.record_system_op(f"设置记忆深度: {depth}")
            await update.message.reply_text(
                f"✅ 记忆深度已设为 {depth} 条。",
                reply_markup=get_main_menu()
            )
        else:
            await update.message.reply_text("⚠️ 请输入 1-500 之间的数字。")
        return
    
    if state == BotState.ADD_PROV_NAME:
        providers = UserDataManager.get('providers', {})
        if text in providers:
            await update.message.reply_text("⚠️ 该名称已存在，请更换。")
            return
        if len(text) > 20:
            await update.message.reply_text("⚠️ 名字太长了。最多20个字符。")
            return
        UserDataManager.set('temp_prov_name', text)
        
        # 如果已经有预设 URL（快速添加模式），跳过 URL 输入
        preset_url = UserDataManager.get('temp_prov_url')
        api_format = UserDataManager.get('temp_prov_format', 'openai')
        if preset_url:
            UserDataManager.set('state', BotState.ADD_PROV_KEY)
            await update.message.reply_text(
                f"🔑 <b>请输入 API Key</b>\n\n"
                f"提供商名称: <b>{safe_text(text)}</b>\n"
                f"接口模式: <b>{safe_text(get_provider_mode_label(api_format, preset_url))}</b>\n"
                f"Base URL: <code>{safe_text(preset_url)}</code>\n"
                f"请求形式: <code>{safe_text(get_provider_request_hint(api_format, preset_url))}</code>\n\n"
                f"{safe_text(get_provider_key_hint(api_format, preset_url))}",
                parse_mode=constants.ParseMode.HTML
            )
        else:
            UserDataManager.set('state', BotState.ADD_PROV_URL)
            await update.message.reply_text(
                "🔗 <b>请输入兼容接口的 Base URL</b>\n\n"
                "这一项用于 <b>OpenAI 兼容</b> 提供商。\n"
                "常见示例：\n"
                "• 深求: <code>https://api.deepseek.com/v1</code>\n"
                "• 魔塔社区: <code>https://api-inference.modelscope.cn/v1</code>\n"
                "• 月之暗面: <code>https://api.moonshot.cn/v1</code>\n"
                "• 其他兼容接口: <code>https://example.com/v1</code>\n\n"
                "⚠️ <b>填 URL 输到 /v1 就行</b>，不需要加模型名\n"
                "实际请求路径例如：\n"
                "<code>https://api.openai.com/v1/chat/completions</code>\n"
                "<i>↑ /chat/completions 部分由 系统自动拼接</i>",
                parse_mode=constants.ParseMode.HTML
            )
        return
    
    if state == BotState.ADD_PROV_URL:
        if not text.startswith("http"):
            await update.message.reply_text("⚠️ 必须是 http 开头。")
            return
        UserDataManager.set('temp_prov_url', text)
        UserDataManager.set('state', BotState.ADD_PROV_KEY)
        api_format = UserDataManager.get('temp_prov_format', 'openai_compatible')
        await update.message.reply_text(
            f"🔑 <b>请输入 API Key</b>\n\n"
            f"接口模式: <b>{safe_text(get_provider_mode_label(api_format, text))}</b>\n"
            f"Base URL: <code>{safe_text(text)}</code>\n"
            f"请求形式: <code>{safe_text(get_provider_request_hint(api_format, text))}</code>\n\n"
            f"{safe_text(get_provider_key_hint(api_format, text))}\n\n"
            f"💡 支持填写多个 Key，用英文逗号 <code>,</code> 隔开即可轮询调用，空格会被自动忽略。",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    if state == BotState.ADD_PROV_KEY:
        name = UserDataManager.get('temp_prov_name')
        url = UserDataManager.get('temp_prov_url')
        api_format = UserDataManager.get('temp_prov_format', 'openai')
        # 支持多个 Key（英文逗号分隔）；只移除复制粘贴混入的空白，不改写连字符。
        text = ','.join(parse_api_keys(text))
        providers = UserDataManager.get('providers', {})
        providers[name] = {'base_url': url, 'api_key': text, 'models': [], 'api_format': api_format}
        db = await BotMemoryDB.get_instance()
        await db.save_provider(name, url, text, [], api_format=api_format)
        await UserDataManager.reload_providers()
        UserDataManager.set('state', BotState.IDLE)
        UserDataManager.set('temp_prov_format', None)
        await GlobalRecorder.record_system_op(f"添加Provider: {name}", {"base_url": url, "format": api_format})
        
        format_label = get_provider_mode_label(api_format, url)
        await update.message.reply_text(
            f"🎉 提供商 <b>{safe_text(name)}</b> 已保存。\n"
            f"🔗 {safe_text(url)}\n"
            f"📌 模式: {safe_text(format_label)}",
            reply_markup=get_providers_menu(),
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    if state == BotState.EDIT_PROV_NAME:
        old_name = UserDataManager.get('editing_provider')
        new_name = text.strip()
        providers = UserDataManager.get('providers', {}) or {}
        if not old_name or old_name not in providers:
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('editing_provider', None)
            await update.message.reply_text("⚠️ 原提供商不存在，请重新打开提供商菜单。", reply_markup=get_providers_menu())
            return
        if not new_name or len(new_name) > 20 or any(ord(ch) < 32 for ch in new_name):
            await update.message.reply_text("⚠️ 名称不能为空、最多 20 个字符，且不能包含控制字符。请重新发送。")
            return
        if new_name != old_name and new_name in providers:
            await update.message.reply_text("⚠️ 该名称已存在，请换一个名称。")
            return
        if new_name == old_name:
            UserDataManager.set('state', BotState.IDLE)
            UserDataManager.set('editing_provider', new_name)
            await update.message.reply_text("✅ 名称未改变。", reply_markup=get_provider_detail_menu(new_name))
            return

        db = await BotMemoryDB.get_instance()
        try:
            await db.rename_provider(old_name, new_name)
        except ValueError as e:
            await update.message.reply_text(f"⚠️ {safe_text(str(e))}", parse_mode=constants.ParseMode.HTML)
            return
        PortalManager.remove_portal(old_name)
        if UserDataManager.get('active_provider_key') == old_name:
            UserDataManager.set('active_provider_key', new_name)
        if UserDataManager.get('default_media_provider_key') == old_name:
            UserDataManager.set('default_media_provider_key', new_name)
        await UserDataManager.reload_providers()
        UserDataManager.set('editing_provider', new_name)
        UserDataManager.set('state', BotState.IDLE)
        await GlobalRecorder.record_system_op(
            f"重命名Provider: {old_name} -> {new_name}",
            {'old_name': old_name, 'new_name': new_name}
        )
        await update.message.reply_text(
            f"✅ 提供商已重命名为 <b>{safe_text(new_name)}</b>。",
            reply_markup=get_provider_detail_menu(new_name),
            parse_mode=constants.ParseMode.HTML
        )
        return

    if state == BotState.EDIT_PROV_KEY:
        p = UserDataManager.get('editing_provider')
        # 支持多个 Key（英文逗号分隔）；只移除复制粘贴混入的空白，不改写连字符。
        text = ','.join(parse_api_keys(text))
        providers = UserDataManager.get('providers', {})
        if p and p in providers:
            providers[p]['api_key'] = text
            prov = providers[p]
            db = await BotMemoryDB.get_instance()
            await db.save_provider(
                p,
                prov['base_url'],
                text,
                prov.get('models', []),
                api_format=prov.get('api_format', 'openai')
            )
            await GlobalRecorder.record_system_op(f"更新Provider API Key: {p}")
            # 清理旧 Key 的客户端缓存并重置轮询计数器
            PortalManager.remove_portal(p)
        UserDataManager.set('state', BotState.IDLE)
        await update.message.reply_text(
            "✅ 新的 Key 已更新。",
            reply_markup=get_provider_detail_menu(p)
        )
        return
    
    if state == BotState.EDIT_PROV_URL:
        p = UserDataManager.get('editing_provider')
        providers = UserDataManager.get('providers', {})
        if p and p in providers:
            providers[p]['base_url'] = text
            prov = providers[p]
            db = await BotMemoryDB.get_instance()
            await db.save_provider(
                p,
                text,
                prov['api_key'],
                prov.get('models', []),
                api_format=prov.get('api_format', 'openai')
            )
            await GlobalRecorder.record_system_op(f"更新Provider URL: {p}", {"new_url": text})
        UserDataManager.set('state', BotState.IDLE)
        await update.message.reply_text(
            "✅ 新的 URL 已更新。",
            reply_markup=get_provider_detail_menu(p)
        )
        return
    
    if state == BotState.ADD_MODEL_MANUAL:
        p = UserDataManager.get('editing_provider')
        providers = UserDataManager.get('providers', {})
        if p and p in providers:
            model_names = parse_manual_model_names(text)
            if not model_names:
                await update.message.reply_text(
                    "⚠️ 没读到模型代号。可以输入一个模型，或用英文逗号 <code>,</code> 批量分隔。",
                    parse_mode=constants.ParseMode.HTML
                )
                return

            if 'models' not in providers[p]:
                providers[p]['models'] = []
            existing_models = providers[p]['models']
            added_models: List[str] = []
            skipped_models: List[str] = []
            for model_name in model_names:
                if model_name in existing_models:
                    skipped_models.append(model_name)
                    continue
                existing_models.append(model_name)
                added_models.append(model_name)

            if added_models:
                db = await BotMemoryDB.get_instance()
                await db.update_provider_models(p, existing_models)
                await GlobalRecorder.record_system_op(
                    f"手动添加模型: {', '.join(added_models)}",
                    {
                        "provider": p,
                        "count": len(added_models),
                        "skipped_existing": skipped_models
                    }
                )
            UserDataManager.set('state', BotState.IDLE)
            kb = build_saved_models_keyboard(p)
            if added_models:
                added_preview = "、".join(safe_text(name) for name in added_models[:8])
                if len(added_models) > 8:
                    added_preview += f" 等 {len(added_models)} 个"
                reply_text = f"✅ 已记住 {len(added_models)} 个模型: {added_preview}"
                if skipped_models:
                    reply_text += f"\nℹ️ 已跳过 {len(skipped_models)} 个重复模型。"
            else:
                reply_text = "ℹ️ 这些模型以前都保存过了。"
            await update.message.reply_text(
                reply_text,
                reply_markup=kb,
                parse_mode=constants.ParseMode.HTML
            )
        return
    
    if state == BotState.SEARCH_SAVED:
        UserDataManager.set('temp_saved_filter', text)
        UserDataManager.set('temp_page', 1)
        UserDataManager.set('state', BotState.IDLE)
        pname = UserDataManager.get('temp_viewing_prov')
        providers = UserDataManager.get('providers', {})
        if not pname or pname not in providers:
            await update.message.reply_text(
                "⚠️ 已保存模型列表已失效，请重新选择提供商。",
                reply_markup=get_providers_menu()
            )
            return
        title, kb = build_saved_models_view(pname)
        await update.message.reply_text(
            title,
            reply_markup=kb,
            parse_mode=constants.ParseMode.HTML
        )
        return

    if state == BotState.SEARCH_FETCHED:
        UserDataManager.set('temp_filter', text)
        UserDataManager.set('temp_page', 1)
        UserDataManager.set('state', BotState.IDLE)
        pname = UserDataManager.get('temp_viewing_prov')
        models = UserDataManager.get('fetched_cache', [])
        title, kb = build_fetched_models_view(pname)
        await update.message.reply_text(title, reply_markup=kb)
        return
    
    if state == BotState.RENAME_CHAT:
        UserDataManager.set('state', BotState.IDLE)
        await update.message.reply_text(
            "🏷️ 现在只有一份全局记忆，不再支持单独重命名。",
            reply_markup=get_main_menu()
        )
        return
    
    # --- 正常对话处理 ---
    forward_prefix = build_forward_origin_prefix(update.message)
    if forward_prefix:
        text = f"{forward_prefix}\n{text}"
    await handle_normal_text_conversation(update, context, text)


async def handle_normal_text_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if has_pending_text_conversation(update):
        await queue_text_conversation(update, context, text)
        return

    if not should_stitch_text_message(text):
        await GlobalRecorder.record_user_message(text, MessageType.USER_TEXT, update.effective_chat.id)
        await process_conversation(update, context, text)
        return

    await queue_text_conversation(update, context, text)


async def queue_text_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    key = get_text_conversation_buffer_key(update)

    with _pending_text_conversations_lock:
        pending = _pending_text_conversations.get(key)
        if pending is None:
            pending = PendingTextConversation(update, context, text)
            _pending_text_conversations[key] = pending
        else:
            pending.append(update, context, text)

    await show_or_update_text_stitch_prompt(pending)


async def show_or_update_text_stitch_prompt(pending: PendingTextConversation):
    text = build_text_stitch_pending_text(pending)
    reply_markup = get_text_stitch_pending_keyboard()

    if pending.prompt_message is not None:
        try:
            await pending.prompt_message.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.HTML
            )
            return
        except Exception as e:
            logger.debug(f"更新拼接提示失败，将重新发送提示: {e}")

    try:
        pending.prompt_message = await pending.update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=constants.ParseMode.HTML
        )
    except Exception as e:
        logger.warning(f"发送拼接提示失败: {e}")


async def finish_text_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_text_conversation_buffer_key(update)
    with _pending_text_conversations_lock:
        pending = _pending_text_conversations.pop(key, None)

    if pending is None:
        if update.callback_query:
            await update.callback_query.answer("没有正在拼接的内容", show_alert=True)
        return

    text = merge_text_conversation_parts(pending.parts)
    if not text:
        if update.callback_query:
            await update.callback_query.answer("拼接内容为空", show_alert=True)
        return

    if pending.prompt_message is not None:
        with contextlib.suppress(Exception):
            await pending.prompt_message.edit_text(
                f"✅ 已完成拼接，正在发送给 AI。\n累计 {len(pending.parts)} 段，{len(text)} 字。"
            )

    await GlobalRecorder.record_user_message(text, MessageType.USER_TEXT, pending.update.effective_chat.id)
    logger.info(
        f"Finished stitched text conversation: parts={len(pending.parts)}, "
        f"chars={len(text)}, chat_id={key[0]}"
    )
    await process_conversation(pending.update, pending.context, text)


async def cancel_text_conversation(update: Update):
    key = get_text_conversation_buffer_key(update)
    with _pending_text_conversations_lock:
        pending = _pending_text_conversations.pop(key, None)

    if pending is None:
        if update.callback_query:
            await update.callback_query.answer("没有正在拼接的内容", show_alert=True)
        return

    try:
        if pending.prompt_message is not None:
            await pending.prompt_message.edit_text("🧹 已清空本次拼接内容。")
    except Exception as e:
        logger.debug(f"清空拼接提示更新失败: {e}")

async def _reset_agent_turn_iteration(db: BotMemoryDB) -> int:
    await db.set_config(AGENT_TURN_ITERATION_CONFIG_KEY, 0)
    return 0


async def _reserve_agent_turn_iteration(db: BotMemoryDB) -> int:
    raw_value = await db.get_config(AGENT_TURN_ITERATION_CONFIG_KEY, 0)
    try:
        current = max(0, int(raw_value))
    except (TypeError, ValueError):
        current = 0
    next_iteration = int(current) + 1
    await db.set_config(AGENT_TURN_ITERATION_CONFIG_KEY, next_iteration)
    return next_iteration


def _build_agent_trigger_round_notice(current_iteration: int, max_iterations: int) -> str:
    if current_iteration > max_iterations:
        exceeded = current_iteration - max_iterations
        return (
            f"🛠️ 第 {current_iteration} 轮Agent操作完成，但已超出最大 {max_iterations} 轮（超出 {exceeded} 轮）。\n"
            "后台任务结果已记录，但本次未提交给 AI。\n"
            "只有新的用户消息才会重置 Agent 轮数。"
        )
    return f"🛠️ 第 {current_iteration} 轮Agent操作完成，正在整理结果..."


async def _send_agent_trigger_round_notice(context: ContextTypes.DEFAULT_TYPE,
                                           chat_id: int, current_iteration: int,
                                           max_iterations: int):
    message = _build_agent_trigger_round_notice(current_iteration, max_iterations)
    # 只发 Telegram 界面，不写入 AI 历史——与普通 Agent 循环里同名进度提示的处理方式保持一致
    # （普通循环仅 safe_edit_text 编辑状态消息，从不入库）；轮数状态由 DB 跟踪，无需靠历史记录。
    await safe_send_message(context, chat_id, message)


async def _send_agent_iteration_limit_notice(context: ContextTypes.DEFAULT_TYPE,
                                             chat_id: int, current_iteration: int,
                                             max_iterations: int):
    message = (
        f"⚠️ Agent 当前为第 {current_iteration} 轮，已超过最大 {max_iterations} 轮。\n"
        "本次系统结果已经保留，但不会继续调用 AI。只有新的用户消息才会重置轮数。"
    )
    # 只发 Telegram 界面，不写入 AI 历史（与 🛠️ 轮数进度提示同原则：协议执行 scaffolding 不进上下文）
    await safe_send_message(context, chat_id, message)


async def process_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                               content_override: Optional[Any] = None,
                               lock_acquired_event: Optional[asyncio.Event] = None,
                               force_agent_mode: bool = False,
                               reset_agent_iterations: bool = True):
    """处理对话（全局模式 + Agent 协议执行：命令 / 读文件 / 发文件 / 写文件 / 媒体）"""
    global _is_processing, _stop_generation_event
    async with _conversation_processing_lock:
        if lock_acquired_event is not None:
            lock_acquired_event.set()
        _is_processing = True
        _stop_generation_event = asyncio.Event()

        try:
            await _process_conversation_inner(
                update,
                context,
                text,
                content_override,
                force_agent_mode,
                reset_agent_iterations,
            )
        finally:
            _stop_generation_event = None
            _is_processing = False


async def _process_conversation_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                                       content_override: Optional[Any] = None,
                                       force_agent_mode: bool = False,
                                       reset_agent_iterations: bool = True):
    """process_conversation 内部实现"""
    agent_mode = force_agent_mode or UserDataManager.get('agent_mode', False)
    stream_mode = normalize_bool(UserDataManager.get('stream_mode', True), True)
    db = await BotMemoryDB.get_instance()
    cid, cdata = await get_or_create_chat_session()
    max_agent_iterations = normalize_agent_max_iterations(
        UserDataManager.get('agent_max_iterations', DEFAULT_AGENT_MAX_ITERATIONS)
    )
    if reset_agent_iterations:
        agent_iteration = await _reset_agent_turn_iteration(db)
    else:
        agent_iteration = await _reserve_agent_turn_iteration(db)
        await db.add_chat_message(cid, 'user', text)
        await _send_agent_trigger_round_notice(
            context,
            update.effective_chat.id,
            agent_iteration,
            max_agent_iterations,
        )
        if agent_iteration > max_agent_iterations:
            return
    model = cdata.get('model') or UserDataManager.get('default_model')
    prov_name, prov_data = get_current_provider()
    
    if not prov_data or not model:
        message = update.message or update.callback_query.message
        await message.reply_text(
            "对话能力尚未配置。请先在【提供商】添加线路，并在【默认模型】中选择对话模型。",
            reply_markup=get_main_menu()
        )
        return
    assert prov_name is not None

    if reset_agent_iterations:
        await db.add_chat_message(cid, 'user', text)

    global_depth = max(1, int(UserDataManager.get('global_depth', 30)))
    system_prompt = build_conversation_system_prompt(agent_mode)
    history = await db.get_conversation_messages(global_depth)
    if content_override is not None:
        # 文件/图片本体只在本轮临时喂给模型；长期记忆和导出仍只保留路径索引。
        for msg in reversed(history):
            if msg.get('role') == 'user':
                msg['content'] = content_override
                break
        else:
            history.append({'role': 'user', 'content': content_override})
    
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=constants.ChatAction.TYPING
    )

    # 根据流式/非流式开关选择回复方式
    if stream_mode:
        response = await send_streaming_response(
            update, context,
            prov_name, prov_data, model,
            system_prompt, history
        )
    else:
        response = await send_non_streaming_response(
            update, context,
            prov_name, prov_data, model,
            system_prompt, history
        )
    
    if not response:
        await db.remove_last_chat_message(cid)
        return
    
    # 保存 AI 回复
    await GlobalRecorder.record_ai_reply(response, update.effective_chat.id)
    await db.add_chat_message(cid, 'assistant', response)
    agent_turn_history: List[Dict[str, Any]] = list(history)
    agent_turn_history.append({'role': 'assistant', 'content': response})
    
    # --- Agent 模式：命令 / 读文件 / 发文件 / 写文件 / 媒体协议循环 ---
    if agent_mode:
        while True:
            if is_stop_requested():
                await safe_send_message(
                    context,
                    update.effective_chat.id,
                    (
                        "⏹️ 已停止当前回合，后续 Agent 操作不会继续执行。\n"
                        "已经产生的工具结果会保留在全局记忆里。"
                    )
                )
                break
            protocol_blocks = AgentExecutor.extract_protocol_blocks(response)
            if not protocol_blocks:
                break  # AI 没有请求任何操作
            
            operation_iteration = agent_iteration + 1
            round_state = AgentRoundState()
            provider_api_format = str(prov_data.get('api_format', 'openai'))
            agent_stop_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🛠️ 第 {operation_iteration} 轮Agent操作进行中...",
                reply_markup=build_stop_keyboard()
            )

            for block in protocol_blocks:
                if is_stop_requested():
                    round_state.should_continue = False
                    break
                block_type = block['type']

                standard_operation = await dispatch_standard_protocol(
                    block,
                    executor=AgentExecutor,
                    provider_api_format=provider_api_format,
                    stop_event_factory=get_or_create_stop_event,
                    logger=logger,
                )
                if standard_operation is not None:
                    operation_notice = standard_operation['notice']
                    operation_presentation = build_standard_operation_presentation(
                        standard_operation
                    )
                    if operation_presentation is not None:
                        await safe_send_message(
                            context,
                            update.effective_chat.id,
                            operation_presentation,
                            parse_mode=constants.ParseMode.HTML,
                        )
                    await persist_standard_operation_result(
                        recorder=GlobalRecorder,
                        message_type=MessageType.AGENT_RESULT,
                        database=db,
                        conversation_id=cid,
                        chat_id=update.effective_chat.id,
                        operation=standard_operation,
                    )
                    round_state.add_context(standard_operation['context_message'])
                    round_state.should_continue = True
                    continue

                if block_type == 'sendfile':
                    sendfile_notice = await execute_sendfile_protocol(
                        block['body'],
                        executor=AgentExecutor,
                        context=context,
                        chat_id=update.effective_chat.id,
                        api_base_url=BotConfig.API_BASE_URL,
                        local_api_host_data_dir=_LOCAL_API_HOST_DATA_DIR,
                        local_api_container_data_dir=_LOCAL_API_CONTAINER_DATA_DIR,
                        max_file_size=AgentExecutor.MAX_FILE_SIZE,
                        safe_send_message=safe_send_message,
                        safe_text=safe_text,
                        logger=logger,
                        cancel_task_quietly=cancel_task_quietly,
                    )
                    if sendfile_notice:
                        await persist_agent_result(
                            recorder=GlobalRecorder,
                            message_type=MessageType.AGENT_RESULT,
                            database=db,
                            conversation_id=cid,
                            chat_id=update.effective_chat.id,
                            notice=sendfile_notice,
                        )
                        round_state.add_context(
                            build_sendfile_context_message(sendfile_notice)
                        )
                        round_state.should_continue = True
                    continue

                if block_type == 'file':
                    filename = block['path']
                    file_notice = ""
                    try:
                        written_file = await write_text_protocol_file(
                            block, executor=AgentExecutor
                        )
                        file_notice = await send_written_agent_file(
                            written_file,
                            protocol="file",
                            context=context,
                            chat_id=update.effective_chat.id,
                            max_file_size=AgentExecutor.MAX_FILE_SIZE,
                            safe_send_message=safe_send_message,
                            safe_text=safe_text,
                            html_parse_mode=constants.ParseMode.HTML,
                        )
                    except Exception as e:
                        logger.error(f"Agent写入文件失败: {e}")
                        file_notice = f"[file结果] 写入失败: {filename}。错误: {str(e)[:200]}"
                        await safe_send_message(
                            context,
                            update.effective_chat.id,
                            f"❌ 文件写入失败: {safe_text(str(e)[:200])}"
                        )

                    if file_notice:
                        await persist_agent_result(
                            recorder=GlobalRecorder,
                            message_type=MessageType.AGENT_RESULT,
                            database=db,
                            conversation_id=cid,
                            chat_id=update.effective_chat.id,
                            notice=file_notice,
                        )
                        round_state.add_context(
                            build_file_context_message(file_notice)
                        )
                        round_state.should_continue = True
                    continue

                if block_type == 'file_base64':
                    filename = block['path']
                    file_notice = ""
                    try:
                        written_file = await write_base64_protocol_file(
                            block, executor=AgentExecutor
                        )
                        file_notice = await send_written_agent_file(
                            written_file,
                            protocol="file:base64",
                            context=context,
                            chat_id=update.effective_chat.id,
                            max_file_size=AgentExecutor.MAX_FILE_SIZE,
                            safe_send_message=safe_send_message,
                            safe_text=safe_text,
                            html_parse_mode=constants.ParseMode.HTML,
                        )
                    except Exception as e:
                        logger.error(f"Agent base64 写入文件失败: {e}")
                        file_notice = f"[file:base64结果] 写入失败: {filename}。错误: {str(e)[:200]}"
                        await safe_send_message(
                            context,
                            update.effective_chat.id,
                            f"❌ base64 文件写入失败: {safe_text(str(e)[:200])}"
                        )

                    if file_notice:
                        await persist_agent_result(
                            recorder=GlobalRecorder,
                            message_type=MessageType.AGENT_RESULT,
                            database=db,
                            conversation_id=cid,
                            chat_id=update.effective_chat.id,
                            notice=file_notice,
                        )
                        round_state.add_context(
                            build_file_context_message(file_notice, protocol="file:base64")
                        )
                        round_state.should_continue = True
                    continue

                if block_type == 'trigger':
                    trigger_notice = await execute_trigger_protocol(
                        block,
                        trigger_manager=SelfTriggerManager,
                        bot=context.bot,
                        chat_id=update.effective_chat.id,
                        conversation_id=cid,
                        original_text=text,
                        response=response,
                    )
                    await persist_agent_result(
                        recorder=GlobalRecorder,
                        message_type=MessageType.AGENT_RESULT,
                        database=db,
                        conversation_id=cid,
                        chat_id=update.effective_chat.id,
                        notice=trigger_notice,
                    )
                    round_state.add_context(
                        build_trigger_context_message(trigger_notice)
                    )
                    round_state.should_continue = True
                    continue

                if block_type in {'shell', 'stdin', 'shellread', 'shellkill'}:
                    shell_execution = await execute_shell_protocol(
                        block,
                        shell_manager=AgentShellSessionManager,
                        executor=AgentExecutor,
                        stop_event_factory=get_or_create_stop_event,
                        stop_requested=is_stop_requested,
                    )
                    shell_result = shell_execution['result']
                    session_id = shell_execution['session_id']
                    command = shell_execution['command']
                    output = shell_execution['output']
                    display_output = format_shell_display_output(
                        output, bool(shell_result.get('running'))
                    )

                    action_label = {
                        'shell': '启动会话',
                        'stdin': '输入会话',
                        'shellread': '读取会话',
                        'shellkill': '关闭会话',
                    }[block_type]
                    pause_note = ""
                    pause_display_text, round_state.pause_message = get_shell_pause_messages(
                        str(shell_result.get('pause_reason') or '')
                    )
                    if shell_result.get('running'):
                        pause_note = "\n" + pause_display_text

                    await safe_send_message(
                        context,
                        update.effective_chat.id,
                        build_shell_presentation(
                            action_label=action_label,
                            shell_result=shell_result,
                            session_id=session_id,
                            display_output=display_output,
                            pause_note=pause_note,
                        ),
                        parse_mode=constants.ParseMode.HTML
                    )

                    shell_notice = build_shell_notice(
                        action_label,
                        shell_result,
                        session_id,
                        command,
                        output
                    )
                    await persist_agent_result(
                        recorder=GlobalRecorder,
                        message_type=MessageType.AGENT_RESULT,
                        database=db,
                        conversation_id=cid,
                        chat_id=update.effective_chat.id,
                        notice=shell_notice,
                    )
                    if shell_result.get('running'):
                        round_state.add_context(
                            build_shell_context_message(shell_notice, running=True)
                        )
                        round_state.should_continue = True
                        break
                    round_state.add_context(
                        build_shell_context_message(shell_notice, running=False)
                    )
                    round_state.should_continue = True
                    continue

                if block_type == 'media':
                    media_prompt = block['body']
                    await GlobalRecorder.record(
                        msg_type=MessageType.AGENT_CMD,
                        role='system',
                        content=f"[Agent媒体生成] {media_prompt}",
                        chat_id=update.effective_chat.id
                    )

                    media_execution = await execute_media_generation(
                        media_prompt,
                        context=context,
                        chat_id=update.effective_chat.id,
                        generate_media=run_default_media_generation,
                        keep_typing=keep_typing_while_waiting,
                        stop_event_factory=get_or_create_stop_event,
                        stop_requested=is_stop_requested,
                        build_stop_keyboard=build_stop_keyboard,
                        safe_edit_text=safe_edit_text,
                        cancel_task_quietly=cancel_task_quietly,
                    )
                    if media_execution['stopped']:
                        round_state.should_continue = False
                        break
                    media_result = media_execution['result']

                    media_notice, media_artifacts = build_external_media_output(media_result, media_prompt)

                    await send_media_generation_result(
                        media_result,
                        media_artifacts,
                        media_notice,
                        context=context,
                        chat_id=update.effective_chat.id,
                        send_artifacts=send_generated_media_artifacts,
                        safe_send_message=safe_send_message,
                        safe_text=safe_text,
                        logger=logger,
                    )

                    await persist_media_result(
                        recorder=GlobalRecorder,
                        database=db,
                        conversation_id=cid,
                        chat_id=update.effective_chat.id,
                        notice=media_notice,
                    )
                    round_state.add_context(build_media_continuation_message(media_result, media_prompt))
                    round_state.should_continue = True
            
            round_decision = plan_agent_round_transition(
                round_state,
                stop_requested=is_stop_requested(),
                agent_turn_history=agent_turn_history,
            )
            if round_decision['send_stop_notice']:
                await safe_send_message(
                    context,
                    update.effective_chat.id,
                    (
                        "⏹️ 已停止当前回合，后续 Agent 操作不会继续。\n"
                        "已经产生的工具结果会保留在全局记忆里。"
                    )
                )

            if round_decision['status_text'] is not None:
                with contextlib.suppress(Exception):
                    await safe_edit_text(
                        agent_stop_msg,
                        round_decision['status_text'],
                        reply_markup=None,
                    )

            if round_decision['show_completion_status']:
                with contextlib.suppress(Exception):
                    await safe_edit_text(
                        agent_stop_msg,
                        f"🛠️ 第 {operation_iteration} 轮Agent操作完成，正在整理结果...",
                        reply_markup=None,
                    )

            if not round_decision['continue_loop']:
                break

            # Continue from this turn's in-memory transcript. Re-reading global history here would
            # duplicate just-recorded Agent results and make prompt caching worse.
            agent_iteration = await _reserve_agent_turn_iteration(db)
            if agent_iteration > max_agent_iterations:
                await _send_agent_iteration_limit_notice(
                    context,
                    update.effective_chat.id,
                    agent_iteration,
                    max_agent_iterations,
                )
                break
            next_history = round_decision['next_history']

            if stream_mode:
                response = await send_streaming_response(
                    update, context,
                    prov_name, prov_data, model,
                    system_prompt, next_history
                )
            else:
                response = await send_non_streaming_response(
                    update, context,
                    prov_name, prov_data, model,
                    system_prompt, next_history
                )
            
            if not response:
                break

            await GlobalRecorder.record_ai_reply(response, update.effective_chat.id)
            await db.add_chat_message(cid, 'assistant', response)
            agent_turn_history = next_history
            agent_turn_history.append({'role': 'assistant', 'content': response})

# --- ☆ 命令函数 ☆ ---
