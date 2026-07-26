from __future__ import annotations

import tomllib
from pathlib import Path
from dataclasses import dataclass, field

@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    model: str = ""

class AgentProfileLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # find the agent profile related to the name
    def resolve(self, name: str) -> AgentProfile | None:
        for path in self._search_path(name):
            if path.exists():
                try:
                    return self._parse(path)
                except Exception:
                    return None
        return None

    def _search_path(self, name: str) -> list[Path]:
        builtin = self._BUILTIN_DIR / f"{name}.toml"
        global_ = Path("~/.mini/agents").expanduser() / f"{name}.toml"
        local = Path(".mini/agents") / f"{name}.toml"
        return [builtin, global_, local]

    def _parse(self, path: Path) -> AgentProfile:
        with open(path, "rb") as f:
            data = tomllib.load(f)

        agent = data.get("agent", {})
        return AgentProfile(
            name=path.stem,
            description=agent.get("description", ""),
            system_prompt=agent.get("system_prompt", ""),
            allowed_tools=agent.get("allowed_tools", []),
            model=agent.get("model", ""),
        )


