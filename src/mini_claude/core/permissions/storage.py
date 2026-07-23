from __future__ import annotations

from pathlib import Path

_DEFAULT_POLICY_PATH = Path("~/.mini/policy.toml")

# load the [always] section in policy.toml, return {"tool_name": "allow"/"deny"}
def load_policy_file(path: Path | None = None) -> dict[str, str]:
    p = (path or _DEFAULT_POLICY_PATH).expanduser()
    if not p.exists():
        return {}
    result: dict[str, str] = {}
    in_always = False

    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[always]":
            in_always = True
            continue
        if stripped.startswith("["):
            in_always = False
            continue
        if in_always and "=" in stripped and not stripped.startswith("#"):
            k, _, v = stripped.partition("=")
            k = k.strip()
            v = v.strip().strip('"')
            if v in ("allow", "deny"):
                result[k] = v

    return result

# write {"tool_name": "allow"/"deny"} to policy.toml
def save_policy_file(always: dict[str, str], path: Path | None = None) -> None:
    p = (path or _DEFAULT_POLICY_PATH).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ~/.kama/policy.toml",
        "# Auto generated and manage by kama-core, can be change manually but the format must be correct",
        "",
        "[always]",
    ]

    for tool, decision in always.items():
        lines.append(f'{tool}="{decision}"')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
