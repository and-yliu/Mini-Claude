from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionContext:
    run_id: str
    goal: str
    max_steps: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    status: str = "running"  # "running" | "success" | "failed"
    reason: str | None = None

    # add goal as first message
    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "user", "content": self.goal})

    # append llm answer
    def add_assistant_message(self, content: list[Any]) -> None:
        self.messages.append({"role": "assistant", "content": content})

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