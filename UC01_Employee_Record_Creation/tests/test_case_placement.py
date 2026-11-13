"""Placement attributes on a case (approved decisions, 2026-11-04).

    location_name        required from the caller
    department           optional, populate later
    grade                optional, populate later
    nationality_group    optional, populate later
    project_name         optional, populate later
    current_stage        default INTAKE when omitted
    onboarding_attributes default empty when omitted

These are live onboarding_cases columns that UC-01 previously had no
representation for at all. Everything here is in-memory; no database is
involved and none of it is database-verified.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factories import make_approved_form, make_deps, make_placement
from sub_agent.adapters.memory import InMemoryAuditLog, InMemoryPersonDirectory
from sub_agent.duplicate_check import DuplicateCheckRequest, check_duplicates
from sub_agent.models import Case, CasePlacement
from sub_agent.models.case import DEFAULT_CURRENT_STAGE
from sub_agent.open_case import create_case

CLIENT = "client-placement-001"
OPTIONAL_FIELDS = ["department", "grade", "nationality_group", "project_name"]


def _open(deps, placement: CasePlacement) -> Case:
    form = make_approved_form(client_id=CLIENT)
    result = check_duplicates(
        DuplicateCheckRequest(
            request_id="req-placement",
            client_id=CLIENT,
            candidate_name=form.candidate_name,
            candidate_email=form.candidate_email,
        ),
        deps.person_directory,
        deps.audit_log,
    )
    case, _ = create_case(
        form=form,
        hr_officer_id="hr-officer-1",
        id_number=None,
        duplicate_result=result,
        declared_variants=None,
        placement=placement,
        case_repo=deps.case_repo,
        form_repo=deps.form_repo,
        approval_repo=deps.approval_repo,
        person_directory=deps.person_directory,
        folder_store=deps.document_store,
        number_generator=deps.number_generator,
        audit_log=deps.audit_log,
    )
    return case


# --------------------------------------------------- 1. all optional omitted


def test_a_case_opens_with_every_optional_field_omitted():
    """The headline decision: only location_name is needed at intake."""
    deps = make_deps()
    case = _open(deps, CasePlacement(location_name="Dubai Office"))

    assert case.location_name == "Dubai Office"
    for name in OPTIONAL_FIELDS:
        assert getattr(case, name) is None
    assert case.current_stage == "INTAKE"
    assert case.onboarding_attributes == []
    # ...and it really is persisted, not just returned.
    assert deps.case_repo.get(case.case_id).location_name == "Dubai Office"


# ------------------------------------ 2/3. optional omitted, NULL, supplied


@pytest.mark.parametrize("name", OPTIONAL_FIELDS)
def test_each_optional_field_may_be_omitted(name):
    case = _open(make_deps(), CasePlacement(location_name="Dubai Office"))
    assert getattr(case, name) is None


@pytest.mark.parametrize("name", OPTIONAL_FIELDS)
def test_each_optional_field_accepts_an_explicit_null(name):
    case = _open(make_deps(), CasePlacement(location_name="Dubai Office", **{name: None}))
    assert getattr(case, name) is None


@pytest.mark.parametrize("name", OPTIONAL_FIELDS)
def test_a_supplied_optional_value_is_preserved(name):
    case = _open(
        make_deps(), CasePlacement(location_name="Dubai Office", **{name: "Supplied Value"})
    )
    assert getattr(case, name) == "Supplied Value"


@pytest.mark.parametrize("name", OPTIONAL_FIELDS)
def test_a_whitespace_only_optional_value_becomes_null(name):
    """One representation for 'not known yet'. '   ' and NULL must not
    both be storable, or every later query needs to handle both."""
    case = _open(make_deps(), CasePlacement(location_name="Dubai Office", **{name: "   "}))
    assert getattr(case, name) is None


# ------------------------------------------- 4/5/6. location_name required


def test_missing_location_name_is_rejected():
    with pytest.raises(ValidationError) as err:
        CasePlacement()
    assert "location_name" in str(err.value)


def test_null_location_name_is_rejected():
    with pytest.raises(ValidationError):
        CasePlacement(location_name=None)


@pytest.mark.parametrize("value", ["", "   ", "\t", "\n  \n"])
def test_empty_and_whitespace_only_location_name_are_rejected(value):
    with pytest.raises(ValidationError) as err:
        CasePlacement(location_name=value)
    assert "location_name" in str(err.value)


def test_location_name_is_trimmed():
    assert CasePlacement(location_name="  Dubai Office  ").location_name == "Dubai Office"


def test_the_case_model_enforces_location_name_too():
    """create_case is not the only way a Case is built, so the model
    that actually gets persisted carries the rule as well."""
    with pytest.raises(ValidationError):
        Case(
            client_id=CLIENT,
            candidate_name="X",
            candidate_email="x@example.test",
            hr_officer_id="hr-1",
            joining_date="2026-03-02",
            work_location_type="OFFICE",
            current_form_version=1,
            location_name="   ",
        )


# --------------------------------------------- 7/8/9. the defaulted fields


def test_omitted_current_stage_defaults_to_intake():
    case = _open(make_deps(), CasePlacement(location_name="Dubai Office"))
    assert case.current_stage == DEFAULT_CURRENT_STAGE == "INTAKE"


def test_explicitly_null_current_stage_also_takes_the_default():
    """An API body renders an omitted field as None, so None has to mean
    'omitted' here rather than failing."""
    assert CasePlacement(location_name="L", current_stage=None).current_stage == "INTAKE"


def test_a_supplied_current_stage_is_preserved():
    assert CasePlacement(location_name="L", current_stage="SCREENING").current_stage == "SCREENING"


def test_omitted_onboarding_attributes_defaults_to_empty():
    case = _open(make_deps(), CasePlacement(location_name="Dubai Office"))
    assert case.onboarding_attributes == []


def test_explicitly_null_onboarding_attributes_takes_the_default():
    assert CasePlacement(location_name="L", onboarding_attributes=None).onboarding_attributes == []


def test_supplied_onboarding_attributes_are_preserved():
    placement = CasePlacement(location_name="L", onboarding_attributes=["relocation", "visa"])
    assert placement.onboarding_attributes == ["relocation", "visa"]


def test_the_empty_default_is_not_shared_between_cases():
    """A mutable default would be shared by every instance - appending to
    one case's attributes would silently change every other case."""
    a = CasePlacement(location_name="A")
    b = CasePlacement(location_name="B")
    assert a.onboarding_attributes is not b.onboarding_attributes
    a.onboarding_attributes.append("relocation")
    assert b.onboarding_attributes == []


# ------------------------------- 10. NULL vs empty collection in payloads


def test_a_payload_distinguishes_null_from_an_empty_collection():
    """SQL NULL ("not known") and an empty collection ("known to be
    none") are different states and must serialise differently.

    NOTE: there is no Supabase case adapter yet - UnsupportedCaseRepository
    still refuses - so this asserts the model's own serialisation, which
    is what any adapter will build its row from.
    """
    case = _open(make_deps(), CasePlacement(location_name="Dubai Office"))
    dumped = case.model_dump(mode="json")

    for name in OPTIONAL_FIELDS:
        assert dumped[name] is None, f"{name} should serialise as NULL, not a placeholder"
    assert dumped["onboarding_attributes"] == []
    assert dumped["onboarding_attributes"] is not None
    assert dumped["current_stage"] == "INTAKE"
    assert dumped["location_name"] == "Dubai Office"


def test_supplied_optional_values_survive_serialisation():
    case = _open(
        make_deps(),
        CasePlacement(
            location_name="Dubai Office",
            department="Engineering",
            grade="G7",
            nationality_group="GCC",
            project_name="Apollo",
            onboarding_attributes=["relocation"],
        ),
    )
    dumped = case.model_dump(mode="json")
    assert dumped["department"] == "Engineering"
    assert dumped["grade"] == "G7"
    assert dumped["nationality_group"] == "GCC"
    assert dumped["project_name"] == "Apollo"
    assert dumped["onboarding_attributes"] == ["relocation"]


# ------------------------------------------- 11. later population (gap)


def test_no_authorized_update_path_populates_the_optional_fields_yet():
    """Documents a GAP rather than asserting a feature.

    The four optional fields are meant to be populated later, but no
    UC-01 path does that today: a Case is only re-saved by
    form_changes.approve_form_change (which copies FORM fields) and
    variants.decide_variant (which sets variant flags). Neither touches
    department/grade/nationality_group/project_name.

    Inventing an update endpoint here would mean inventing an API
    contract, so this test pins the current reality. When a real update
    path lands it must reuse the existing RBAC, client-scoping and audit
    mechanisms - and this test should be replaced by one that exercises
    it.
    """
    import inspect

    from sub_agent import form_changes, service, variants

    writers = []
    for module in (form_changes, variants, service):
        source = inspect.getsource(module)
        for name in OPTIONAL_FIELDS:
            if f"{name}=" in source or f'"{name}"' in source:
                writers.append(f"{module.__name__}:{name}")

    assert writers == [], (
        "a path now writes the optional placement fields - replace this "
        f"gap test with one that checks its authorization and audit: {writers}"
    )


def test_the_intake_simulator_refuses_rather_than_inventing_a_location():
    """location_name has no source in an offer or a form, so the
    [Proposed] email-intake simulator parks the offer instead of making
    one up."""
    from sub_agent.intake.email_intake import IntakeOutcome, try_start
    from factories import make_intake_deps
    from sub_agent.models import OfferRecord

    deps = make_intake_deps()
    deps.placement = None  # as it is outside dev
    deps.offer_repo.save(
        OfferRecord(
            client_id=CLIENT,
            offer_ref="OFFER-9001",
            candidate_email="x@example.test",
            hr_officer_id="hr-1",
        )
    )

    result = try_start(CLIENT, "OFFER-9001", deps)
    assert result.outcome is IntakeOutcome.WAITING_FOR_FORM
    assert "location_name_required" in result.reasons


def test_tenant_isolation_is_unaffected_by_the_new_fields():
    """Guard against the new fields widening a lookup."""
    directory = InMemoryPersonDirectory()
    audit = InMemoryAuditLog()
    result = check_duplicates(
        DuplicateCheckRequest(
            request_id="r",
            client_id="client-other",
            candidate_name="X",
            candidate_email="x@example.test",
        ),
        directory,
        audit,
    )
    assert result.matched_record_ids == []
