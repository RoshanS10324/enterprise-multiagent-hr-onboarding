import pytest

from sub_agent.compare_sources import (
    DiscrepancyDecision,
    compare_sources,
    resolve_discrepancy,
)
from sub_agent.models import ApprovalStatus, DiscrepancyStatus, SourceSnapshot

from factories import make_compare_deps, make_linked_case_and_form

CANDIDATE_NAME = "Jordan A. Lee"
SALARY = "Base: 120000 SYNTHETIC/yr; benefits: standard"


def test_no_differences_flags_nothing():
    case, form = make_linked_case_and_form(candidate_name=CANDIDATE_NAME, salary_and_terms=SALARY)
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="offer_letter", values={"candidate_name": CANDIDATE_NAME})

    result = compare_sources(case, form, [snapshot], deps)

    assert result.new_discrepancies == []
    assert result.already_known == 0
    assert result.approvals_created == []
    entries = deps.audit_log.entries()
    assert [e.action for e in entries] == ["COMPARE_COMPLETED"]


def test_name_differing_only_in_case_and_spacing_not_flagged():
    case, form = make_linked_case_and_form(candidate_name="Jordan   A.  Lee")
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="offer_letter", values={"candidate_name": "jordan a. lee"})

    result = compare_sources(case, form, [snapshot], deps)

    assert result.new_discrepancies == []


def test_work_location_differing_only_in_case_spacing_and_hyphens_not_flagged():
    case, form = make_linked_case_and_form(
        work_location_type="CLIENT_SITE", client_site_id="site-001"
    )
    deps = make_compare_deps()

    for variant in ("client site", "Client-Site", "  CLIENT_SITE  ", "client-site"):
        snapshot = SourceSnapshot(source="offer_letter", values={"work_location_type": variant})
        result = compare_sources(case, form, [snapshot], deps)
        assert result.new_discrepancies == [], f"unexpected flag for variant {variant!r}"


def test_name_difference_against_another_hr_record_is_flagged():
    """R6 against a non-document source. UC-01 compares the approved form
    with the offer and other HR records; identity-document checks belong
    to the document-validation use case."""
    case, form = make_linked_case_and_form(candidate_name=CANDIDATE_NAME)
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="hr_system", values={"candidate_name": "Jordan Alexander Lee"})

    result = compare_sources(case, form, [snapshot], deps)

    assert len(result.new_discrepancies) == 1
    assert len(result.approvals_created) == 1
    approval = deps.approval_repo.list_for_case(case.case_id)[0]
    assert approval.reason == "candidate_name differs between the approved form and hr_system"
    assert form.candidate_name == CANDIDATE_NAME  # form unchanged


def test_reason_is_source_agnostic():
    """No source name gets special handling. A document-shaped source
    reads exactly like any other, so no document rule can hide here."""
    case, form = make_linked_case_and_form(candidate_name=CANDIDATE_NAME)

    reasons = []
    for source in ("hr_system", "offer_letter", "passport"):
        deps = make_compare_deps()
        snapshot = SourceSnapshot(source=source, values={"candidate_name": "Jordan Alexander Lee"})
        compare_sources(case, form, [snapshot], deps)
        reasons.append(deps.approval_repo.list_for_case(case.case_id)[0].reason)

    assert reasons == [
        "candidate_name differs between the approved form and hr_system",
        "candidate_name differs between the approved form and offer_letter",
        "candidate_name differs between the approved form and passport",
    ]


def test_designation_joining_date_work_location_each_flagged_separately():
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()
    snapshots = [
        SourceSnapshot(source="hr_system", values={"designation": "Senior Engineer"}),
        SourceSnapshot(source="hr_system", values={"joining_date": "2026-02-01"}),
        SourceSnapshot(source="hr_system", values={"work_location_type": "CLIENT_SITE"}),
    ]

    result = compare_sources(case, form, snapshots, deps)

    assert len(result.new_discrepancies) == 3
    fields = {
        d.field
        for d in deps.discrepancy_repo.list_for_case(case.case_id)
    }
    assert fields == {"designation", "joining_date", "work_location_type"}


def test_unparseable_date_is_flagged():
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="hr_system", values={"joining_date": "not-a-date"})

    result = compare_sources(case, form, [snapshot], deps)

    assert len(result.new_discrepancies) == 1
    discrepancy = deps.discrepancy_repo.list_for_case(case.case_id)[0]
    assert discrepancy.field == "joining_date"
    assert discrepancy.value_other == "not-a-date"


def test_snapshot_missing_key_is_ignored():
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="offer_letter", values={})

    result = compare_sources(case, form, [snapshot], deps)

    assert result.new_discrepancies == []
    assert result.already_known == 0


def test_salary_difference_is_redacted_everywhere():
    case, form = make_linked_case_and_form(salary_and_terms=SALARY)
    deps = make_compare_deps()
    other_salary = "Base: 999999 SYNTHETIC/yr; benefits: platinum"
    snapshot = SourceSnapshot(source="payroll_system", values={"salary_and_terms": other_salary})

    result = compare_sources(case, form, [snapshot], deps)

    assert len(result.new_discrepancies) == 1
    discrepancy = deps.discrepancy_repo.get(result.new_discrepancies[0])
    assert discrepancy.value_form == "[REDACTED]"
    assert discrepancy.value_other == "[REDACTED]"

    assert SALARY not in str(discrepancy)
    assert other_salary not in str(discrepancy)
    assert SALARY not in str(discrepancy.model_dump())
    assert other_salary not in str(discrepancy.model_dump())

    approval = deps.approval_repo.list_for_case(case.case_id)[0]
    assert SALARY not in str(approval.model_dump())
    assert other_salary not in str(approval.model_dump())

    for entry in deps.audit_log.entries():
        dumped = str(entry.model_dump())
        assert SALARY not in dumped
        assert other_salary not in dumped

    assert SALARY not in repr(snapshot)
    assert other_salary not in repr(snapshot)


def test_rerunning_with_same_snapshots_creates_no_duplicates():
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Senior Engineer"})

    first = compare_sources(case, form, [snapshot], deps)
    second = compare_sources(case, form, [snapshot], deps)

    assert len(first.new_discrepancies) == 1
    assert second.new_discrepancies == []
    assert second.already_known == 1
    assert len(deps.discrepancy_repo.list_for_case(case.case_id)) == 1


def test_after_keep_form_value_same_value_not_reflagged_but_new_value_is():
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Senior Engineer"})

    first = compare_sources(case, form, [snapshot], deps)
    discrepancy_id = first.new_discrepancies[0]
    resolve_discrepancy(discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE, "hr-officer-001", deps)

    repeat = compare_sources(case, form, [snapshot], deps)
    assert repeat.new_discrepancies == []
    assert repeat.already_known == 1

    different_snapshot = SourceSnapshot(source="hr_system", values={"designation": "Staff Engineer"})
    changed = compare_sources(case, form, [different_snapshot], deps)
    assert len(changed.new_discrepancies) == 1


def test_accept_as_change_leaves_form_untouched_and_approves():
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Senior Engineer"})

    result = compare_sources(case, form, [snapshot], deps)
    discrepancy_id = result.new_discrepancies[0]
    approval_id = result.approvals_created[0]

    original_designation = form.designation
    decided = resolve_discrepancy(
        discrepancy_id, DiscrepancyDecision.ACCEPT_AS_CHANGE, "hr-officer-001", deps
    )

    assert form.designation == original_designation  # untouched
    assert decided.status == DiscrepancyStatus.ACCEPTED_AS_CHANGE
    assert decided.decided_by == "hr-officer-001"
    assert decided.decided_at is not None

    approvals = deps.approval_repo.list_for_case(case.case_id)
    approved = next(a for a in approvals if a.approval_id == approval_id)
    assert approved.status == ApprovalStatus.APPROVED
    assert approved.decided_by == "hr-officer-001"


def test_resolving_twice_raises():
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Senior Engineer"})
    result = compare_sources(case, form, [snapshot], deps)
    discrepancy_id = result.new_discrepancies[0]

    resolve_discrepancy(discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE, "hr-officer-001", deps)

    with pytest.raises(ValueError):
        resolve_discrepancy(discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE, "hr-officer-001", deps)


def test_resolving_unknown_id_raises():
    deps = make_compare_deps()
    with pytest.raises(ValueError):
        resolve_discrepancy("not-a-real-id", DiscrepancyDecision.KEEP_FORM_VALUE, "hr-officer-001", deps)


def test_form_not_approved_raises():
    case, _ = make_linked_case_and_form()
    deps = make_compare_deps()

    from factories import make_form

    draft = make_form(client_id=case.client_id).with_case(case.case_id)
    with pytest.raises(ValueError):
        compare_sources(case, draft, [], deps)


def test_dedup_is_scoped_to_form_version():
    # v1 designation A, source says B, HR resolves KEEP_FORM_VALUE.
    case, form_v1 = make_linked_case_and_form(designation="Engineer A")
    deps = make_compare_deps()
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})

    first = compare_sources(case, form_v1, [snapshot], deps)
    assert len(first.new_discrepancies) == 1
    resolve_discrepancy(
        first.new_discrepancies[0], DiscrepancyDecision.KEEP_FORM_VALUE, "hr-officer-001", deps
    )

    # Same "Engineer B" value from the same source, but against a newer
    # form version (designation changed to C by an unrelated edit): must
    # be flagged again, since the dedup key now includes form_version.
    form_v2 = form_v1.next_version(designation="Engineer C").approve(
        approved_by="hr-officer-001", approved_at=form_v1.approved_at
    )
    second = compare_sources(case, form_v2, [snapshot], deps)

    assert len(second.new_discrepancies) == 1
    assert second.already_known == 0


def test_form_of_another_case_raises():
    case, form = make_linked_case_and_form()
    deps = make_compare_deps()

    from factories import make_case

    other_case = make_case(client_id=form.client_id)
    with pytest.raises(ValueError):
        compare_sources(other_case, form, [], deps)
