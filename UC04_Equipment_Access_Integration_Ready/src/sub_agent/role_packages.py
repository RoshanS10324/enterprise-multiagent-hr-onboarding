"""Skill `recommend_role_standard_items` (context_layer/skills/recommend_role_standard_items.md).

When nothing has been requested for a case (`NO_ITEMS_REQUESTED`, DL-17: missing information is asked of HR), this
reads the role's standard equipment and access package from the case's own verified configuration and returns it as a
recommendation HR can review and confirm. It is the list HR would otherwise look up and type by hand.

Advisory only (L1, read-only): it requests nothing, approves nothing and changes no case, item or ticket. HR still
records the request, so `requested_by` is always the HR user. Every recommended item comes from the role's own matrix,
so it still goes through its standard approval (GR-03 is neither avoided nor triggered by following it).

Nothing is guessed (BR-03, BR-20):
- a configuration that belongs to another client or role gives no recommendation at all (`CONFIG_SCOPE_MISMATCH`);
- a MISSING matrix gives no recommendation for that kind (`CONFIG_MISSING:role_*_matrix`), never an empty list;
- a VERIFIED_EMPTY matrix recommends nothing for that kind, because the client configured none.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sub_agent.config_state import ConfigBlock
from sub_agent.contracts import Uc04Input

SKILL_NAME = "recommend_role_standard_items"


class RolePackageRecommendation(BaseModel):
    skill: str = SKILL_NAME
    skill_version: str = "1.0.0"
    client_id: str
    role_id: str
    config_version: str
    # None: no recommendation for that kind (its matrix is MISSING, or the configuration is out of scope).
    equipment: list[str] | None = None
    access: list[str] | None = None
    gaps: list[str] = Field(default_factory=list)
    advisory_only: bool = True
    note: str = ("The role's standard package from its configuration. HR reviews it and records the request; each item "
                 "still needs its standard approval.")


def _package(block: ConfigBlock[list[str]]) -> list[str] | None:
    if block.is_missing:
        return None
    return list(dict.fromkeys(block.value or []))  # VERIFIED_EMPTY -> []; order kept, duplicates dropped


def recommend_standard_items(inp: Uc04Input) -> RolePackageRecommendation:
    cfg = inp.config
    rec = RolePackageRecommendation(client_id=inp.client_id, role_id=inp.role_id, config_version=cfg.version)
    if cfg.client_id != inp.client_id or cfg.role_id != inp.role_id:
        rec.gaps.append("CONFIG_SCOPE_MISMATCH")
        return rec
    rec.equipment = _package(cfg.role_equipment_matrix)
    rec.access = _package(cfg.role_access_matrix)
    if rec.equipment is None:
        rec.gaps.append("CONFIG_MISSING:role_equipment_matrix")
    if rec.access is None:
        rec.gaps.append("CONFIG_MISSING:role_access_matrix")
    return rec
