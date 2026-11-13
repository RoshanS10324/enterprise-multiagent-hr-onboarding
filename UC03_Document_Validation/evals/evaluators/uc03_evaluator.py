from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from src.sub_agent.checklist import ChecklistService
from src.sub_agent.extraction import MockDocumentExtractor
from src.sub_agent.models import DocumentValidationRequest
from src.sub_agent.validator import DocumentValidationService
from src.state.store import InMemoryAuditStore, InMemoryCaseStore


@dataclass
class ScenarioResult:
    scenario_id: str
    description: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    actual: dict[str, Any] = field(default_factory=dict)


class UC03Evaluator:
    """
    Evaluates UC-03 scenarios against expected outputs.
    Uses the same service and rules as production — no mocking of business logic.
    """

    async def run_scenario(self, scenario: dict) -> ScenarioResult:
        scenario_id = scenario["scenario_id"]
        description = scenario["description"]
        raw_input = scenario["input"]
        expected = scenario["expected"]

        # Fresh stores per scenario — full isolation
        case_store = InMemoryCaseStore()
        audit_store = InMemoryAuditStore()
        checklist = ChecklistService("default")
        extractor = MockDocumentExtractor()
        service = DocumentValidationService(
            case_store=case_store,
            audit_store=audit_store,
            checklist_service=checklist,
            extractor=extractor,
        )

        try:
            request = DocumentValidationRequest(**raw_input)
        except Exception as exc:
            return ScenarioResult(
                scenario_id=scenario_id,
                description=description,
                passed=False,
                failures=[f"Request construction failed: {exc}"],
            )

        try:
            result = await service.validate(request)
        except Exception as exc:
            return ScenarioResult(
                scenario_id=scenario_id,
                description=description,
                passed=False,
                failures=[f"Service raised unexpected exception: {exc}"],
            )

        actual = {
            "validation_findings": [f.value for f in result.validation_findings],
            "document_status": result.document_status.value,
            "compliance_item_status": result.compliance_item_status.value,
            "required_next_action": result.required_next_action.value,
            "evidence_status": result.evidence_status.value,
            "visa_pack_status": result.visa_pack_status.value,
            "injection_detected": result.injection_detected,
            "detail": result.detail.lower(),
        }

        failures = _check_expected(expected, actual, scenario_id, service, request)

        # Idempotency check — run twice, compare findings
        if expected.get("idempotent"):
            result2 = await service.validate(request)
            actual2_findings = [f.value for f in result2.validation_findings]
            if scenario_id == "SC-19":
                if "DUPLICATE_REQUEST" not in actual2_findings:
                    failures.append(
                        f"Expected DUPLICATE_REQUEST on re-submission, got: {actual2_findings}"
                    )
            elif actual["validation_findings"] != actual2_findings:
                failures.append(
                    f"Idempotency failed: first run={actual['validation_findings']}, "
                    f"second run={actual2_findings}"
                )

        return ScenarioResult(
            scenario_id=scenario_id,
            description=description,
            passed=len(failures) == 0,
            failures=failures,
            actual=actual,
        )

    async def run_all(self, scenarios: list[dict]) -> list[ScenarioResult]:
        results = []
        for scenario in scenarios:
            result = await self.run_scenario(scenario)
            results.append(result)
        return results


def _check_expected(
    expected: dict,
    actual: dict,
    scenario_id: str,
    service: DocumentValidationService,
    request: DocumentValidationRequest,
) -> list[str]:
    failures: list[str] = []

    # findings must include
    for finding in expected.get("validation_findings_must_include", []):
        if finding not in actual["validation_findings"]:
            failures.append(f"Expected finding '{finding}' not in {actual['validation_findings']}")

    # findings must include at least one of
    must_include_one = expected.get("validation_findings_must_include_one_of", [])
    if must_include_one:
        if not any(f in actual["validation_findings"] for f in must_include_one):
            failures.append(
                f"Expected at least one of {must_include_one} in {actual['validation_findings']}"
            )

    # findings must exclude
    for finding in expected.get("validation_findings_must_exclude", []):
        if finding in actual["validation_findings"]:
            failures.append(f"Finding '{finding}' should NOT be present but was")

    # exact field matches
    for field_name in ["compliance_item_status", "required_next_action",
                       "document_status", "injection_detected"]:
        if field_name in expected:
            if actual.get(field_name) != expected[field_name]:
                failures.append(
                    f"{field_name}: expected '{expected[field_name]}', got '{actual.get(field_name)}'"
                )

    # field must NOT equal
    for field_name in ["evidence_status", "compliance_item_status", "visa_pack_status"]:
        not_key = f"{field_name}_not"
        if not_key in expected:
            if actual.get(field_name) == expected[not_key]:
                failures.append(
                    f"{field_name} should NOT be '{expected[not_key]}' but was"
                )

    # detail string contains
    if "detail_contains" in expected:
        if expected["detail_contains"].lower() not in actual["detail"]:
            failures.append(
                f"detail should contain '{expected['detail_contains']}', got: '{actual['detail']}'"
            )

    return failures
