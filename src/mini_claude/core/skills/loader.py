from __future__ import annotations


import re
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class Skill:
    name: str
    description: str
    system_prompt_template: str
    allowed_tools: list[str] = field(default_factory=list)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# parse skill markdown file and get frontmatter information and the rest of the prompt
def _parse_skill_document(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    name = path.stem
    description = ""
    allowed_tools: list[str] = []
    body = text

    m = _FRONTMATTER_RE.match(text)
    if m:
        info = m.group(1)
        body = text[m.end():]
        lines = info.splitlines()

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("name:"):
                name = stripped[len("name:"):].strip().strip('"').strip("'")
            elif stripped.startswith("description:"):
                description = stripped[len("description:"):].strip().strip('"').strip("'")
            elif stripped.startswith("allowed_tools:"):
                pass
            elif stripped.startswith("- "):
                allowed_tools.append(stripped[2:].strip())

    return Skill(
        name=name,
        description=description,
        system_prompt_template=body.strip(),
        allowed_tools=allowed_tools
    )

class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # find the skill related to the name
    def resolve(self, name: str) -> Skill | None:
        for path in self._search_paths(name):
            if path.exists():
                try:
                    return _parse_skill_document(path)
                except Exception:
                    return None
        return None

    # find all possible path for a skill
    def _search_path(self, name: str) -> list[Path]:
        dirs = [
            Path(".mini/skills"),
            Path("~/.mini/skills").expanduser(),
            self._BUILTIN_DIR
        ]

        paths: list[Path] = []
        for d in dirs:
            paths.append(d / f"{name}.md")
            paths.append(d / name / "SKILL.md")
        return paths

    # list all skill name for the agent
    def list_all(self) -> list[str]:
        seen: dict[str, None]= {}

        for d in [
            Path(".mini/skills"),
            Path("~/.mini/skills").expanduser(),
            self._BUILTIN_DIR
        ]:
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    seen[f.stem] = None
                for f in sorted(d.glob("*/SKILL.md")):
                    seen[f.parent.stem] = None

        return list(seen)

    # list all skill object for the agent
    def list_all_skills(self) -> list[Skill]:
        seen: dict[str, Skill]= {}
        
        for d in [
            Path(".mini/skills"),
            Path("~/.mini/skills").expanduser(),
            self._BUILTIN_DIR
        ]:
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    try:
                        skill = _parse_skill_document(f)
                        seen[skill.name] = skill
                    except Exception:
                        pass
                for f in sorted(d.glob("*/SKILL.md")):
                    try:
                        skill = _parse_skill_document(f)
                        seen[skill.name] = skill
                    except Exception:
                        pass

        return list(seen.values())

    # replace $ARGUMENTS with the prompt of the user
    def render_prompt(self, skill: Skill, prompt: str) -> str:
        return skill.system_prompt_template.replace("$ARGUMENTS", prompt)