from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from datetime import datetime, UTC
from dataclasses import dataclass

from pydantic import BaseModel

from mini_claude.core.bus.envelope import EventPushEnvelope
from mini_claude.core.trace.writer import TraceRecord, TraceWriter

logger = logging.getLogger(__name__)

def _now() -> str:
    return datetime.now(UTC).isoformat()

@dataclass
class _Subscription:
    sub_id: str
    writer: asyncio.StreamWriter  # TCP connection writer
    topics: list[str]  # like ["run.*", "llm.token"]
    scope: str  # "global" or "run:<run_id>"


class IpcEventBroadcaster:
    def __init__(self, trace: TraceWriter | None = None):
        self._subscription: list[_Subscription] = []
        self._trace = trace

    def subscribe(self, writer: asyncio.StreamWriter, topics: list[str], scope: str) -> str:
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        sub = _Subscription(sub_id, writer, topics, scope)
        self._subscription.append(sub)
        return sub_id

    def unsubscribe(self, writer):
        new_sub = []
        for sub in self._subscription:
            if sub.writer is not writer:
                new_sub.append(sub)
        self._subscription = new_sub

    async def handle(self, event: BaseModel) -> None:
        event_dict = event.model_dump()
        event_type: str = event_dict.get("type", "")
        run_id: str | None = event_dict.get("run_id")

        dead: list[asyncio.StreamWriter] = []

        for sub in self._subscription:
            if not self._matches_topic(event_type, sub.topics):
                continue
            if not self._matches_scope(run_id, sub.scope):
                continue

            try:
                envelop = EventPushEnvelope(event=event_dict)
                sub.writer.write(envelop.model_dump_json().encode() + b"\n")
                await sub.writer.drain()

                if self._trace is not None:
                    client_id = str(sub.writer.get_extra_info("peername", "<unknown>"))
                    self._trace.emit(
                        TraceRecord(
                            ts=_now(),
                            direction="CORE→CLIENT",
                            layer="ipc",
                            kind="push",
                            run_id=run_id,
                            client_id=client_id,
                            data={"sub_id": sub.sub_id, "event_type": event_type}
                        )
                    )
            except (ConnectionResetError, BrokenPipeError, OSError):
                logger.debug("dead connection for sub %s, scheduling cleanup", sub.sub_id)
                dead.append(sub.writer)

        for writer in dead:
            self.unsubscribe(writer)

    @staticmethod
    def _matches_topic(event_type: str, topics: list[str]) -> bool:
        return any(fnmatch.fnmatch(event_type, pattern) for pattern in topics)

    @staticmethod
    def _matches_scope(run_id: str | None, scope: str) -> bool:
        if scope == "global":
            return True
        if scope.startswith("run:"):
            return run_id == scope[4:]
        return False
