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


def head_lines(text: str, n: int):
    """按行截断：返回 (前 n 行拼成的字符串, 被丢弃的行数)。

    - text 总行数 <= n：原样返回 (text, 0)。
    - 总行数 > n：只保留前 n 行，dropped = 总行数 - n（即第 n+1 行起被折的行数）。
    只做行级切割，绝不改动保留行的内容；供折叠引擎（协议块「前 50 行 + 已折叠 N 行」）
    与 CLI TUI 共用，保证三端封顶规则一致。
    """
    if not text:
        return "", 0
    lines = text.split("\n")
    if n < 0:
        n = 0
    if len(lines) <= n:
        return text, 0
    return "\n".join(lines[:n]), len(lines) - n


