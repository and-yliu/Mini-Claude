from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pydantic import BaseModel
from typing import ClassVar

@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    error_type: str | None = None  # "runtime_error" | "timeout" | "schema_error"

class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: ClassVar[type[BaseModel] | None] = None

    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...