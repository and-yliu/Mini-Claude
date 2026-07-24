from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mini_claude.core.bus.envelope import HandlerError
from mini_claude.core.bus.events import (
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
)
from mini_claude.core.events.bus import EventBus
from mini_claude.core.runs import new_run_id
from mini_claude.core.session.model import Session, SessionMode
from mini_claude.core.session.store import SessionStore
from mini_claude.core.llm.base import LLMProvider
from mini_claude.core.compact.compactor import Compactor

if TYPE_CHECKING:
    from mini_claude.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012

def _now() -> str:
    return datetime.now(UTC).isoformat()

class SessionManager:
    def __init__(self, store: SessionStore, runner_factory: Callable[[], AgentRunner], bus: EventBus, provider: LLMProvider | None = None):
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        
    
    # create a session and write to meta.json
    async def create(self, session_mode: SessionMode, title: str = "") -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=session_mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[]
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=session_mode, ts=ts))
        return session

    # handle messages, by adding it to thread and start agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        
        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            self._store.append_message(sid, "user", content)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            if not session.title:
                session.title = content[:40]

            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)
            session.updated_at = _now()
            self._store.write_meta(session)

            runner = self._runner_factory()
            await runner.run_and_capture(
                content,
                run_id=run_id,
                session=session,
                store=self._store,
            )

            session.updated_at = _now()
            if session.mode == "one_shot":
                session.status = "closed"
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            self._store.write_meta(session)
            return run_id
    
    # close a session
    async def close(self, sid: str) -> None:
        session: Session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        
        async with lock:
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # manual and overwrite thread.jsonl
    async def compact(self, session_id: str, focus: str = ""):
        session = self._get_session(session_id)
        lock = self._locks[session_id]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")

        async with lock:
            from mini_claude.core.bus.commands import SessionCompactResult
            messages = self._store.read_messages(session_id)
            session_dir = self._store.session_dir(session_id)
            compactor = Compactor(self._bus, session_dir, session_id)

            result = await compactor.compact_messages(messages, self._provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")

            self._store.write_compacted(session_id, [
                {"role": "user", "content": result.summary_text},
                {"role": "assistant", "content": "Understood, I'll continue from this summary."},
            ])

            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )


    # return a list of history for a session
    def list_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_messages(sid)

    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session


