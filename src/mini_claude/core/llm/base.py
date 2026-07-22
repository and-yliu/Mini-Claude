from __future__ import annotations

from typing import Protocol

from mini_claude.core.events.bus import EventBus
from mini_claude.core.llm.types import LlmResponse

class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None
    ) -> LlmResponse: ...