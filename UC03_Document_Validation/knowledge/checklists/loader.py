from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

_CHECKLISTS_DIR = Path(__file__).parent


class DocumentRequirement(BaseModel):
    id: str
    label: str
    required: bool
    applies_to: list[str]
    validations: dict[str, Any]


class Checklist(BaseModel):
    version: str
    name: str
    documents: list[DocumentRequirement]


def load_checklist(name: str = "default") -> Checklist:
    path = _CHECKLISTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Checklist '{name}' not found at {path}. "
            "Create a YAML file in knowledge/checklists/ for this client."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Checklist(**data)


def _applies(rule: str, context: dict) -> bool:
    """Return True if a single applies_to rule matches the context."""
    if rule == "all":
        return True
    if ":" in rule:
        key, value = rule.split(":", 1)
        ctx_val = context.get(key)
        if isinstance(ctx_val, bool):
            return ctx_val == (value.lower() == "true")
        return str(ctx_val).lower() == value.lower()
    return bool(context.get(rule))


def resolve_applicable_documents(
    checklist: Checklist, context: dict
) -> list[DocumentRequirement]:
    """Filter checklist documents to those applicable given the context dict."""
    result: list[DocumentRequirement] = []
    for doc in checklist.documents:
        if any(_applies(rule, context) for rule in doc.applies_to):
            result.append(doc)
    return result
