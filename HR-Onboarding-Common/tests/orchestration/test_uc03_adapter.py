"""UC03 adapter tests: in-process, async UC03 graph via sys.path insertion of its repo root
(its own wheel omits the sibling guardrails/knowledge packages it imports -- verified, not
assumed). A document is only treated as valid from document_status/compliance_item_status,
never from "the call didn't raise"."""

from __future__ import annotations

from _helpers import UC_ROOTS, require_uc
from hr_onboarding_common.orchestration.adapters.uc03_documents import call_uc03_validate_documents
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state


def _deps():
    require_uc("uc03")
    return OrchestratorDeps(ctx=None, worker_pool=None, uc03_repo_root=UC_ROOTS["uc03"])


def _state(documents=None):
    state = new_state(case_id="C1", client_id="CL1", correlation_id="corr1",
                       actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"]},
                       intake_trigger={"candidate_name": "Arun Kumar", "documents": documents or {}},
                       uc02_role_context={"role_id": "engineer", "grade": "G1", "nationality": "IN", "location": "Dubai"})
    state["employee_id"] = "EMP-001"
    return state


def test_missing_documents_wait_for_input_on_this_stage_only():
    deps = _deps()
    result = call_uc03_validate_documents(_state(documents={}), deps)
    assert result["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.WAITING_FOR_INPUT.value
    assert result["document_validation_result"]
    assert result["blockers"]
    requirements = result["correction_requirements"]
    assert requirements and {r["action"] for r in requirements} == {"supply"}
    assert all("MISSING_DOCUMENT" in r["reason_codes"] for r in requirements)
    assert {s for st, s in result["stage_statuses"].items() if st != Stage.VALIDATE_DOCUMENTS.value} <= \
        {StageStatus.PENDING.value, StageStatus.NOT_REQUESTED.value}


def test_a_valid_document_is_not_verified_without_an_hr_review_decision():
    """UC03 never returns ACCEPTED: a valid document is UNDER_REVIEW / PENDING_APPROVAL until the
    caller supplies hr_review_status=APPROVED (an HR reviewer's recorded decision)."""
    deps = _deps()
    good = {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01", "evidence_stored": True,
            "extracted_name": "Arun Kumar", "attestation_confirmed": True}
    state = _state()
    state["submitted_documents"] = {t: {"document_type": t, "document_ref": f"SYN-{t}", "revision": 1, "metadata": good}
                                    for t in ("signed_offer_letter", "passport", "degree_certificate",
                                              "experience_letter", "payslips", "nda", "policy_acknowledgements")}
    result = call_uc03_validate_documents(state, deps)
    assert result["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.AWAITING_APPROVAL.value
    assert {r["classification"] for r in result["document_validation_result"].values()} == {"hr_review_required"}
    assert {r["document_status"] for r in result["document_validation_result"].values()} == {"UNDER_REVIEW"}

    for doc in result["submitted_documents"].values():  # an HR reviewer approved every revision
        doc["hr_review"] = {"decision": "approved", "revision": 1, "reviewer": "user:hr-admin-01",
                            "reviewed_at": "2026-10-29T00:00:00+00:00"}
    again = call_uc03_validate_documents(result, deps)
    assert again["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.COMPLETE.value
    assert {r["compliance_item_status"] for r in again["document_validation_result"].values()} == {"COMPLETE"}


def test_an_unchanged_revision_is_never_sent_to_uc03_twice():
    """UC03 flags a second RECEIVED submission of a type as DUPLICATE_REQUEST; re-running the stage
    without a new revision must reuse the earlier result, not trigger that."""
    deps = _deps()
    state = _state()
    state["submitted_documents"] = {"passport": {"document_type": "passport", "document_ref": "SYN-P", "revision": 1,
                                                 "metadata": {"readable": True, "required_fields_present": True,
                                                              "expiry_date": "2031-01-01",
                                                              "extracted_name": "Arun Kumar"}}}
    first = call_uc03_validate_documents(state, deps)
    second = call_uc03_validate_documents(first, deps)
    assert second["document_validation_result"]["passport"] == first["document_validation_result"]["passport"]
    assert "DUPLICATE_REQUEST" not in second["document_validation_result"]["passport"]["findings"]


def test_a_uc03_exception_is_a_technical_failure_not_a_document_verdict():
    deps = _deps()

    class _Broken:
        async def ainvoke(self, _):
            raise ConnectionError("store down; passport holder Arun Kumar")  # the message is never recorded

    call_uc03_validate_documents(_state(), deps)  # builds the runtime
    deps._uc03_graph = _Broken()
    result = call_uc03_validate_documents(_state(documents={"passport": {"readable": True}}), deps)
    assert result["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.RETRY_PENDING.value
    assert result["stage_errors"][Stage.VALIDATE_DOCUMENTS.value]["code"] == "UC03_TECHNICAL_ERROR"
    assert result["correction_requirements"] == [] and "Arun Kumar" not in str(result["stage_errors"])


def test_execution_success_is_not_conflated_with_document_validity():
    """UC03 can return successfully with document_status=REJECTED/MISSING -- the adapter must
    never read that as VALID merely because the call didn't raise."""
    deps = _deps()
    result = call_uc03_validate_documents(_state(documents={}), deps)
    for doc_result in result["document_validation_result"].values():
        if doc_result.get("document_status") not in ("ACCEPTED",):
            assert result["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] != StageStatus.COMPLETE.value


def test_no_raw_document_content_leaks_into_state():
    deps = _deps()
    result = call_uc03_validate_documents(
        _state(documents={"passport": {"extracted_name": "Arun Kumar", "readable": True}}), deps,
    )
    for doc_result in result["document_validation_result"].values():
        assert "extracted_name" not in doc_result  # projection only -- see state.py docstring
        assert "extracted_fields" not in doc_result
