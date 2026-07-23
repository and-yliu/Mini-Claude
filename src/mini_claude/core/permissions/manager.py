from __future__ import annotations

import asyncio
import datetime
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from mini_claude.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    matches_outside_cwd,
    param_preview,
)
from mini_claude.core.permissions.storage import load_policy_file, save_policy_file

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()

@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    session_id: str
    tool_name: str

class PermissionManager:
    def __init__(
        self,
        policies: dict[str, ToolPolicy] | None = None,
        *,
        policy_file: Path | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._policies: dict[str, ToolPolicy] = policies or dict(DEFAULT_POLICIES)
        # tool_use_id → pending Future + metadata
        self._pending: dict[str, _PendingRequest] = {}
        # (session_id, tool_name) → "allow" | "deny"（session memory, loss on restart）
        self._session_always: dict[tuple[str, str], str] = {}
        # tool_name → "allow" | "deny"（persisted, load from policy_file）
        self._policy_file = policy_file
        self._persistent_always: dict[str, str] = (
            load_policy_file(policy_file) if policy_file is not None else {}
        )
        # 0 means no timeout
        self._timeout_s = timeout_s

    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionDecision:
        from mini_claude.core.permissions.policy import evaluate
        policy = self._policies.get(tool_name)
        return evaluate(tool_name, params, policy)

    async def wait_and_ask(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> tuple[bool, str]:
        command = str(params.get("command", "")) if tool_name == "bash" else ""
        policy = self._policies.get(tool_name)

        if command and policy:
            for pat in policy.deny_patterns:
                if re.search(pat, command):
                    logger.debug("permission: deny_pattern hit tool=%s", tool_name)
                    return False, "auto_deny"

        outside_cwd = matches_outside_cwd(command)

        if not outside_cwd:
            session_key = (session_id, tool_name)
            if session_key in self._session_always:
                cached = self._session_always[session_key]
                logger.debug("permission: session cache hit tool=%s decision=%s", tool_name, cached)
                return cached == "allow", f"auto_{cached}"

            persisted_key = tool_name
            if persisted_key in self._persistent_always:
                cached = self._persistent_always[persisted_key]
                logger.debug("permission: persistent cache hit tool=%s decision=%s", tool_name, cached)
                return cached == "allow", f"auto_{cached}"

            if command and policy:
                for pat in policy.allow_patterns:
                    if re.search(pat, command):
                        return True, "auto_allow"

            if policy is not None:
                if policy.default == PermissionDecision.DENY:
                    return False, "auto_deny"
                if policy.default == PermissionDecision.ALLOW:
                    return True, "auto_allow"

        # ASK (default ask or outside_cwd)
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[tool_use_id] = _PendingRequest(
            future=future,
            session_id=session_id,
            tool_name=tool_name
        )

        await event_emitter(
            {
                "type": "permission.requested",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "params": params,
                "param_preview": param_preview(tool_name, params),
                "session_id": session_id,
                "ts": _now(),
            }
        )

        try:
            if self._timeout_s> 0:
                result = await asyncio.wait_for(future, timeout=self._timeout_s)
            else:
                result = await future
        except asyncio.TimeoutError:
            self._pending.pop(tool_use_id, None)
            logger.info("permission: timeout tool_use_id=%s tool=%s", tool_use_id, tool_name)
            return False, "timeout"

        allowed = self._apply_response(result, session_id, tool_name)
        return allowed, result

    def response(self, tool_use_id: str, decision: str) -> None:
        req = self._pending.pop(tool_use_id, None)
        if req is None:
            logger.warning("permission.respond: unknown tool_use_id=%s", tool_use_id)
            return
        if not req.future.done():
            req.future.set_result(decision)

    def _apply_response(self, result: str, session_id: str, tool_name: str) -> bool:
        allow = result in ("allow_once", "always_allow")
        if result == "always_allow":
            self._session_always[(session_id, tool_name)] = "allow"
            self._persistent_always[tool_name] = "allow"
            logger.info(
                "permission: always allow tool=%s policy_file=%s persistent=%s",
                tool_name, self._policy_file, self._persistent_always,
            )

            if self._policy_file is not None:
                try:
                    save_policy_file(self._persistent_always, self._policy_file)
                    logger.info("permission: policy.toml written path=%s", self._policy_file)
                except Exception:
                    logger.exception("permission: failed to write policy.toml path=%s", self._policy_file)
            else:
                logger.warning("permission: policy_file is None, skipping persistence")
        elif result == "always_deny":
            self._session_always[(session_id, tool_name)] = "deny"
            self._persistent_always[tool_name] = "deny"
            logger.info(
                "permission: always deny tool=%s policy_file=%s persistent=%s",
                tool_name, self._policy_file, self._persistent_always,
            )
            if self._policy_file is not None:
                try:
                    save_policy_file(self._persistent_always, self._policy_file)
                    logger.info("permission: policy.toml written path=%s", self._policy_file)
                except Exception:
                    logger.exception("permission: failed to write policy.toml path=%s", self._policy_file)
            else:
                logger.warning("permission: policy_file is None, skipping persistence")

        return allow

    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        to_cancel = [tool_use_id for tool_use_id, req in self._pending.items() if req.session_id == session_id]
        for tool_use_id in to_cancel:
            req = self._pending.pop(tool_use_id)
            if not req.future.done():
                logger.debug(
                    "permission: cancel pending tool_use_id=%s reason=%s", tool_use_id, reason
                )
                req.future.set_result("deny_once")
