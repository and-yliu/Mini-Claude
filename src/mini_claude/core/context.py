from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionContext:
    run_id: str
    goal: str
    max_steps: int
    prefill_messages: list[dict[str, Any]] = field(default_factory=list)
    session_notes: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    status: str = "running"  # "running" | "success" | "failed"
    reason: str | None = None
    result: str = ""
    global_context: str = ""
    project_context: str = ""
    system_prompt_override: str | None = None

    # add goal as first message
    def __post_init__(self) -> None:
        if self.prefill_messages:
            self.messages = [dict(m) for m in self.prefill_messages]
        if not self.messages:
            self.messages.append({"role": "user", "content": self.goal})

    # append llm answer
    def add_assistant_message(self, content: list[Any]) -> None:
        self.messages.append({"role": "assistant", "content": content})
    
    def system_prompt(self, base: str) -> str:
        parts = [self.system_prompt_override if self.system_prompt_override else base]
        if self.global_context.strip():
            parts.append("\n\n## Global Context\n" + self.global_context.strip())
        if self.project_context.strip():
            parts.append("\n\n## Project Context\n" + self.project_context.strip())
        if self.session_notes.strip():
            parts.append(
                "\n\n## Session Notes\n"
                + self.session_notes.strip()
                + "\n\nRemember important durable facts by calling note_save."
            )
        return "".join(parts)

    # add tool result to message as user message, bundle multiple tool results
    def add_tool_result(
        self, 
        tool_use_id: str, 
        content: str, 
        is_error: bool = False
    ) -> None:
        block: dict[str, Any]= {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }

        if is_error:
            block["is_error"] = True
        
        last = self.messages[-1] if self.messages else None
        if (
            last is not None
            and last["role"] == "user"
            and isinstance(last["content"], list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            last["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})
    
    # return true if done
    def is_done(self) -> bool:
        return self.status != "running"

    # mark run as success
    def mark_success(self) -> None:
        self.status = "success"

    # mark run as failed and document reason
    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.reason = reason