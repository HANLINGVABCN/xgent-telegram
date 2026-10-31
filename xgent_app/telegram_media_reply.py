"""One generated reply containing media and following prose, via the SDK request."""
from contextlib import ExitStack
from html import escape
from pathlib import Path

from .telegram_rich import render_rich, legacy_chunks


async def send(bot, op, extra, rich=True):
    from telegram import InputFile, InputMediaPhoto, InputMediaDocument
    from .telegram_native import _unsupported
    artifacts = op.payload['artifacts']
    saved = op.payload.setdefault('_native_parts', [])
    routing = op.payload.get('kwargs') or {}
    routing = {key: value for key, value in routing.items() if key in {'message_thread_id', 'disable_notification'}}
    editing = bool(op.payload.get('_edit'))
    if rich:
        groups, group, budget = [], [], 0
        for item in artifacts:
            size = len(escape(str(item['path']))) + 180
            if group and (len(group) == 50 or budget+size > 24000):
                groups.append(group); group, budget = [], 0
            group.append(item); budget += size
        if group:
            groups.append(group)
        for index, group in enumerate(groups):
            if index < len(saved) and not editing:
                continue
            with ExitStack() as stack:
                data, media, figures = {'chat_id': op.chat_id, **routing}, [], []
                photos_only = True
                for n, artifact in enumerate(group):
                    path = Path(artifact['path']); name = 'file'+str(n); mid = 'm'+str(n)
                    photo = str(artifact.get('mime_type', '')).startswith('image/') and path.stat().st_size <= 10*1024*1024
                    kind = 'photo' if photo else 'document'
                    photos_only = photos_only and photo
                    data[name] = InputFile(stack.enter_context(path.open('rb')), filename=path.name, read_file_handle=False)
                    media.append({'id': mid, 'media': {'type': kind, 'media': 'attach://'+name}})
                    tag = '<img src="tg://photo?id='+mid+'"/>' if photo else '<tg-document src="tg://document?id='+mid+'"></tg-document>'
                    figures.append('<figure>'+tag+'<figcaption>'+escape(str(path))+'</figcaption></figure>')
                html = ('<tg-collage>'+''.join(figures)+'</tg-collage>' if len(figures)>1 and photos_only else ''.join(figures))
                if index == len(groups)-1:
                    prose = render_rich(op.payload.get('text') or '', details=op.payload.get('details_html'))
                    if len(html)+len(prose[0])<=32000:
                        html += prose.pop(0)
                    op.payload['_remaining_text'] = prose
                data['rich_message'] = {'html': html, 'media': media}
                try:
                    pages = op.payload.setdefault('_page_html', [])
                    if index < len(saved):
                        if index < len(pages) and pages[index] == html:
                            continue
                        data['message_id'] = saved[index]
                    result = await bot._post('editMessageText' if index < len(saved) else 'sendRichMessage', data, **extra)
                except Exception as exc:
                    if not saved and _unsupported(exc):
                        return await send(bot, op, extra, rich=False)
                    raise
                if index >= len(saved):
                    saved.append(result['message_id'])
                while len(pages) <= index:
                    pages.append('')
                pages[index] = html
    else:
        op.payload['_fallback'] = True
        progress = int(op.payload.get('_media_sent', 0))
        while progress < len(artifacts):
            item = artifacts[progress]
            photo = str(item.get('mime_type', '')).startswith('image/') and Path(item['path']).stat().st_size<=10*1024*1024
            group = []
            for artifact in artifacts[progress:progress+10]:
                is_photo = str(artifact.get('mime_type', '')).startswith('image/') and Path(artifact['path']).stat().st_size<=10*1024*1024
                if is_photo != photo:
                    break
                group.append(artifact)
            with ExitStack() as stack:
                items = [(InputMediaPhoto if photo else InputMediaDocument)(InputFile(stack.enter_context(Path(a['path']).open('rb')), read_file_handle=False),
                    caption=str(a['path']) if len(str(a['path']))<=1000 else None) for a in group]
                if len(items)>1:
                    messages = await bot.send_media_group(chat_id=op.chat_id, media=items, **routing, **extra)
                else:
                    method = bot.send_photo if photo else bot.send_document
                    argument = {'photo' if photo else 'document': items[0].media}
                    messages = [await method(chat_id=op.chat_id, **argument, caption=items[0].caption, **routing, **extra)]
                saved.extend(message.message_id for message in messages)
            progress += len(group)
            op.payload['_media_sent'] = progress
        if editing and op.payload.get('details_html'):
            op.payload['_remaining_text'] = [op.payload['details_html']]
        else:
            op.payload.setdefault('_remaining_text', legacy_chunks(op.payload.get('text') or ''))
    remaining = op.payload.get('_remaining_text') or []
    position = len(groups) if rich else len(saved)
    while remaining:
        body = remaining[0]
        if body.strip():
            method = 'editMessageText' if rich and position < len(saved) else 'sendRichMessage' if rich else 'sendMessage'
            field = {'rich_message': {'html': body}} if rich else {'text': body, 'parse_mode': 'HTML'}
            data = {'chat_id': op.chat_id, **routing, **field}
            pages = op.payload.setdefault('_page_html', [])
            if rich and position < len(saved):
                data['message_id'] = saved[position]
                if position < len(pages) and pages[position] == body:
                    remaining.pop(0); position += 1; continue
            result = await bot._post(method, data, **extra)
            if not rich or position >= len(saved):
                saved.append(result['message_id'])
            while len(pages) <= position:
                pages.append('')
            pages[position] = body
        remaining.pop(0)
        position += 1
    return saved[0] if saved else None
