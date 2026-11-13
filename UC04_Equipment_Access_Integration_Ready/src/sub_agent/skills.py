"""UC-04 Skills loader and registry.

Discovers and validates structured skill definitions from `context_layer/skills/`,
providing programmatic access for the sub-agent and Fake LLM advisory layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / "context_layer" / "skills"


class SkillDefinition(BaseModel):
    """Structured representation of a sub-agent reasoning skill."""

    name: str
    use_case: str = "UC-04"
    version: str = "1.0.0"
    type: str = "advisory_skill"
    description: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    when_to_use: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    knowledge_dependencies: list[str] = Field(default_factory=list)
    failure_behavior: dict[str, str] = Field(default_factory=dict)
    doc_body: str = ""


class SkillRegistry:
    """Discovers, validates, and loads skills from context_layer/skills/."""

    def __init__(self, skills_dir: Path | str | None = None) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
        self._skills: dict[str, SkillDefinition] = {}
        self.load_all()

    def load_all(self) -> dict[str, SkillDefinition]:
        """Load and validate all skill markdown files in the skills directory."""
        if not self.skills_dir.exists():
            return {}

        for path in self.skills_dir.glob("*.md"):
            if path.name.lower() == "readme.md":
                continue
            skill = self._parse_skill_file(path)
            if skill:
                self._skills[skill.name] = skill

        return self._skills

    def _parse_skill_file(self, path: Path) -> SkillDefinition | None:
        raw = path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            return None

        parts = raw.split("---", 2)
        if len(parts) < 3:
            return None

        frontmatter_str = parts[1]
        body = parts[2].strip()

        try:
            data = yaml.safe_load(frontmatter_str) or {}
            data["doc_body"] = body
            return SkillDefinition(**data)
        except Exception:
            return None

    def get_skill(self, name: str) -> SkillDefinition | None:
        """Get a validated skill by name."""
        return self._skills.get(name)

    def list_skills(self) -> list[SkillDefinition]:
        """List all loaded skills."""
        return list(self._skills.values())

    def match_skills_for_status(self, status: str) -> list[SkillDefinition]:
        """Find skills applicable to a specific case status."""
        matched: list[SkillDefinition] = []
        for skill in self._skills.values():
            conditions = " ".join(skill.when_to_use).lower()
            if status.lower() in conditions:
                matched.append(skill)
        # If none specifically match the status, provide general assessment skill
        if not matched and "assess_case_and_requests" in self._skills:
            matched.append(self._skills["assess_case_and_requests"])
        return matched

    def match_skills(
        self,
        result: Any | None = None,
        status: str = "",
    ) -> list[SkillDefinition]:
        """Find skills applicable based on both case status and result context."""
        status_val = status
        if result is not None and hasattr(result, "overall_status"):
            st = result.overall_status
            status_val = st.value if hasattr(st, "value") else str(st)

        matched = list(self.match_skills_for_status(status_val))

        if result is not None:
            # Config gaps or out-of-matrix items activate requirements retrieval skill
            has_gaps = bool(getattr(result, "config_gaps", None))
            has_exc = any(
                getattr(i, "exception_required", False) for i in getattr(result, "items", [])
            )
            if (has_gaps or has_exc) and "retrieve_access_equipment_requirements" in self._skills:
                skill = self._skills["retrieve_access_equipment_requirements"]
                if skill not in matched:
                    matched.append(skill)
            elif not (has_gaps or has_exc):
                # Its own text applies only to out-of-matrix items or configuration gaps, so an ordinary case never lists it,
                # even though its text also names the statuses it is usually consulted in.
                matched = [s for s in matched if s.name != "retrieve_access_equipment_requirements"]
                if not matched and "assess_case_and_requests" in self._skills:
                    matched.append(self._skills["assess_case_and_requests"])

            # Nothing requested yet (DL-17): the role's standard package, from its configuration, for HR to review
            no_items = any(getattr(b, "code", None) == "NO_ITEMS_REQUESTED" for b in getattr(result, "blockers", []) or [])
            if no_items and "recommend_role_standard_items" in self._skills:
                skill = self._skills["recommend_role_standard_items"]
                if skill not in matched:
                    matched.append(skill)

            # Ticket existence activates evidence and IT status skill
            has_tickets = any(
                getattr(i, "ticket_id", None) for i in getattr(result, "items", [])
            )
            if has_tickets and "summarize_evidence_and_it_status" in self._skills:
                skill = self._skills["summarize_evidence_and_it_status"]
                if skill not in matched:
                    matched.append(skill)

        return matched
