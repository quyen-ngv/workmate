"""In-process pub/sub. Channels (web, Telegram) subscribe; services publish."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from actoroffice.domain.ids import utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    kind: str  # e.g. "ticket.updated", "run.output", "interaction.created"
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=utcnow)


class EventBus:
    def __init__(self, queue_size: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size

    def publish(self, name: str, /, **data: Any) -> None:
        event = Event(name, data)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropping event %s for a slow subscriber", name)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        q: asyncio.Queue[Event] = asyncio.Queue(self._queue_size)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)
