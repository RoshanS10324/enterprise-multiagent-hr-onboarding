from __future__ import annotations

from knowledge.checklists.loader import (
    Checklist,
    DocumentRequirement,
    load_checklist,
    resolve_applicable_documents,
)


class ChecklistService:
    def __init__(self, checklist_name: str = "default") -> None:
        self._checklist: Checklist = load_checklist(checklist_name)

    def get_applicable_documents(self, context: dict) -> list[DocumentRequirement]:
        return resolve_applicable_documents(self._checklist, context)

    def get_requirement(
        self, document_type: str, context: dict
    ) -> DocumentRequirement | None:
        return next(
            (d for d in self.get_applicable_documents(context) if d.id == document_type),
            None,
        )

    def get_outstanding(
        self,
        context: dict,
        already_complete: set[str],
    ) -> list[str]:
        applicable = self.get_applicable_documents(context)
        return [d.id for d in applicable if d.id not in already_complete]

    def build_context(self, request_dict: dict) -> dict:
        """Build checklist context dict from a request-like object."""
        return {
            "location": request_dict.get("location", ""),
            "sponsorship": request_dict.get("sponsorship_applicable", False),
            "visa_required": request_dict.get("visa_required", False),
            "nationality": request_dict.get("nationality", ""),
            "role": request_dict.get("role", ""),
            "grade": request_dict.get("grade", ""),
        }
