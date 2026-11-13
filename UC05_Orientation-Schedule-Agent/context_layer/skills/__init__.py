"""Skill loader. A skill is a directory under this package holding a SKILL.md file: YAML front matter
(name, version, description) followed by the model instructions.

Services call load_skill(name) for their system prompt and record skill.ref (name@vN) in the audit
trail, so every model call can be traced to the exact instructions it ran with. Editing a skill's
instructions should bump its version.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

SKILLS_ROOT = Path(__file__).resolve().parent


class SkillNotFoundError(KeyError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    version: int
    description: str
    instructions: str

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"


def _parse(path: Path) -> Skill:
    _, front_matter, body = path.read_text(encoding="utf-8").split("---", 2)
    meta = yaml.safe_load(front_matter)
    return Skill(name=meta["name"], version=int(meta["version"]), description=meta["description"],
                 instructions=body.strip())


@lru_cache(maxsize=None)
def load_skill(name: str) -> Skill:
    path = SKILLS_ROOT / name / "SKILL.md"
    if not path.is_file():
        raise SkillNotFoundError(f"no skill named '{name}' under {SKILLS_ROOT}")
    skill = _parse(path)
    if skill.name != name:
        raise ValueError(f"{path}: front-matter name '{skill.name}' does not match its directory '{name}'")
    return skill


def list_skills() -> list[Skill]:
    return [load_skill(p.parent.name) for p in sorted(SKILLS_ROOT.glob("*/SKILL.md"))]
