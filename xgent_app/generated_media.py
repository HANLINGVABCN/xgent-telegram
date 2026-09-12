"""One-time extraction and durable handoff of a native media reply."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable


async def await_media_handoff(task: asyncio.Task) -> Any:
    """Finish the durable write before propagating even repeated cancellations."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class GeneratedMediaReply:
    def __init__(
        self,
        extract: Callable[..., tuple[str, list[dict]]],
        persist: Callable[[str, list[dict], bool], Awaitable[Any]],
    ):
        self.extract = extract
        self.persist = persist
        self.text = ""
        self.artifacts: list[dict] = []
        self.recorded = False
        self._task: asyncio.Task | None = None

    @property
    def started(self) -> bool:
        return self._task is not None

    async def prepare(self, raw: str, *, stopped: bool = False,
                      partial: bool = False) -> tuple[str, list[dict]]:
        # Delivery failures must not extract/save the same response a second time.
        if self._task is None:
            self._task = asyncio.create_task(self._prepare(raw, stopped, partial))
        return await await_media_handoff(self._task)

    async def _prepare(self, raw: str, stopped: bool, partial: bool) -> tuple[str, list[dict]]:
        self.text, self.artifacts = await asyncio.to_thread(
            self.extract, raw, partial=partial,
        )
        if any(str(item.get("mime_type") or "").startswith("image/")
               for item in self.artifacts):
            await self.persist(self.text, self.artifacts, stopped)
            self.recorded = True
        return self.text, self.artifacts
