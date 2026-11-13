"""Swagger request examples. They are generated from the scenario registry (sub_agent.api.scenarios), so every example is a documented,
tested scenario built from the shared synthetic fixtures. Nothing here is real data."""

from typing import Any

from sub_agent.api.scenarios import EXTRA_103, IN_101, build_examples, status_label  # noqa: F401  (IN_101/EXTRA_103 re-exported)

EVALUATE_EXAMPLES: dict[str, dict[str, Any]] = build_examples("evaluate")
SUBMIT_EXAMPLES: dict[str, dict[str, Any]] = build_examples("submit")
ORCHESTRATED_EXAMPLES: dict[str, dict[str, Any]] = build_examples("orchestrated")

STATUS_EXAMPLES: dict[str, dict[str, Any]] = {
    status.lower(): {"summary": status_label(status), "description": f"DEV ONLY. Sets the mock parent case to {status}. {note}",
                     "value": {"status": status}}
    for status, note in (("ON_HOLD", "UC-04 then blocks the case (CASE_ON_HOLD)."),
                         ("WITHDRAWN", "UC-04 then stops processing and drafts a notice to IT."),
                         ("CANCELLED", "Treated like WITHDRAWN."), ("ACTIVE", "Restores the normal state."))
}
