"""Narrated end-to-end demo: an offer-acceptance email in, to a created
employee record out. Deterministic Python only, in-memory adapters,
synthetic data.

[Proposed] The email trigger itself is a dev simulator (see
src/sub_agent/intake/email_intake.py); the real trigger and
event source are open question OQ-01/Q-11.

Run with: uv run python -m demo.full_flow
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sub_agent.api.container import build_container
from sub_agent.api.settings import Settings
from sub_agent.compare_sources import DiscrepancyDecision
from sub_agent.intake.email_intake import (
    IntakeOutcome,
    handle_acceptance_email,
    handle_form_approved,
)
from sub_agent.models import (
    FormStatus,
    OfferRecord,
    OnboardingForm,
    Role,
    SourceSnapshot,
    Viewer,
    WorkLocationType,
)

EMAILS_DIR = Path(__file__).parent / "emails"
SALARY_A = "Base: 118000 SYNTHETIC/yr; benefits: standard"
SALARY_B = "Base: 102000 SYNTHETIC/yr; benefits: standard"
PRINTED: list[str] = []


def say(line: str) -> None:
    print(line)
    PRINTED.append(line)


def fail(message: str) -> None:
    print(f"\nFAILURE: {message}")
    sys.exit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def _viewers(client_id: str, hr_officer_id: str) -> dict[str, Viewer]:
    return {
        "HR_OFFICER": Viewer(user_id=hr_officer_id, role=Role.HR_OFFICER, client_ids=frozenset({client_id})),
        "HR_MANAGER": Viewer(
            user_id="hr-manager-demo-1", role=Role.HR_MANAGER, client_ids=frozenset({client_id})
        ),
    }


def _seed_offer_and_form(
    container, client_id: str, offer_ref: str, hr_officer_id: str, candidate_email: str,
    candidate_name: str, salary_and_terms: str, form_status: FormStatus,
) -> None:
    offer = OfferRecord(
        client_id=client_id, offer_ref=offer_ref, candidate_email=candidate_email, hr_officer_id=hr_officer_id
    )
    container.intake_deps.offer_repo.save(offer)

    form_kwargs = dict(
        client_id=client_id,
        version=1,
        candidate_name=candidate_name,
        candidate_email=candidate_email,
        designation="Software Engineer",
        joining_date=date(2026, 4, 1),
        work_location_type=WorkLocationType.OFFICE,
        salary_and_terms=salary_and_terms,
    )
    form = OnboardingForm(**form_kwargs)
    if form_status == FormStatus.APPROVED:
        form = form.approve(approved_by="hr-officer-demo-approver", approved_at=datetime.now(timezone.utc))
    container.intake_deps.form_source.save_for_offer(client_id, offer_ref, form)


def path_a(container) -> str:
    say("\n=== Path A: offer accepted, form already approved ===")
    # OFFER-1001 (and the fixed .eml files) are reserved for Path C below,
    # which owns find_by_ref lookups against those files; Path A uses its
    # own offer ref so the two paths' offers never collide.
    client_id = "client-demo-a"
    offer_ref = "OFFER-1000"
    hr_officer_id = "hr-officer-demo-a"
    candidate_email = "morgan.synthetic.candidate@example.test"
    viewers = _viewers(client_id, hr_officer_id)

    _seed_offer_and_form(
        container, client_id, offer_ref, hr_officer_id, candidate_email,
        "Morgan Synthetic Candidate", SALARY_A, FormStatus.APPROVED,
    )
    say(f"seeded offer {offer_ref} with an already-APPROVED form")

    raw = (
        f"From: {candidate_email}\n"
        f"Subject: Re: Your offer [{offer_ref}] - I accept\n\n"
        "Synthetic acceptance body for the demo.\n"
    )
    result = handle_acceptance_email(raw, container.intake_deps)
    say(f"inbound acceptance email -> {result.outcome.value} (case_id={result.case_id})")
    expect(result.outcome == IntakeOutcome.CASE_OPENED, f"expected CASE_OPENED, got {result.outcome}")
    case_id = result.case_id

    compare_result = container.service.compare_sources(
        viewers["HR_OFFICER"],
        case_id,
        [SourceSnapshot(source="hr_system", values={"candidate_name": "Taylor Different Legalname"})],
    )
    say(f"compare against hr_system snapshot (name differs) -> {len(compare_result.new_discrepancies)} new discrepancy")
    expect(len(compare_result.new_discrepancies) == 1, "expected exactly one discrepancy from the hr_system mismatch")
    discrepancy_id = compare_result.new_discrepancies[0]

    container.service.resolve_discrepancy(viewers["HR_OFFICER"], discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE)
    say("HR officer keeps the approved form's name (hr_system mismatch resolved)")

    prepare_result = container.service.prepare_hris(viewers["HR_OFFICER"], case_id)
    say(f"prepare HRIS records -> {prepare_result.outcome.value}")
    expect(prepare_result.outcome.value == "PREPARED", f"expected PREPARED, got {prepare_result.outcome}")

    container.service.decide_hris_write(viewers["HR_MANAGER"], prepare_result.approval_id, True, "approved for demo")
    say("HR manager approves the HC-05 HRIS write")

    write_outcome = container.service.execute_hris_write(viewers["HR_MANAGER"], case_id)
    say(f"execute HRIS write -> completed={write_outcome.completed}")
    expect(write_outcome.completed, "expected the HRIS write to complete")

    status = container.service.get_status(viewers["HR_MANAGER"], case_id)
    say(f"case status -> {status.stage.value}")
    expect(status.stage.value == "RECORD_CREATED", f"expected RECORD_CREATED, got {status.stage}")

    audit = container.service.get_audit_trail(viewers["HR_MANAGER"], case_id)
    say("audit trail (case-scoped, in order):")
    for entry in audit:
        say(f"  - {entry.action}")

    return case_id


def path_b(container) -> None:
    say("\n=== Path B: acceptance arrives before the form is approved ===")
    client_id = "client-demo-b"
    offer_ref = "OFFER-2002"
    hr_officer_id = "hr-officer-demo-b"
    candidate_email = "jordan.synthetic.candidate@example.test"

    _seed_offer_and_form(
        container, client_id, offer_ref, hr_officer_id, candidate_email,
        "Jordan Synthetic Candidate", SALARY_B, FormStatus.DRAFT,
    )
    say(f"seeded offer {offer_ref} with only a DRAFT form (not yet approved)")

    raw = (
        f"From: {candidate_email}\n"
        f"Subject: Re: Your offer [{offer_ref}] - I accept\n\n"
        "Synthetic acceptance body for the demo.\n"
    )
    result = handle_acceptance_email(raw, container.intake_deps)
    say(f"inbound acceptance email -> {result.outcome.value}, reasons={result.reasons}")
    expect(result.outcome == IntakeOutcome.WAITING_FOR_FORM, f"expected WAITING_FOR_FORM, got {result.outcome}")

    form = container.intake_deps.form_source.latest_for_offer(client_id, offer_ref)
    approved_form = form.approve(approved_by="hr-officer-demo-b-approver", approved_at=datetime.now(timezone.utc))
    container.intake_deps.form_source.save_for_offer(client_id, offer_ref, approved_form)
    say("(simulating HR approving the onboarding form upstream)")

    started = handle_form_approved(client_id, offer_ref, container.intake_deps)
    say(f"dev approve-form -> {started.outcome.value} (case_id={started.case_id})")
    expect(started.outcome == IntakeOutcome.CASE_OPENED, f"expected CASE_OPENED, got {started.outcome}")
    case_id = started.case_id

    viewers = _viewers(client_id, hr_officer_id)
    prepare_result = container.service.prepare_hris(viewers["HR_OFFICER"], case_id)
    expect(prepare_result.outcome.value == "PREPARED", f"expected PREPARED, got {prepare_result.outcome}")
    container.service.decide_hris_write(viewers["HR_MANAGER"], prepare_result.approval_id, True, "approved for demo")
    write_outcome = container.service.execute_hris_write(viewers["HR_MANAGER"], case_id)
    expect(write_outcome.completed, "expected the HRIS write to complete")

    status = container.service.get_status(viewers["HR_MANAGER"], case_id)
    say(f"continued to -> {status.stage.value}")
    expect(status.stage.value == "RECORD_CREATED", f"expected RECORD_CREATED, got {status.stage}")


def path_c(container) -> None:
    say("\n=== Path C: guardrails ===")
    # OFFER-1001 is the ref the three fixed .eml files are written
    # against; this path owns it exclusively (see path_a's note above).
    client_id = "client-demo-c"
    offer_ref = "OFFER-1001"
    hr_officer_id = "hr-officer-demo-c"
    candidate_email = "taylor.synthetic.candidate@example.test"

    _seed_offer_and_form(
        container, client_id, offer_ref, hr_officer_id, candidate_email,
        "Taylor Synthetic Candidate", "Base: 77000 SYNTHETIC/yr; benefits: standard", FormStatus.APPROVED,
    )

    wrong_sender_raw = (EMAILS_DIR / "acceptance_wrong_sender.eml").read_text()
    result = handle_acceptance_email(wrong_sender_raw, container.intake_deps)
    say(f"wrong sender email -> {result.outcome.value} / {result.rejection_code}")
    expect(result.outcome == IntakeOutcome.REJECTED, "expected REJECTED for the wrong-sender email")
    expect(result.rejection_code == "SENDER_MISMATCH", f"expected SENDER_MISMATCH, got {result.rejection_code}")

    no_ref_raw = (EMAILS_DIR / "acceptance_no_ref.eml").read_text()
    result = handle_acceptance_email(no_ref_raw, container.intake_deps)
    say(f"email with no offer reference -> {result.outcome.value} / {result.rejection_code}")
    expect(result.outcome == IntakeOutcome.REJECTED, "expected REJECTED for the no-ref email")
    expect(result.rejection_code == "NO_OFFER_REF", f"expected NO_OFFER_REF, got {result.rejection_code}")

    decline_raw = (EMAILS_DIR / "decline.eml").read_text()
    result = handle_acceptance_email(decline_raw, container.intake_deps)
    say(f"decline email from the correct sender -> {result.outcome.value} / {result.review_code}")
    expect(result.outcome == IntakeOutcome.NEEDS_HR_REVIEW, f"expected NEEDS_HR_REVIEW, got {result.outcome}")
    expect(result.case_id is None, "expected no case to be created for a decline")

    ok_raw = (EMAILS_DIR / "acceptance_ok.eml").read_text()
    first = handle_acceptance_email(ok_raw, container.intake_deps)
    say(f"legitimate acceptance email -> {first.outcome.value}")
    expect(first.outcome == IntakeOutcome.CASE_OPENED, f"expected CASE_OPENED, got {first.outcome}")

    second = handle_acceptance_email(ok_raw, container.intake_deps)
    say(f"the same acceptance email again -> {second.outcome.value}")
    expect(
        second.outcome == IntakeOutcome.DUPLICATE_ACCEPTANCE,
        f"expected DUPLICATE_ACCEPTANCE, got {second.outcome}",
    )


def main() -> int:
    settings = Settings(app_env="dev", dedup_hmac_key="demo-fixed-fingerprint-key", system_api_key="demo-system-key")
    container = build_container(settings)

    path_a(container)
    path_b(container)
    path_c(container)

    for sensitive in (SALARY_A, SALARY_B):
        for line in PRINTED:
            if sensitive in line:
                fail(f"salary text leaked into printed output: {line!r}")
    say("\nsalary text appears in no printed output - OK")

    say("\nALL PATHS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
