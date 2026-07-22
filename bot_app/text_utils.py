"""通用文本处理工具；不依赖 Telegram、数据库或 Agent 状态。"""

def clip_middle_text(text: str, limit: int, label: str = "内容") -> str:
    if len(text) <= limit:
        return text
    marker = f"\n... ({label}已省略 {len(text) - limit} 字符，保留开头和末尾) ...\n"
    available = limit - len(marker)
    if available < 80:
        return text[:limit]
    head_len = max(1, available // 3)
    tail_len = max(1, available - head_len)
    return text[:head_len].rstrip() + marker + text[-tail_len:].lstrip()


