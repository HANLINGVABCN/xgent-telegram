"""Display-only history and durable, authenticated attachment descriptors."""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from xgent_app.web_media import generated_media_group_id


_UPLOAD = re.compile(
    r"^(?:\u7b2c\d+\u5f20: )?\[(?:\u6587\u4ef6|\u56fe\u7247)\] (?P<name>.+?)"
    r"\uff0c\u5df2\u4fdd\u5b58\u5230 (?P<path>.+?)(?:\u3002\u8bf4\u660e\uff1a.*)?$"
)
_GENERATED = re.compile(
    r"^\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a"
    r"\u672c(?:\u56fe\u7247|\u89c6\u9891|\u97f3\u9891)\u5df2\u81ea\u52a8\u5b58\u5165 "
    r"(?P<path>.+?)\uff0c(?:\u9700\u8981\u65f6\u8bf7read|"
    r"\u539f\u56fe\u81ea\u52a8\u8fdb\u5165).*\u3011$",
    re.MULTILINE,
)
_DELIVERED = re.compile(
    r"^\[(?:sendfile|file(?::base64)?)\u7ed3\u679c\] "
    r"\u5df2(?:\u53d1\u9001\u670d\u52a1\u5668\u6587\u4ef6\u7ed9\u7528\u6237|"
    r"\u5199\u5165\u670d\u52a1\u5668\u6587\u4ef6|加入 Telegram 发送队列): "
    r"(?P<path>.+) \(\d+ bytes(?:, [^)\r\n]+)?\)(?: \[\u672c\u5730API\u76f4\u53d1(?:；网络恢复后自动补发)?\])?$"
)
_EXPORT = re.compile(
    r"\u670d\u52a1\u5668\u6587\u4ef6\u8def\u5f84\uff1a(?P<path>.+?)"
    r"\uff08\d+ bytes\uff09"
)
_HTML_PRESENTATION = re.compile(r"<(?:b|i|pre|code|blockquote)(?:\s|>)", re.I)
_CONFIG_PREFIXES = (
    "[\u63d0\u4f9b\u5546\u914d\u7f6e\u6587\u4ef6]",
    "[\u9ed1\u540d\u5355\u6587\u4ef6]",
    "[\u8bb0\u5fc6\u6587\u4ef6]",
)
_GENERATED_PREFIX = "\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a\u672c"
_FILE_TYPES = {"user_file", "user_photo", "ai_reply", "media_reply", "agent_result", "system_op"}
_MISSING = "\u539f\u4ef6\u4e0d\u5b58\u5728\u6216\u65e0\u6cd5\u8bfb\u53d6"
_INVALID = "\u9644\u4ef6\u5173\u8054\u65e0\u6548"


def media_kind(mime_type: str) -> str:
    # Active formats must be downloaded, not embedded on the app's origin.
    if not isinstance(mime_type, str):
        raise ValueError(_INVALID)
    mime_type = mime_type.lower()
    if mime_type.startswith("image/") and mime_type != "image/svg+xml":
        return "photo"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return "file"


def display_media_reference(
    path: str, name: str | None = None, mime_type: str | None = None,
) -> dict[str, Any]:
    """Authorize exactly the file that a trusted operation saved or delivered."""
    target = Path(path).resolve()
    filename = name or target.name
    return {
        "version": 1, "path": str(target), "name": filename,
        "mime_type": mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
    }


def delivered_file_reference(notice: str) -> list[dict[str, Any]]:
    """Only complete operation results authorize files outside app storage."""
    match = _DELIVERED.fullmatch(notice)
    if not match or not Path(match["path"]).is_absolute():
        return []
    return [display_media_reference(match["path"])]


def _within(path: str, root: Path) -> Path:
    root = root.resolve()
    from .data_paths import resolve_stored_path
    target = resolve_stored_path(path)
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("\u8def\u5f84\u4e0d\u5728\u5141\u8bb8\u7684\u76ee\u5f55\u5185")
    return target


def _legacy_media(record: dict, storage: Path, workspace: Path) -> list[dict]:
    kind, content = record.get("msg_type"), str(record.get("content") or "")
    refs = []
    if kind in {"user_file", "user_photo"}:
        for line in content.splitlines():
            match = _UPLOAD.fullmatch(line)
            if match:
                refs.append({
                    "path": match["path"], "name": match["name"], "_root": storage / "uploads",
                    "_legacy_pattern": r"\d{6}_[0-9a-f]{8}_.+",
                })
    elif kind in {"ai_reply", "media_reply"}:
        for match in _GENERATED.finditer(content):
            refs.append({
                "path": match["path"], "_root": storage / "generated_media",
                "_legacy_pattern": r"\d{6}_[0-9a-f]{8}_assistant_(?:image|video|audio)\.[a-zA-Z0-9]+",
            })
    elif kind == "agent_result":
        match = _DELIVERED.fullmatch(content)
        if match:
            refs.append({"path": match["path"], "_roots": (storage, workspace)})
    elif kind == "system_op" and "\u5df2\u6210\u529f\u5bfc\u51fa" in content:
        for match in _EXPORT.finditer(content):
            refs.append({
                "path": match["path"], "_root": storage / "exports",
                "_legacy_pattern": r"\d{6}_[0-9a-f]{8}_.+",
                "name": "\u7cfb\u7edf\u8bb0\u5fc6.zip" if match["path"].endswith(".zip") else None,
            })
    return refs


def _describe_media(ref: Any, storage: Path, *, attachment: bool) -> dict[str, Any]:
    item: dict[str, Any] = {"filename": "\u9644\u4ef6", "kind": "file"}
    try:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise ValueError(_INVALID)
        item["filename"] = str(ref.get("name") or Path(ref["path"]).name)
        if attachment:
            category = ref.get("storage", "uploads")
            if category not in {"uploads", "generated_media"}:
                raise ValueError(_INVALID)
            target = _within(ref["path"], storage / category)
        elif "_root" in ref:
            target = _within(ref["path"], ref["_root"])
        elif "_roots" in ref:
            target = None
            for root in ref["_roots"]:
                try:
                    target = _within(ref["path"], root)
                    break
                except ValueError:
                    pass
            if target is None:
                raise ValueError("\u65e7\u8bb0\u5f55\u8def\u5f84\u4e0d\u5728\u53ef\u6062\u590d\u76ee\u5f55\u5185")
        else:
            from .data_paths import resolve_stored_path
            target = resolve_stored_path(ref["path"])
            if ref.get("version") != 1 or not target.is_absolute() or target.resolve() != target:
                raise ValueError(_INVALID)
        if ref.get("_legacy_pattern"):
            relative = target.relative_to(ref["_root"].resolve())
            if (len(relative.parts) != 2
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative.parts[0])
                    or not re.fullmatch(ref["_legacy_pattern"], target.name)):
                raise ValueError("\u65e7\u9644\u4ef6\u8def\u5f84\u65e0\u6cd5\u9a8c\u8bc1")
        item["path"] = str(target)
        mime = ref.get("mime_type") or mimetypes.guess_type(item["filename"])[0] or "application/octet-stream"
        if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+", mime):
            raise ValueError(_INVALID)
        mime = mime.lower()
        item.update(mime_type=mime, kind=media_kind(mime))
        if isinstance(ref.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', ref['sha256']):
            item['sha256'] = ref['sha256']
        if not target.is_file():
            raise OSError(_MISSING)
        item["size"] = target.stat().st_size
        if item['kind'] == 'photo':
            width, height = ref.get('width'), ref.get('height')
            if not all(type(value) is int and 0 < value <= 1_000_000 for value in (width, height)):
                # Older records did not retain dimensions. Read only the image
                # header so history can reserve its space without downloading it.
                from PIL import Image
                try:
                    with Image.open(target) as image:
                        width, height = image.size
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                    width = height = None
            if all(type(value) is int and 0 < value <= 1_000_000 for value in (width, height)):
                item.update(width=width, height=height)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        item["error"] = str(exc) if isinstance(exc, ValueError) else _MISSING
    return item


def _file_message_text(content, metadata, storage):
    """Render all user file sources independently of the model's file index.

    Web batches and native Telegram files retain captions beside generated indexes.
    Remove only those exact suffixes; never strip arbitrary user path lines.
    """
    refs = [ref for key in ('attachments', 'server_uploads')
            for ref in (metadata.get(key) if isinstance(metadata.get(key), list) else [])
            if isinstance(ref, dict)]
    refs.sort(key=lambda ref: ref.get('order', 0) if type(ref.get('order', 0)) is int else 0)
    presentation = metadata.get('web_file_message')
    if isinstance(presentation, dict) and isinstance(presentation.get('text'), str):
        text = presentation['text']
    else:
        text = content
        for number, ref in reversed(list(enumerate(refs, 1))):
            try:
                target = _within(ref['path'], storage / 'uploads')
                if ref in metadata.get('server_uploads', []):
                    from .file_ingress import describe_file
                    candidates = [describe_file(ref, storage / 'uploads')]
                else:
                    caption = str(ref.get('caption') or '')
                    compact = ' '.join(caption.split())
                    short = compact[:80].rstrip() + '...' if len(compact) > 80 else compact
                    candidates = [f'[{kind}] {ref.get("name") or target.name}，已保存到 {path}'
                                  + (f'。说明：{note}' if note else '')
                                  for kind in ('文件', '图片') for path in {str(target), target.as_posix()}
                                  for note in {caption, short}]
                candidates += [f'第{number}{unit}: {candidate}' for candidate in list(candidates) for unit in ('张', '份')]
                for candidate in sorted(candidates, key=len, reverse=True):
                    if text == candidate:
                        text = ''
                        break
                    if text.endswith('\n\n' + candidate):
                        text = text[:-len(candidate) - 2]
                        break
                    if text.endswith('\n' + candidate):
                        text = text[:-len(candidate) - 1]
                        break
            except (ValueError, KeyError, TypeError):
                continue
        # Old merged albums have a generated heading after their indexes have
        # been removed. Keep any actual forward/reply context before it.
        heading = re.compile(rf'\[(?:相册|文件组)\] 共{len(refs)}(?:张图片|份附件)')
        if heading.fullmatch(text.rsplit('\n', 1)[-1]):
            text = text.rsplit('\n', 1)[0] if '\n' in text else ''
    captions = [(ref['caption'] if len(refs) == 1 else f'{ref.get("name") or "文件"}：{ref["caption"]}') for ref in refs
                if isinstance(ref.get('caption'), str) and ref['caption'] and ref['caption'] != text]
    return '\n\n'.join([text, *captions]) if text else '\n\n'.join(captions)


def _section_ns() -> dict | None:
    """运行中的服务端把所有 section 函数 exec 进 xgent_server（直接跑时是 __main__）
    的 globals。web_history 是叶子模块、不在那个共享命名空间里，按需从 sys.modules
    取回折叠要用的渲染器/开关；找不到（如纯单测、独立进程）返回 None → 回退不折叠
    （沿用历史行为，测试不受影响）。"""
    import sys
    for name in ("xgent_server", "__main__"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "markdown_to_telegram_html") \
                and hasattr(mod, "_should_hide_protocol_blocks"):
            return vars(mod)
    return None


def _folded_history_html(content: str) -> str | None:
    """折叠模式 ON 且正文含协议块时，返回折叠后的 Telegram-HTML；否则 None（照常走 markdown）。

    只改**显示**：DB 记录里的 content 仍是原始文本，翻页/刷新时按开关**实时**折叠——
    与 Telegram/CLI 三端同源（同一个 ProtocolParser.render_folded_html），落库/执行/
    镜像永远用完整原文。返回 None 表示"没得折叠/开关关着/取不到渲染器"，交回上层
    照常走 markdown。
    """
    if not content:
        return None
    ns = _section_ns()
    if ns is None:
        return None
    try:
        from xgent_app.protocols import ProtocolParser
        if not ns["_should_hide_protocol_blocks"]():
            return None
        folded = ProtocolParser.render_folded_html(
            content, prose_renderer=ns["markdown_to_telegram_html"], raw_copy=True)
    except Exception:
        return None
    # 无协议块时 render_folded_html 原样返回输入——那种情况让 markdown 渲染器
    # 处理更合适（它支持标题/列表/表格），别把纯文本硬塞进 HTML 通道。
    return folded if folded != content else None


def _history_display_html(content: str) -> str | None:
    """Match the live HTML renderer regardless of the protocol-fold preference.

    Folding controls protocol blocks, not whether prose is parsed by a different
    Markdown engine. Stored display metadata takes precedence over this legacy
    fallback; standalone readers without the app renderer still receive raw text.
    """
    if not content:
        return None
    folded = _folded_history_html(content)
    if folded is not None:
        return folded
    ns = _section_ns()
    if ns is None:
        return None
    try:
        return ns["markdown_to_telegram_html"](content)
    except Exception:
        return None


def history_web_display(content: str, display=None):
    """Reconstruct semantic prose for old records without changing frozen folding.

    Old display snapshots already encode whether protocol blocks were folded.
    Only records without a snapshot consult the current preference, as before.
    """
    from .web_display import build_web_display
    if isinstance(display, dict) and isinstance(display.get('content'), str):
        folded = '<blockquote expandable' in display['content']
    else:
        ns = _section_ns()
        folded = bool(ns and ns['_should_hide_protocol_blocks']())
    return build_web_display(content, fold=folded)


def apply_history_presentation(message, record, metadata, *, namespace=None, mode_override=None):
    """Use the live semantic renderers for dynamic history; snapshots are legacy fallback."""
    from .presentation import normalize_mode, ai_html, operation_html, footer_html, successful_status, metadata_dict, current_turn, legacy_operation
    from .agent_status import compact_agent_round_status
    from .web_display import build_web_display, build_operation_display
    ns = namespace if namespace is not None else _section_ns()
    presentation = metadata_dict(metadata.get('presentation'))
    kind, raw = record.get('msg_type'), str(record.get('content') or '')
    if isinstance(metadata.get('ask_form'),dict):
        from .agent_ask import PENDING_ASKS
        snapshot = metadata['ask_form']
        pending = PENDING_ASKS.get(snapshot.get('id'))
        if (pending and pending.conversation_id==record.get('session_id') and
                pending.generation==snapshot.get('generation')):
            snapshot = pending.web_snapshot()
        elif snapshot.get('status')=='pending':
            snapshot = {**snapshot, 'status':'expired'}
        message.update(ask_form=snapshot, content='📝 需要你的输入' if snapshot['status']=='pending' else
                       {'submitted':'表单已提交。','cancelled':'表单已取消。','expired':'表单已失效，请让 AI 重新发起。'}.get(snapshot['status'],'表单已结束。'),
                       parse_mode=None, presentation_mode='folded', reply_markup=[])
        message['web_display'] = build_web_display(message['content'],mode='folded',presentation=presentation)
        return
    caption = compact_agent_round_status(raw) if kind == 'agent_status' else None
    if caption:
        message.update(content=caption, parse_mode=None, presentation_mode='folded')
        message['web_display'] = build_web_display(caption, mode='folded', presentation=presentation)
        return
    if ns is None and not presentation:
        return
    mode = (normalize_mode(ns['UserDataManager'].get('presentation_mode', 'folded'))
            if ns and 'UserDataManager' in ns else normalize_mode(presentation.get('mode')))
    if mode_override is not None:
        mode = normalize_mode(mode_override)
    turn = current_turn()
    if turn is not None:
        mode = turn.mode
    if kind == 'agent_result' and not presentation.get('operation'):
        recovered = legacy_operation(raw, metadata)
        if recovered is not None:
            presentation['operation'] = recovered
    message['presentation_mode'] = mode
    if kind == 'token_usage' or mode == 'folded' and (successful_status(raw)
            or raw.startswith('系统提示：⚠️ 协议块 nonce')
            or kind == 'agent_status' and presentation.get('technical')
            and not any(word in raw for word in ('停止', '失败', '超过', '超限', '等待填写'))):
        message['hidden'] = True
        return
    renderer = ns.get('markdown_to_telegram_html') if ns else None
    if kind in {'ai_reply', 'media_reply'} and renderer:
        content = ai_html(raw, mode, renderer)
        footer = footer_html(presentation)
        message.update(content=content + ('\n\n' + footer if footer else ''), parse_mode='HTML')
        message['web_display'] = build_web_display(raw, fold=True, mode=mode,
            presentation={**presentation, 'default_protocol_state': 'pending' if presentation.get('execution_schema') else 'unknown'})
    elif kind == 'agent_result' and isinstance(presentation.get('operation'), dict):
        operation = presentation['operation']
        content = operation_html(operation, detailed=mode == 'detailed')
        footer = footer_html(presentation) if mode == 'folded' else ''
        message.update(content=content + ('\n\n' + footer if footer else ''), parse_mode='HTML')
        details = '\n'.join(f'{label}：{operation[key]}' for key, label in (
            ('return_code', '返回码'), ('elapsed_seconds', '耗时（秒）'),
            ('session_id', '终端会话'), ('output_path', '输出存档'), ('notice', '执行说明'))
            if operation.get(key) is not None)
        message['web_display'] = build_operation_display(operation, mode, presentation)
        if operation.get('output_path') and record.get('id'):
            message['reply_markup'] = [[{'text': '完整输出', 'callback_data': f'present_output:{record["id"]}'}]]
    elif (kind == 'presentation_note' or kind == 'agent_status' and presentation.get('summary')) and renderer:
        message.update(content=renderer(raw), parse_mode='HTML')
        footer = footer_html(presentation)
        if footer:
            message['content'] += '\n\n' + footer
        message['web_display'] = build_web_display(raw, fold=True, mode=mode, presentation=presentation)
    if mode == 'folded' and presentation.get('usage') and record.get('id'):
        message.setdefault('reply_markup', []).append([{
            'text': '用量详情', 'callback_data': f'present_usage:{record["id"]}:1'}])


def build_history_message(
    record: dict[str, Any], storage_root: str | Path, workspace_root: str | Path,
    *, presentation_namespace=None, presentation_mode=None,
) -> dict[str, Any]:
    """No Base64 or short-lived download tokens enter the history response."""
    kind = record.get("msg_type")
    if kind == 'ui_message':
        return dict(record)
    role = record.get("role") or "user"
    if kind in {"agent_result", "media_reply", "agent_status", "runtime_error"}:
        role = "assistant"
    elif kind == "token_usage":
        role = "system"
    content = str(record.get("content") or "")
    message = {
        "id": record.get("id"), "timestamp": record.get("timestamp"), "msg_type": kind,
        "role": role, "content": content, "media": [],
    }
    if record.get('turn_id'):
        message['turn_id'] = record['turn_id']
    if record.get('source_record_id'):
        message['source_record_id'] = record['source_record_id']
    if record.get('session_id'):
        message['conversation_id'] = record['session_id']
    if isinstance(record.get('id'), int):
        if role == 'user' and kind in {'user_text', 'user_file', 'user_photo', 'command'}:
            message['conversation_actions'] = {'branch_mode': 'user', 'withdraw': True}
        elif kind == 'ai_reply':
            message['conversation_actions'] = {'branch_mode': 'assistant'}
    raw_metadata = record.get("metadata") or {}
    try:
        metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
        if not isinstance(metadata, dict):
            raise ValueError(_INVALID)
    except (ValueError, TypeError):
        metadata = {}
        message["media_error"] = "\u5386\u53f2\u9644\u4ef6\u5143\u6570\u636e\u635f\u574f"
    live = metadata.get('web_live')
    if metadata.get('run_id') or metadata.get('queue_run_id'):
        message['run_id']=metadata.get('run_id') or metadata['queue_run_id']
    if isinstance(metadata.get('queue_id'), str):
        message['queue_id'] = metadata['queue_id']
    if (isinstance(live, dict) and isinstance(live.get('epoch'), str)
            and isinstance(live.get('message_ids'), list)):
        message['web_live'] = {'epoch': live['epoch'], 'message_ids': [mid for mid in live['message_ids'] if type(mid) is int]}
    generation = metadata.get('ai_generation')
    if isinstance(generation, dict):
        message['ai_generation'] = {
            'provider': str(generation.get('provider') or ''),
            'model': str(generation.get('model') or ''),
            'generated_at': generation.get('generated_at', record.get('timestamp')),
        }
    display = metadata.get("display")
    if kind in {'ai_reply', 'media_reply'}:
        web_display = metadata.get('web_display')
        message['web_display'] = (web_display if isinstance(web_display, dict) else
                                  history_web_display(content, display))
    if isinstance(display, dict) and isinstance(display.get("content"), str):
        from .protocols import ProtocolParser
        from .output_archive import attach_archive_path
        message.update(content=attach_archive_path(ProtocolParser.restore_folded_html(display["content"])),
                       parse_mode=display.get("parse_mode"))
    elif kind in {"ai_reply", "media_reply"}:
        # 历史回放与 live 同源：含协议块折叠、纯 markdown 也转成 Telegram-HTML，
        # 两条路都 parse_mode=HTML，刷新后不再"变脸"（详见 _history_display_html）。
        # 取不到渲染器时 html 为 None，保持原始 markdown（老行为）。
        html = _history_display_html(content)
        if html is not None:
            message.update(content=html, parse_mode="HTML")
    elif kind not in {"user_text", "user_file", "user_photo"} and _HTML_PRESENTATION.search(content):
        message["parse_mode"] = "HTML"
    task = metadata.get('compression_task')
    if (isinstance(task, dict) and re.fullmatch(r'[a-f0-9]{32}', str(task.get('id', '')))
            and task.get('status') in {'pending', 'running', 'failed', 'stopped'}):
        message['reply_markup'] = [[{
            'text': '\u91cd\u8bd5\u6062\u590d', 'callback_data': 'retry_compress:' + task['id'],
        }]]
    apply_history_presentation(message, record, metadata,
        namespace=presentation_namespace, mode_override=presentation_mode)
    if kind not in _FILE_TYPES and not metadata.get('server_uploads'):
        return message
    if (metadata.get("attachment_purpose") == "configuration"
            or kind in {"user_file", "user_photo"} and content.startswith(_CONFIG_PREFIXES)):
        return message
    storage, workspace = Path(storage_root), Path(workspace_root)
    groups = []
    for key in ("display_media", "attachments", "server_uploads"):
        if key in metadata:
            refs = metadata[key]
            if not isinstance(refs, list):
                message["media_error"] = _INVALID
                continue
            if key == "attachments" and not refs:
                message["media_error"] = _INVALID
            if key == "attachments":
                refs = sorted(enumerate(refs), key=lambda pair: (
                    pair[1].get("order", pair[0])
                    if isinstance(pair[1], dict) and type(pair[1].get("order", pair[0])) is int
                    else pair[0], pair[0],
                ))
                refs = [ref for _, ref in refs]
            groups.extend((ref, key in {"attachments", "server_uploads"}) for ref in refs)
    if "attachments" not in metadata and "display_media" not in metadata and "server_uploads" not in metadata:
        if not (kind in {"ai_reply", "media_reply"} and metadata.get("generated_media_processed") is True):
            legacy = _legacy_media(record, storage, workspace)
            if kind in {'user_file', 'user_photo'} and role == 'user':
                try:
                    from .attachments import _legacy_references
                    verified = _legacy_references(record, storage / 'uploads')
                    groups.extend((ref, True) for ref in verified)
                except ValueError:
                    groups.extend((ref, False) for ref in legacy)
            else:
                groups.extend((ref, False) for ref in legacy)
            if not legacy and (kind in {"user_file", "user_photo"} or (
                    kind in {"ai_reply", "media_reply"} and _GENERATED_PREFIX in content)):
                message["media_error"] = "\u65e7\u8bb0\u5f55\u672a\u4fdd\u7559\u53ef\u9a8c\u8bc1\u7684\u9644\u4ef6\u5173\u8054\uff0c\u65e0\u6cd5\u6062\u590d\u4e0b\u8f7d"
            album = re.search(r"\[\u76f8\u518c\] \u5171(\d+)\u5f20\u56fe\u7247", content)
            if kind == "user_photo" and album and int(album[1]) != len(legacy):
                message["media_error"] = "\u65e7\u76f8\u518c\u9644\u4ef6\u8bb0\u5f55\u4e0d\u5b8c\u6574"
    if role == 'user' and metadata.get('file_ingress_mode') in {'attachment', 'server_upload'}:
        groups.sort(key=lambda pair: pair[0].get('order', 0)
                    if isinstance(pair[0], dict) and type(pair[0].get('order', 0)) is int else 0)
    seen = set()
    for ref, attachment in groups:
        item = _describe_media(ref, storage, attachment=attachment)
        if item.get("path") and item["path"] in seen:
            continue
        if item.get("path"):
            seen.add(item["path"])
        index = len(message["media"])
        if role == 'user' and attachment:
            item['ingress_mode'] = 'server_upload' if ref in metadata.get('server_uploads', []) else 'attachment'
        if (kind in {'user_file', 'user_photo'} and role == 'user' and attachment and isinstance(ref, dict)
                and ref.get('storage', 'uploads') == 'uploads' and not ref.get('immutable_object')
                and ref not in metadata.get('server_uploads', [])):
            try:
                from .attachment_versions import attachment_identity
                identity, _ = attachment_identity(ref, storage / 'uploads')
                item['versions_id'] = f"{record['id']}:{identity}"
            except (ValueError, KeyError, TypeError):
                pass  # Unsafe/damaged associations never expose a version capability.
        if not item.get("error") and type(record.get("id")) is int:
            item["download_url"] = f"/api/history/media/{record['id']}/{index}"
            if record.get('session_id'):
                item['conversation_id'] = record['session_id']
                item['download_url'] += '?' + urlencode({'conversation_id': record['session_id']})
        message["media"].append(item)
    if kind in {'ai_reply', 'media_reply'} and message['media']:
        message['media_group_id'] = generated_media_group_id([
            item.get('path', '') for item in message['media']
        ])
    if role == 'user' and (metadata.get('file_ingress_mode') in {'attachment', 'server_upload'}
                           or kind in {'user_file', 'user_photo'} and isinstance(metadata.get('attachments'), list)):
        message.update(content=_file_message_text(content, metadata, storage), parse_mode=None, file_message=True)
        if isinstance(metadata.get('upload_request_key'), str):
            message['upload_request_key'] = metadata['upload_request_key']
    return message
