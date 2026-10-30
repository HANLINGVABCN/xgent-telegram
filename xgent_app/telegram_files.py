"""Select playable Telegram media from bytes, keeping other files as documents."""
def native_kind(path):
    import filetype
    try:kind=filetype.guess(path)
    except (OSError,ValueError):return None
    if kind is None:return None
    return {'video/mp4':'video','image/gif':'animation','audio/mpeg':'audio','audio/mp4':'audio',
            'audio/ogg':'voice','audio/opus':'voice'}.get(kind.mime)
