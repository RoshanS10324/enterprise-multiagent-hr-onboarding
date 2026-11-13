"""Enforcement tests for the UC-01 security controls.

Every test here asserts an observable refusal or an observable record --
a write that did not happen, a value that is absent from a prompt, a
chain that no longer verifies. None of them assert merely that a
function was called or that a config key exists.

Synthetic data only (factories.py). Nothing here reaches a network.
"""

import socket
from dataclasses import replace

import pytest

from sub_agent.audit_integrity import (
    GENESIS_HASH,
    ChainViolationCode,
    SensitiveValueInAuditError,
    check_entry,
    verify_chain,
)
from sub_agent.errors import CaseNotFoundError, PolicyViolationCode, PolicyViolationError
from sub_agent.hooks import before_hris_write
from sub_agent.hris_prepare import prepare_hris_records
from sub_agent.hris_write import approve_hris_write, execute_hris_write
from sub_agent.llm.base import LlmInputRefused, LlmRequest
from sub_agent.llm.brain import load_all_tasks
from sub_agent.llm.fake import FakeLlm
from sub_agent.llm.guard import FORBIDDEN_INPUT_NAMES, build_prompt
from sub_agent.models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditEntry,
    Checkpoint,
    HrisRecordStatus,
    Role,
)
from sub_agent.policy import authorize_decision

from factories import (
    TEST_FINGERPRINT_KEY,
    make_change_deps,
    make_intake_deps,
    make_policy,
    make_viewer,
    seed_linked_case_and_form,
)

# A distinctive synthetic salary: if this string turns up anywhere it
# should not, a substring search finds it unambiguously.
SECRET_SALARY = "Base: 987654 SYNTHETIC-CANARY/yr; benefits: standard"
SALARY_DIGITS = "987654"
SECRET_EMAIL = "canary.joiner@example.test"


REQUESTER = "hr-manager-001"
APPROVER = "hr-manager-002"


def _prepared(deps=None, **form_overrides):
    deps = deps or make_change_deps()
    case, form = seed_linked_case_and_form(deps, **form_overrides)
    result = prepare_hris_records(case.case_id, deps, requested_by=REQUESTER)
    return deps, case, form, result


def _prepared_and_approved(**form_overrides):
    deps, case, form, result = _prepared(**form_overrides)
    approve_hris_write(result.approval_id, APPROVER, deps)
    return deps, case, form, result


def _hris_approval(deps, case, form):
    return next(
        a
        for a in deps.approval_repo.list_for_case(case.case_id)
        if a.subject_ref == f"hris:{case.case_id}:v{form.version}"
    )


# ---------------------------------------------------------------------
# 1. An unapproved protected action is rejected
# ---------------------------------------------------------------------


def test_write_without_approval_is_refused_and_nothing_reaches_the_gateway():
    deps, case, _, _ = _prepared(salary_and_terms=SECRET_SALARY)

    with pytest.raises(ValueError):
        execute_hris_write(case.case_id, deps)

    assert deps.hris_gateway.written_count() == 0
    assert all(
        r.status == HrisRecordStatus.PREPARED
        for r in deps.hris_record_repo.list_for_case(case.case_id)
    )


def test_hook_blocks_when_the_approval_is_still_pending():
    deps, case, form, _ = _prepared()
    record = deps.hris_record_repo.list_for_case(case.case_id)[0]
    pending = _hris_approval(deps, case, form)
    assert pending.status == ApprovalStatus.PENDING

    assert before_hris_write(record, form, pending, deps) == "APPROVAL_NOT_APPROVED"


# ---------------------------------------------------------------------
# 2. An approval for employee A cannot authorize an action for B
# ---------------------------------------------------------------------


def test_approval_from_another_case_cannot_authorize_this_write():
    deps, case_a, form_a, _ = _prepared_and_approved()
    _, case_b, _, result_b = _prepared(deps=deps, candidate_email="other.joiner@example.test")
    approve_hris_write(result_b.approval_id, APPROVER, deps)

    record_a = deps.hris_record_repo.list_for_case(case_a.case_id)[0]
    approval_b = deps.approval_repo.get(result_b.approval_id)

    # Case B's approval is genuinely APPROVED, so only the subject
    # binding can reject it here.
    assert approval_b.status == ApprovalStatus.APPROVED
    assert case_b.case_id != case_a.case_id
    assert before_hris_write(record_a, form_a, approval_b, deps) == "APPROVAL_SUBJECT_MISMATCH"


def test_approval_from_a_different_checkpoint_cannot_authorize_a_write():
    deps, case, form, _ = _prepared_and_approved()
    record = deps.hris_record_repo.list_for_case(case.case_id)[0]

    # Same case, same subject_ref shape, wrong checkpoint.
    impostor = Approval(
        case_id=case.case_id,
        checkpoint=Checkpoint.HC_04_DATA,
        subject_ref=f"hris:{case.case_id}:v{form.version}",
        requested_to="hr-officer-001",
        requested_by="hr-officer-001",
        reason="not an HRIS write approval",
        status=ApprovalStatus.APPROVED,
        decided_by="hr-manager-001",
    )

    assert before_hris_write(record, form, impostor, deps) == "APPROVAL_SUBJECT_MISMATCH"


# ---------------------------------------------------------------------
# 3. A stale approval cannot authorize a protected action
# ---------------------------------------------------------------------


def test_approval_for_a_superseded_form_version_is_refused():
    deps, case, form, _ = _prepared_and_approved()
    record = deps.hris_record_repo.list_for_case(case.case_id)[0]
    approval = _hris_approval(deps, case, form)

    # The record now claims a version the approval was never given for.
    stale_record = record.model_copy(update={"form_version": form.version + 1})

    assert before_hris_write(stale_record, form, approval, deps) == "APPROVAL_SUBJECT_MISMATCH"


def test_kill_switch_blocks_an_otherwise_valid_approved_write():
    deps, case, _, _ = _prepared_and_approved(salary_and_terms=SECRET_SALARY)
    deps = replace(deps, hris_writes_enabled=lambda: False)

    outcome = execute_hris_write(case.case_id, deps)

    assert outcome.blocked_code == "WRITES_DISABLED"
    assert outcome.written == []
    assert deps.hris_gateway.written_count() == 0


# ---------------------------------------------------------------------
# 4. Duplicate retries do not bypass approval or duplicate the employee
# ---------------------------------------------------------------------


def test_repeated_writes_do_not_create_a_second_employee():
    deps, case, _, _ = _prepared_and_approved(salary_and_terms=SECRET_SALARY)

    first = execute_hris_write(case.case_id, deps)
    second = execute_hris_write(case.case_id, deps)
    third = execute_hris_write(case.case_id, deps)

    assert first.completed is True
    assert second.written == [] and third.written == []
    # Two systems, one employee each -- never four.
    assert deps.hris_gateway.written_count() == 2
    assert len(deps.publisher.events) == 1


# ---------------------------------------------------------------------
# 5. Sensitive fields do not enter LLM prompts
# ---------------------------------------------------------------------


def test_no_task_declares_an_input_that_could_carry_a_salary():
    for name, spec in load_all_tasks().items():
        assert not set(spec.inputs_allowed) & FORBIDDEN_INPUT_NAMES, name


def test_passing_the_salary_under_a_forbidden_name_is_refused_for_every_task():
    tasks = load_all_tasks()
    llm = FakeLlm(tasks)

    for name in tasks:
        with pytest.raises(LlmInputRefused):
            llm.complete(LlmRequest(task=name, inputs={"salary_and_terms": SECRET_SALARY}))

    # Refused before any work: nothing was recorded as a call.
    assert llm.calls == []


def test_the_complete_prompt_for_a_salary_discrepancy_excludes_the_figure():
    """The one place a model sees a salary *field* is a discrepancy
    summary. It gets the field NAME; the figure must appear in neither
    the assembled prompt nor the draft that comes back."""
    tasks = load_all_tasks()
    llm = FakeLlm(tasks)
    spec = tasks["discrepancy_summary"]

    inputs = {
        "case_id": "case-001",
        "fields_in_conflict": "salary_and_terms",
        "other_source": "offer_letter",
        "form_version": "1",
    }
    prompt = build_prompt(spec, inputs)
    response = llm.complete(LlmRequest(task="discrepancy_summary", inputs=inputs))

    assert SALARY_DIGITS not in prompt and SECRET_SALARY not in prompt
    assert SALARY_DIGITS not in response.text and SECRET_SALARY not in response.text
    # The field name is what HR needs, and it is still there.
    assert "salary_and_terms" in response.text


# ---------------------------------------------------------------------
# 6. Sensitive values are not exposed through audit entries
# ---------------------------------------------------------------------


def test_audit_append_refuses_a_sensitive_key():
    deps = make_change_deps()
    entry = AuditEntry(
        case_id="case-001",
        actor_type=ActorType.AGENT,
        actor_id="test",
        action="SOMETHING",
        after={"salary_and_terms": SECRET_SALARY},
        reference="case-001",
    )

    with pytest.raises(SensitiveValueInAuditError) as exc_info:
        deps.audit_log.append(entry)

    assert exc_info.value.offending == ["salary_and_terms"]
    # The value itself must not be in the error message.
    assert SECRET_SALARY not in str(exc_info.value)
    assert deps.audit_log.entries() == []


def test_audit_append_refuses_a_sensitive_key_nested_in_a_list():
    entry = AuditEntry(
        case_id="case-001",
        actor_type=ActorType.AGENT,
        actor_id="test",
        action="SOMETHING",
        after={"changes": [{"field": "designation"}, {"iban": "XX00 SYNTHETIC"}]},
        reference="case-001",
    )

    with pytest.raises(SensitiveValueInAuditError):
        check_entry(entry)


def test_audit_append_refuses_a_stringified_secret():
    entry = AuditEntry(
        case_id="case-001",
        actor_type=ActorType.AGENT,
        actor_id="test",
        action="SOMETHING",
        after={"note": "value was **********"},
        reference="case-001",
    )

    with pytest.raises(SensitiveValueInAuditError):
        check_entry(entry)


def test_field_names_are_still_allowed_as_values():
    """The guard must not break legitimate processing: audit entries are
    expected to name the fields that changed."""
    deps = make_change_deps()
    entry = AuditEntry(
        case_id="case-001",
        actor_type=ActorType.AGENT,
        actor_id="test",
        action="HRIS_PREPARED",
        after={"fields": ["designation", "salary_and_terms"], "systems": ["zoho_people"]},
        reference="case-001",
    )

    deps.audit_log.append(entry)  # must not raise

    assert len(deps.audit_log.entries()) == 1


def test_a_full_write_flow_never_puts_the_salary_in_the_audit_log():
    deps, case, _, _ = _prepared_and_approved(
        salary_and_terms=SECRET_SALARY, candidate_email=SECRET_EMAIL
    )
    execute_hris_write(case.case_id, deps)

    for entry in deps.audit_log.entries():
        dumped = str(entry.model_dump())
        assert SECRET_SALARY not in dumped
        assert SALARY_DIGITS not in dumped
        assert SECRET_EMAIL not in dumped


# ---------------------------------------------------------------------
# 7 & 8. Audit completeness, ordering and integrity
# ---------------------------------------------------------------------


def test_required_audit_events_are_present_on_the_success_path():
    deps, case, _, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)

    actions = [e.action for e in deps.audit_log.entries_for_case(case.case_id)]
    for required in ("HRIS_WRITE_APPROVED", "HRIS_WRITE_SUCCEEDED"):
        assert required in actions, f"missing {required} in {actions}"


def test_a_blocked_write_is_audited_on_the_failure_path():
    deps, case, _, _ = _prepared_and_approved()
    deps = replace(deps, hris_writes_enabled=lambda: False)

    execute_hris_write(case.case_id, deps)

    blocked = [
        e
        for e in deps.audit_log.entries_for_case(case.case_id)
        if e.action == "HRIS_WRITE_BLOCKED_BY_HOOK"
    ]
    assert len(blocked) == 1
    assert blocked[0].after["code"] == "WRITES_DISABLED"


def test_an_authorization_failure_is_audited_without_naming_the_case():
    from sub_agent.service import CaseService

    deps, case, _, _ = _prepared()
    service = CaseService(deps, TEST_FINGERPRINT_KEY, make_policy())
    stranger = make_viewer(
        user_id="outsider-001", role=Role.HR_OFFICER, client_ids=frozenset({"client-other"})
    )

    with pytest.raises(CaseNotFoundError):
        service.compare_sources(stranger, case.case_id, [])

    denied = [e for e in deps.audit_log.entries() if e.action == "ACCESS_DENIED"]
    assert denied, "expected an ACCESS_DENIED entry"
    # Must not reveal that the case exists.
    assert all(case.case_id not in str(e.model_dump()) for e in denied)


def test_audit_entries_are_chained_in_append_order():
    deps, case, _, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)

    entries = deps.audit_log.entries()
    assert len(entries) > 3
    assert entries[0].prev_hash == GENESIS_HASH
    assert [e.sequence for e in entries] == list(range(len(entries)))
    assert verify_chain(entries) == []


def test_editing_an_entry_breaks_the_chain():
    deps, case, _, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)
    entries = deps.audit_log.entries()

    tampered = list(entries)
    target = len(tampered) // 2
    tampered[target] = tampered[target].model_copy(update={"actor_id": "someone-else"})

    violations = verify_chain(tampered)

    assert ChainViolationCode.DIGEST_MISMATCH in {v.code for v in violations}
    assert any(v.index == target for v in violations)


def test_removing_an_entry_breaks_the_chain():
    deps, case, _, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)
    entries = deps.audit_log.entries()

    without_one = entries[:2] + entries[3:]

    codes = {v.code for v in verify_chain(without_one)}
    assert codes & {ChainViolationCode.PREV_HASH_MISMATCH, ChainViolationCode.SEQUENCE_BREAK}


def test_reordering_entries_breaks_the_chain():
    deps, case, _, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)
    entries = deps.audit_log.entries()

    swapped = list(entries)
    swapped[1], swapped[2] = swapped[2], swapped[1]

    assert verify_chain(swapped) != []


def test_an_unchained_entry_is_reported_rather_than_silently_accepted():
    entry = AuditEntry(
        case_id="case-001",
        actor_type=ActorType.AGENT,
        actor_id="test",
        action="SOMETHING",
        reference="case-001",
    )

    violations = verify_chain([entry])

    assert [v.code for v in violations] == [ChainViolationCode.UNCHAINED_ENTRY]


# ---------------------------------------------------------------------
# 9 & 10. Persistence boundary and client isolation
# ---------------------------------------------------------------------


def test_a_viewer_from_another_client_cannot_decide_an_hris_write():
    deps, case, form, _ = _prepared()
    approval = _hris_approval(deps, case, form)
    outsider = make_viewer(
        user_id="hr-manager-999",
        role=Role.HR_MANAGER,
        client_ids=frozenset({"client-somewhere-else"}),
    )

    with pytest.raises(CaseNotFoundError):
        authorize_decision(
            make_policy(),
            outsider,
            Checkpoint.HC_05_HRIS_WRITE,
            case,
            approval,
            deps.audit_log,
        )

    assert approval.status == ApprovalStatus.PENDING


def test_self_approval_is_refused_for_the_hris_write_checkpoint():
    deps, case, form, _ = _prepared()
    approval = _hris_approval(deps, case, form)
    # prepare_hris_records records requested_by; the same person may not decide.
    requester = make_viewer(
        user_id=approval.requested_by,
        role=Role.HR_MANAGER,
        client_ids=frozenset({case.client_id}),
    )

    with pytest.raises(PolicyViolationError) as exc_info:
        authorize_decision(
            make_policy(),
            requester,
            Checkpoint.HC_05_HRIS_WRITE,
            case,
            approval,
            deps.audit_log,
        )

    assert exc_info.value.code == PolicyViolationCode.SELF_APPROVAL
    assert approval.status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------
# 11. Validation and payload rules remain enforced
# ---------------------------------------------------------------------


def test_a_tampered_prepared_payload_is_caught_before_the_write():
    deps, case, form, _ = _prepared_and_approved()
    record = deps.hris_record_repo.list_for_case(case.case_id)[0]
    approval = _hris_approval(deps, case, form)

    tampered = record.model_copy(
        update={"prepared_payload": {**record.prepared_payload, "job_title": "Smuggled Title"}}
    )

    assert before_hris_write(tampered, form, approval, deps) == "PAYLOAD_MISMATCH"


def test_the_salary_is_never_stored_on_a_prepared_record():
    deps, case, _, _ = _prepared(salary_and_terms=SECRET_SALARY)

    for record in deps.hris_record_repo.list_for_case(case.case_id):
        dumped = str(record.model_dump())
        assert SECRET_SALARY not in dumped
        assert SALARY_DIGITS not in dumped


# ---------------------------------------------------------------------
# 12. Stubs cannot make real network calls
# ---------------------------------------------------------------------


def test_a_full_write_flow_opens_no_socket(monkeypatch):
    """The HRIS gateway is a stub. Prove it by making any socket
    connection raise for the duration of a complete write."""

    def forbidden(*args, **kwargs):
        raise AssertionError("the stubbed flow attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    deps, case, _, _ = _prepared_and_approved(salary_and_terms=SECRET_SALARY)
    outcome = execute_hris_write(case.case_id, deps)

    assert outcome.completed is True


# The ONLY module allowed to import a network client. UC-01 reaches
# Supabase over PostgREST, so exactly one module does HTTP; every other
# module must stay free of it, which is what this control protects. Any
# addition here is a deliberate, reviewed decision - not a default.
_NETWORK_CLIENT_ALLOWLIST = {"src/sub_agent/adapters/supabase/client.py"}


def test_only_the_supabase_client_imports_a_network_client():
    """A stub that quietly grew an HTTP call would still pass the socket
    test if that path were never exercised. This checks the import
    surface instead.

    Narrowed (was: no module at all) when Supabase persistence landed:
    the database adapter necessarily speaks HTTP. The guarantee kept is
    that network access is confined to one reviewed module, so no
    domain, model, or stub module can acquire it unnoticed.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    src = root / "src" / "sub_agent"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in _NETWORK_CLIENT_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in ("import requests", "import httpx", "import urllib", "import http.client"):
            if marker in text:
                offenders.append(f"{rel}: {marker}")

    assert offenders == [], f"network client imported in src/: {offenders}"


def test_network_client_allowlist_entries_exist():
    """An allowlist entry for a deleted file would silently widen the
    control the next time a module is added at that path."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    missing = [rel for rel in _NETWORK_CLIENT_ALLOWLIST if not (root / rel).is_file()]
    assert missing == [], f"allowlisted files do not exist: {missing}"


# ---------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------


def test_every_brain_task_declares_a_version():
    for name, spec in load_all_tasks().items():
        assert isinstance(spec.version, int) and spec.version >= 1, name


def test_an_advisory_call_records_the_task_version_it_used():
    """Provenance: an audited advisory label must name the prompt
    version that produced it, and still carry no prompt text."""
    from sub_agent.intake.email_intake import handle_acceptance_email
    from sub_agent.models import OfferRecord, OfferStatus

    tasks = load_all_tasks()
    deps = make_intake_deps()
    deps.llm = FakeLlm(tasks)
    deps.offer_repo.save(
        OfferRecord(
            client_id="client-synthetic-001",
            offer_ref="OFFER-1001",
            candidate_email="advisory.joiner@example.test",
            hr_officer_id="hr-officer-001",
            status=OfferStatus.SENT,
        )
    )

    raw = "\n".join(
        [
            "From: advisory.joiner@example.test",
            # A subject the deterministic classifier accepts, so the
            # advisory second opinion is reached at all.
            "Subject: Re: Your offer [OFFER-1001] - I accept",
            "",
            "Body text that UC-01 never reads.",
            "",
        ]
    )
    handle_acceptance_email(raw, deps)

    advisory = [
        e for e in deps.onboarding_deps.audit_log.entries() if e.action == "LLM_ADVISORY"
    ]
    assert advisory, "expected an LLM_ADVISORY entry"
    recorded = advisory[0].after
    assert recorded["task"] == "intake_intent"
    assert recorded["task_version"] == tasks["intake_intent"].version
    # Still no prompt text and no subject line in the audit entry.
    assert "subject" not in recorded
    assert "Re: Your offer" not in str(recorded)


def test_a_policy_denial_records_the_policy_version():
    deps, case, form, _ = _prepared()
    approval = _hris_approval(deps, case, form)
    requester = make_viewer(
        user_id=approval.requested_by,
        role=Role.HR_MANAGER,
        client_ids=frozenset({case.client_id}),
    )

    with pytest.raises(PolicyViolationError):
        authorize_decision(
            make_policy(), requester, Checkpoint.HC_05_HRIS_WRITE, case, approval, deps.audit_log
        )

    denied = [e for e in deps.audit_log.entries() if e.action == "POLICY_DENIED"]
    assert denied
    assert denied[0].after["policy_version"] == make_policy().policy_version


# ---------------------------------------------------------------------
# Review follow-ups: chain verification is wired, and scoped correctly
# ---------------------------------------------------------------------


def test_verify_chain_on_a_per_case_subset_is_not_how_it_is_used():
    """A filtered subset breaks the chain by construction. This pins the
    reason verify_audit_integrity passes the complete log instead."""
    from sub_agent.adapters.memory import InMemoryAuditLog

    log = InMemoryAuditLog()
    for i in range(6):
        log.append(
            AuditEntry(
                case_id="caseA" if i % 2 == 0 else "caseB",
                actor_type=ActorType.AGENT,
                actor_id="a",
                action=f"A{i}",
                reference="r",
            )
        )

    assert verify_chain(log.entries()) == []  # complete log: intact
    assert verify_chain(log.entries_for_case("caseA")) != []  # subset: false alarm


def test_tail_truncation_is_not_detected_and_is_documented_as_such():
    """Pins the known blind spot so nobody later claims the chain
    catches it. Dropping entries off the END leaves a valid chain."""
    from sub_agent.adapters.memory import InMemoryAuditLog
    from sub_agent import audit_integrity

    log = InMemoryAuditLog()
    for i in range(5):
        log.append(
            AuditEntry(
                case_id="c1",
                actor_type=ActorType.AGENT,
                actor_id="a",
                action=f"A{i}",
                reference="c1",
            )
        )

    assert verify_chain(log.entries()[:3]) == []
    # The limitation must stay documented where a reader will find it.
    assert "Truncation of the most recent entries" in audit_integrity.verify_chain.__doc__


def test_audit_integrity_check_is_auditor_only():
    from sub_agent.case_status import verify_audit_integrity
    from sub_agent.errors import AccessDeniedError
    from factories import make_u9_deps

    deps, case, _, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)
    u9 = make_u9_deps(deps)

    for role in (Role.HR_OFFICER, Role.HR_MANAGER, Role.CLIENT_USER):
        with pytest.raises(AccessDeniedError):
            verify_audit_integrity(make_viewer(role=role, user_id="u1"), u9)

    checked, violations = verify_audit_integrity(
        make_viewer(role=Role.AUDITOR, user_id="auditor-001"), u9
    )
    assert checked > 0 and violations == []


def test_audit_integrity_check_reports_a_tampered_log():
    """The wired check must actually surface a broken chain, not just
    return an empty list on every call."""
    from sub_agent.case_status import verify_audit_integrity
    from factories import make_u9_deps

    deps, case, _, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)

    # Reach into the adapter's store and edit one entry in place.
    stored = deps.audit_log._entries
    stored[1] = stored[1].model_copy(update={"actor_id": "evil"})

    _, violations = verify_audit_integrity(
        make_viewer(role=Role.AUDITOR, user_id="auditor-001"), make_u9_deps(deps)
    )

    assert ChainViolationCode.DIGEST_MISMATCH in {v.code for v in violations}


def test_per_case_audit_response_hides_the_chain_fields():
    """`sequence` is global across clients, so it must not ride out on a
    per-case read."""
    from sub_agent.api.schemas import AuditEntryView

    deps, case, _, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)

    for entry in deps.audit_log.entries_for_case(case.case_id):
        view = AuditEntryView.of(entry)
        dumped = view.model_dump()
        assert entry.entry_hash is not None  # stored...
        assert {"sequence", "prev_hash", "entry_hash"}.isdisjoint(dumped)  # ...not served


def test_persona_is_not_silently_absent_from_prompts():
    """The persona is loaded but never prepended. Pin that, so a future
    change to build_prompt is a deliberate one."""
    from sub_agent.llm.brain import load_persona

    tasks = load_all_tasks()
    persona = load_persona()
    spec = tasks["intake_intent"]

    prompt = build_prompt(spec, {"subject": "Re: offer"})

    assert persona.strip() not in prompt
    assert "NOT currently prepended" in load_persona.__doc__
