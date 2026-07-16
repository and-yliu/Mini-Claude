from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

type EventHandler = Callable[[BaseModel], Awaitable[None]]

class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler):
        self._subscribers.append(handler)
    
    async def publish(self, event: BaseModel):
        for handler in self._subscribers:
            handler(event)
