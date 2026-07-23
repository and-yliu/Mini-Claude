from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"

# outside cwd(Current Working Directory) bash command, ask user before continuing
OUTSIDE_CWD_HEURISTICS: list[str] = [
    r"(^|\s)/[^\s]",              # absolute path
    r"(^|\s)~",                   # tilde home
    r"(^|\s)\.\.(/|$|\s)",        # parent traversal
    r"\$\{?HOME\b",               # $HOME variable
    r"\$\{?PWD\b",                # $PWD variable
    r"(^|\s|;|&&|\|\|)cd(\s|$)",  # explicit cd
]

_OUTSIDE_CWD_RE: list[re.Pattern[str]] = [re.compile(p) for p in OUTSIDE_CWD_HEURISTICS]

# check if command is outside-cwd
def matches_outside_cwd(command: str) -> bool:
    return any(pat.search(command) for pat in _OUTSIDE_CWD_RE)

@dataclass
class ToolPolicy:
    default: PermissionDecision
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)


DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "bash":       ToolPolicy(default=PermissionDecision.ASK),
    "write_file": ToolPolicy(default=PermissionDecision.ASK),
    "read_file":  ToolPolicy(default=PermissionDecision.ALLOW),
    "list_dir":   ToolPolicy(default=PermissionDecision.ALLOW),
    "note_save":  ToolPolicy(default=PermissionDecision.ALLOW),
}

_UNKNOWN_TOOL_DEFAULT = PermissionDecision.ASK

_PREVIEW_KEY: dict[str, str] = {
    "bash":       "command",
    "read_file":  "path",
    "write_file": "path",
    "list_dir":   "path",
    "note_save":  "content",
}
_PREVIEW_MAX = 60

# human readable param preview for permission decision
def param_preview(tool_name: str, params: dict[str, Any]) -> str:
    key = _PREVIEW_KEY.get(tool_name)
    if key and key in params:
        content = str(params[key])
        if len(content) > _PREVIEW_MAX:
            content = content[:_PREVIEW_MAX] + "…"
        return f"{key}={content!r}"
    snippet = str(params)
    return snippet[:_PREVIEW_MAX] if len(snippet) > _PREVIEW_MAX else snippet

# Four level of static permission judgement
def evaluate(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy | None = None,
) -> PermissionDecision:
    if policy is None:
        policy = DEFAULT_POLICIES.get(tool_name)

    if policy is None:
        return _UNKNOWN_TOOL_DEFAULT

    command = str(params.get("command", "")) if tool_name == "bash" else ""

    # Layer 1, deny pattern (bash only)
    if command:
        for pat in policy.deny_patterns:
            if re.search(pat, command):
                return PermissionDecision.DENY

    # Layer 2, outside cwd command (bash only) - force ask
    if command and matches_outside_cwd(command):
        return PermissionDecision.ASK

    # Layer 3, allow pattern (bash only)
    if command:
        for pat in policy.allow_patterns:
            if re.search(pat, command):
                return PermissionDecision.ALLOW

    # Layer 4: tool default
    return ToolPolicy.default