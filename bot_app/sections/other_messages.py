# This file is executed by bot_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

async def handle_photo_message_legacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    
    await UserDataManager.init()
    
    caption = update.message.caption or ""
    photo_desc = f"[图片]{': ' + caption if caption else ''}"
    
    await GlobalRecorder.record_user_message(
        photo_desc,
        MessageType.USER_PHOTO,
        update.effective_chat.id
    )
    
    # 如果有文字说明，转发给AI处理
    if caption:
        prov_name, prov_data = get_current_provider()
        model = UserDataManager.get('default_model')
        if prov_data and model:
            await process_conversation(update, context, f"[用户发送了一张图片，附言: {caption}]")
            return
    
    await update.message.reply_text("📷 图片已收到。如需模型处理，请发送图片时附带文字说明。")

async def handle_photo_message_indexed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return

    await UserDataManager.init()

    caption = update.message.caption or ""
    photo_desc = f"[图片]{': ' + caption if caption else ''}"

    await GlobalRecorder.record_user_message(
        photo_desc,
        MessageType.USER_PHOTO,
        update.effective_chat.id
    )

    prov_name, prov_data = get_current_provider()
    model = UserDataManager.get('default_model')
    if not prov_data or not model:
        await update.message.reply_text("📷 图片已收到。请先配置提供商和默认对话模型，系统才能处理图片。")
        return

    try:
        largest_photo = update.message.photo[-1]
        photo_bytes = await download_telegram_file(largest_photo)
        image_b64 = base64.b64encode(bytes(photo_bytes)).decode('ascii')

        multimodal_content: List[Dict[str, str]] = []
        if caption.strip():
            multimodal_content.append({"type": "text", "text": caption.strip()})
        multimodal_content.append({
            "type": "image",
            "mime_type": "image/jpeg",
            "data": image_b64
        })

        await process_conversation(
            update,
            context,
            photo_desc,
            content_override=multimodal_content
        )
        return
    except Exception as e:
        logger.error(f"Photo multimodal processing error: {e}")
        await update.message.reply_text("图片已收到，但转给模型时失败。请稍后重试。")

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return

    await UserDataManager.init()

    caption = (update.message.caption or "").strip()
    # 转发的富文本消息：caption 可能为空，文字在 rich_message.blocks 里
    if not caption:
        caption = _extract_rich_message_text(update.message).strip()
        if caption:
            logger.warning(f"handle_photo_message: extracted caption via rich_message, len={len(caption)}: {caption[:200]}")
    prov_name, prov_data = get_current_provider()
    model = UserDataManager.get('default_model')
    if not prov_data or not model:
        await update.message.reply_text("📷 图片已收到。请先配置提供商和默认对话模型，系统才能处理图片。")
        return

    try:
        largest_photo = update.message.photo[-1]
        photo_bytes = await download_telegram_file(largest_photo)
        saved_photo = ArtifactManager.save_binary_upload("telegram_photo.jpg", photo_bytes)
        image_b64 = base64.b64encode(photo_bytes).decode('ascii')
        memory_text = ArtifactManager.build_index_message(
            "图片",
            "telegram_photo.jpg",
            saved_photo['rel_path'],
            ArtifactManager.shorten_text(caption, 80) if caption else ""
        )

        await GlobalRecorder.record_user_message(
            memory_text,
            MessageType.USER_PHOTO,
            update.effective_chat.id
        )

        multimodal_content: List[Dict[str, str]] = []
        if caption:
            multimodal_content.append({"type": "text", "text": f"用户附言：{caption}"})
        multimodal_content.append({
            "type": "text",
            "text": ArtifactManager.build_saved_notice("图片", saved_photo['rel_path'])
        })
        multimodal_content.append({
            "type": "image",
            "mime_type": "image/jpeg",
            "data": image_b64
        })

        context_prefix = build_incoming_context_prefix(update.message)
        if context_prefix:
            memory_text = f"{context_prefix}\n{memory_text}"
            multimodal_content.insert(0, {"type": "text", "text": context_prefix})

        await process_conversation(
            update,
            context,
            memory_text,
            content_override=multimodal_content
        )
    except Exception as e:
        logger.error(f"Photo multimodal processing error: {e}")
        await update.message.reply_text("图片已收到，但保存或转给模型时失败。")

async def handle_sticker_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return
    
    await UserDataManager.init()
    sticker = update.message.sticker
    emoji = sticker.emoji or ''
    set_name = sticker.set_name or ''
    sticker_desc = f"[贴纸] {emoji}" + (f" ({set_name})" if set_name else "")
    
    await GlobalRecorder.record_user_message(
        sticker_desc,
        MessageType.USER_STICKER,
        update.effective_chat.id
    )
    
    # 尝试用AI回复贴纸
    prov_name, prov_data = get_current_provider()
    model = UserDataManager.get('default_model')
    if prov_data and model and emoji:
        sticker_conv_text = f"[用户发送了一个贴纸: {emoji}]"
        context_prefix = build_incoming_context_prefix(update.message)
        if context_prefix:
            sticker_conv_text = f"{context_prefix}\n{sticker_conv_text}"
        await process_conversation(update, context, sticker_conv_text)
    else:
        await update.message.reply_text(f"已收到贴纸 {emoji} ")


def build_forward_origin_prefix(msg) -> str:
    """从消息中提取转发来源信息，返回前缀字符串。非转发消息返回空字符串。"""
    origin = getattr(msg, 'forward_origin', None)

    # 旧版 API 兼容：forward_from / forward_from_chat
    if not origin:
        ff = getattr(msg, 'forward_from', None)
        ffc = getattr(msg, 'forward_from_chat', None)
        if ff:
            name = ff.full_name or ff.first_name or ff.username or "未知用户"
            return f"[转发消息，来源：{name}]"
        if ffc:
            name = ffc.title or ffc.username or "未知聊天"
            return f"[转发消息，来源：{name}]"
        if getattr(msg, 'forward_date', None):
            return "[转发消息，来源：未知]"
        return ""

    origin_type = getattr(origin, 'type', '')

    if origin_type == 'user':
        sender = getattr(origin, 'sender_user', None)
        name = (sender.full_name or sender.first_name or sender.username or "未知用户") if sender else "未知用户"
        return f"[转发消息，来源：{name}]"

    if origin_type == 'hidden_user':
        name = getattr(origin, 'sender_user_name', None) or "隐藏用户"
        return f"[转发消息，来源：{name}]"

    if origin_type == 'chat':
        sender = getattr(origin, 'sender_chat', None)
        name = (sender.title or sender.username or "未知聊天") if sender else "未知聊天"
        sig = getattr(origin, 'author_signature', None)
        if sig:
            name += f"（作者：{sig}）"
        return f"[转发消息，来源：{name}]"

    if origin_type == 'channel':
        chat = getattr(origin, 'chat', None)
        name = (chat.title or chat.username or "未知频道") if chat else "未知频道"
        sig = getattr(origin, 'author_signature', None)
        if sig:
            name += f"（作者：{sig}）"
        return f"[转发消息，来源：{name}]"

    return "[转发消息]"


def _truncate_context_preview(text: str, limit: int) -> str:
    """截断上下文预览文本，超长时追加省略号。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _describe_message_media(m) -> str:
    """为无文字消息生成简短的媒体类型标签。"""
    if m is None:
        return "[非文字消息]"
    if getattr(m, 'photo', None):
        return "[图片]"
    document = getattr(m, 'document', None)
    if document is not None:
        file_name = getattr(document, 'file_name', None)
        return f"[文件：{file_name}]" if file_name else "[文件]"
    sticker = getattr(m, 'sticker', None)
    if sticker is not None:
        emoji = getattr(sticker, 'emoji', None) or ""
        return f"[贴纸 {emoji}]".rstrip()
    for attr, label in (
        ('video', '[视频]'),
        ('voice', '[语音]'),
        ('audio', '[音频]'),
        ('animation', '[动图]'),
        ('video_note', '[视频消息]'),
        ('location', '[位置]'),
        ('venue', '[地点]'),
        ('contact', '[联系人]'),
        ('poll', '[投票]'),
        ('story', '[快拍]'),
        ('game', '[游戏]'),
    ):
        if getattr(m, attr, None):
            return label
    return "[非文字消息]"


def _sender_display_name(m) -> str:
    """取消息发送者的可读名称（用户或频道/群组）。"""
    for attr in ('from_user', 'sender_chat'):
        sender = getattr(m, attr, None)
        if sender is None:
            continue
        name = (
            getattr(sender, 'full_name', None)
            or getattr(sender, 'first_name', None)
            or getattr(sender, 'title', None)
            or getattr(sender, 'username', None)
        )
        if name:
            return str(name)
    return "对方"


def _describe_external_reply_origin(origin) -> str:
    """从 MessageOrigin 提取来源标签，用于跨聊天引用回复。"""
    origin_type = getattr(origin, 'type', '')
    if origin_type == 'user':
        sender = getattr(origin, 'sender_user', None)
        name = (
            (getattr(sender, 'full_name', None) or getattr(sender, 'first_name', None)
             or getattr(sender, 'username', None)) if sender else None
        )
        return name or "未知用户"
    if origin_type == 'hidden_user':
        return getattr(origin, 'sender_user_name', None) or "隐藏用户"
    if origin_type in ('chat', 'channel'):
        chat = getattr(origin, 'sender_chat', None) or getattr(origin, 'chat', None)
        name = (getattr(chat, 'title', None) or getattr(chat, 'username', None)) if chat else None
        return name or "未知聊天"
    return "未知来源"


def build_reply_context_prefix(msg) -> str:
    """提取引用回复上下文，返回前缀字符串。非回复消息返回空字符串。

    覆盖：直接回复（被回复消息正文截断预览或媒体类型标签）、
    部分引用（quote 片段）与跨聊天引用（external_reply，仅来源与媒体类型，
    Bot API 不向其提供正文）。
    """
    reply = getattr(msg, 'reply_to_message', None)
    external = getattr(msg, 'external_reply', None)
    if reply is None and external is None:
        return ""

    lines: List[str] = []
    if reply is not None:
        name = _sender_display_name(reply)
        content = (getattr(reply, 'text', None) or getattr(reply, 'caption', None) or "").strip()
        preview = _truncate_context_preview(content, 500) if content else _describe_message_media(reply)
        lines.append(f"[回复 {name} 的消息：{preview}]")
    else:
        origin_label = _describe_external_reply_origin(getattr(external, 'origin', None))
        media_label = _describe_message_media(external)
        lines.append(f"[回复来自 {origin_label} 的消息：{media_label}]")

    quote = getattr(msg, 'quote', None)
    quote_text = _truncate_context_preview(getattr(quote, 'text', None) or "", 200)
    if quote_text:
        lines.append(f"[引用片段：{quote_text}]")

    return "\n".join(lines)


def build_incoming_context_prefix(msg) -> str:
    """组合转发来源与引用回复上下文前缀，供所有消息入口统一使用。"""
    parts: List[str] = []
    forward_prefix = build_forward_origin_prefix(msg)
    if forward_prefix:
        parts.append(forward_prefix)
    reply_prefix = build_reply_context_prefix(msg)
    if reply_prefix:
        parts.append(reply_prefix)
    return "\n".join(parts)


async def handle_other_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_authorized_user_middleware(update, context):
        return

    await UserDataManager.init()
    msg = update.message

    # 完整原始字典和 api_kwargs 写入日志
    try:
        msg_dict = msg.to_dict() if hasattr(msg, 'to_dict') else {}
        logger.warning(f"handle_other_message raw dict: {msg_dict}")
        extra = getattr(msg, 'api_kwargs', None) or {}
        if extra:
            logger.warning(f"handle_other_message api_kwargs keys: {list(extra.keys())}")
            for k, v in extra.items():
                if isinstance(v, str) and len(v) > 3:
                    logger.warning(f"handle_other_message api_kwargs['{k}'] = {v[:200]}")
    except Exception:
        msg_dict = {}

    # 转发消息优先用 forwardMessage API 获取完整内容
    # （api_kwargs 可能有短字符串导致提前命中，forwardMessage 必须最先尝试）
    is_forwarded = (
        getattr(msg, 'forward_origin', None) is not None
        or getattr(msg, 'forward_from', None) is not None
        or getattr(msg, 'forward_from_chat', None) is not None
    )
    extracted_text = ""

    if is_forwarded:
        try:
            fwd_msg = await context.bot.forward_message(
                chat_id=update.effective_chat.id,
                from_chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                disable_notification=True
            )
            extracted_text = (getattr(fwd_msg, 'text', None) or getattr(fwd_msg, 'caption', None) or "").strip()
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=fwd_msg.message_id
                )
            except Exception:
                pass
            if extracted_text:
                logger.warning(f"handle_other_message: extracted via forwardMessage (first), len={len(extracted_text)}: {extracted_text[:200]}")
            else:
                logger.warning("handle_other_message: forwardMessage returned but no text/caption found")
        except Exception as e:
            logger.warning(f"handle_other_message: forwardMessage failed: {e}")

    # 标准提取（forwardMessage 失败或非转发消息的 fallback）
    if not extracted_text:
        extracted_text = (msg.text or msg.caption or "").strip()

    if not extracted_text:
        extra = getattr(msg, 'api_kwargs', None) or {}
        extracted_text = (extra.get('text') or extra.get('caption') or "").strip()

    if not extracted_text:
        extracted_text = (msg_dict.get('text') or msg_dict.get('caption') or "").strip()

    # 富文本消息：text 在 rich_message.blocks 结构里
    if not extracted_text:
        extracted_text = _extract_rich_message_text(msg).strip()
        if extracted_text:
            logger.warning(f"handle_other_message: extracted via rich_message.blocks, len={len(extracted_text)}: {extracted_text[:200]}")

    if not extracted_text:
        # 深度搜索：遍历 api_kwargs 的所有键值，找最长的字符串（最可能是消息正文）
        extra = getattr(msg, 'api_kwargs', None) or {}
        _SKIP_KEYS = {'date', 'message_id', 'chat_id', 'from_user_id', 'update_id',
                       'id', 'file_id', 'file_unique_id', 'mime_type', 'file_size',
                       'width', 'height', 'duration', 'is_bot', 'language_code',
                       'username', 'phone_number', 'color', 'type',
                       'is_topic_message', 'is_automatic_forward',
                       'has_protected_content', 'message_thread_id',
                       'chat', 'from', 'from_user', 'forward_origin',
                       'forward_from', 'forward_from_chat', 'forward_date',
                       'sender_chat', 'sender_user', 'entities',
                       'caption_entities', 'new_chat_members',
                       'new_chat_photo', 'left_chat_member',
                       'photo', 'reply_to_message', 'pinned_message',
                       'via_bot', 'author'}
        # 记录所有候选字符串
        for k, v in extra.items():
            if isinstance(v, str) and len(v) > 3:
                logger.warning(f"handle_other_message candidate api_kwargs['{k}'] len={len(v)}: {v[:100]}")
        # 找最长的字符串
        best_text = ""
        best_key = ""
        for k, v in extra.items():
            if k in _SKIP_KEYS:
                continue
            if isinstance(v, str) and len(v) > 10 and len(v) > len(best_text):
                best_text = v
                best_key = k
        if best_text:
            extracted_text = best_text
            logger.warning(f"handle_other_message: extracted from api_kwargs['{best_key}'], len={len(best_text)}")

    if not extracted_text:
        # 递归深度搜索 to_dict() 的所有值，找最长的字符串
        def _deep_search_longest(obj, depth=0) -> Optional[str]:
            if depth > 5:
                return None
            best = None
            if isinstance(obj, str) and len(obj) > 10:
                best = obj
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in _SKIP_KEYS:
                        continue
                    result = _deep_search_longest(v, depth + 1)
                    if result and (best is None or len(result) > len(best)):
                        best = result
            if isinstance(obj, list):
                for item in obj:
                    result = _deep_search_longest(item, depth + 1)
                    if result and (best is None or len(result) > len(best)):
                        best = result
            return best

        found = _deep_search_longest(msg_dict)
        if found:
            extracted_text = found
            logger.warning(f"handle_other_message: extracted via deep search, len={len(found)}: {found[:200]}")

    if not extracted_text:
        # 检查嵌套消息
        for nested_key in ('pinned_message', 'reply_to_message'):
            nested = getattr(msg, nested_key, None)
            if nested:
                nested_text = (getattr(nested, 'text', None) or getattr(nested, 'caption', None) or "").strip()
                if nested_text:
                    extracted_text = nested_text
                    break

    if not extracted_text:
        # 最后手段：用 forwardMessage API 重新获取消息内容
        # Bot API 的 update 可能没有 text 字段，但 forwardMessage 返回的 Message 可能有
        try:
            fwd_msg = await context.bot.forward_message(
                chat_id=update.effective_chat.id,
                from_chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                disable_notification=True
            )
            fwd_text = (getattr(fwd_msg, 'text', None) or getattr(fwd_msg, 'caption', None) or "").strip()
            # 立即删除转发的副本
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=fwd_msg.message_id
                )
            except Exception:
                pass
            if fwd_text:
                extracted_text = fwd_text
                logger.warning(f"handle_other_message: extracted via forwardMessage, len={len(fwd_text)}: {fwd_text[:200]}")
            else:
                logger.warning("handle_other_message: forwardMessage returned but no text/caption found")
        except Exception as e:
            logger.warning(f"handle_other_message: forwardMessage fallback failed: {e}")

    if extracted_text:
        context_prefix = build_incoming_context_prefix(msg)
        if context_prefix:
            extracted_text = f"{context_prefix}\n{extracted_text}"
        if _conversation_processing_lock.locked():
            await msg.reply_text("⏳ 系统仍在处理上一个请求... 请稍后再发送新请求。")
            return
        await handle_normal_text_conversation(update, context, extracted_text)
        return

    # 无法提取文字内容
    context_prefix = build_incoming_context_prefix(msg)
    if context_prefix:
        # 转发/回复消息但无法提取文字：仍然送入对话流程，让 AI 自然回复
        conv_text = f"{context_prefix}（此消息未包含可读取的文字内容）"
        if _conversation_processing_lock.locked():
            await msg.reply_text("⏳ 系统仍在处理上一个请求... 请稍后再发送新请求。")
            return
        await GlobalRecorder.record_user_message(conv_text, MessageType.USER_TEXT, update.effective_chat.id)
        await process_conversation(update, context, conv_text)
    else:
        # 非转发消息，确实没有文字内容
        await GlobalRecorder.record_user_message(
            "[其他类型消息]",
            MessageType.USER_TEXT,
            update.effective_chat.id
        )
        await msg.reply_text("已收到该类型消息。目前建议发送文字或文件。")

# --- ☆ 错误处理 ☆ ---
