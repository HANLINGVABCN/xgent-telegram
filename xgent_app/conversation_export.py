"""Authenticated browser download of the same scoped archive as Bot /export."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConversationDownload:
    path: Path
    filename: str = '系统记忆.zip'


async def export_conversation(namespace):
    _, bundle = await namespace['create_conversation_export']()
    return ConversationDownload(Path(bundle['archive_path']))
