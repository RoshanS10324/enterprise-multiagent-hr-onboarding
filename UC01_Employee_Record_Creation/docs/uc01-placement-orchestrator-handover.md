# Handover: UC-01 placement in the shared orchestrator

**For:** the owner of the HR-Onboarding-Common orchestration code (Prasanna).
**Status:** UC-01's side is done. The two shared files below are **not**
changed. An HR decision is still open (§3).

## 1. The problem

UC-01's `OpenCaseRequest` carries a `placement: CasePlacement`.
`location_name` is required: decision D3 (2026-11-04) says it is "required
from the caller", because the live `onboarding_cases.location_name` column
is NOT NULL. The orchestrator never sends a placement:

- `orchestration/adapters/uc01_intake.py` (`call_uc01_intake`, lines 46-54)
  builds the payload without placement and without `declared_variants`.
- `orchestration/workers/uc_worker_runtime.py` (`_Uc01Handler.dispatch`,
  lines 109-117) builds `OpenCaseRequest(...)` without either field.

Before this change, pydantic rejected the request (`placement Field
required`), intake was marked **FAILED** (`INVALID_INPUT`), and every
full-orchestrator test stopped before reaching UC-02.

## 2. What UC-01 does now (no shared change needed for this part)

When there is no placement, `open_onboarding_case` returns **HELD** with
reason `location_name_required` and creates nothing. Nothing is invented.
The hold comes after the duplicate check, so a repeat request still links
to an existing case. The orchestrator's existing HELD branch therefore marks
intake **BLOCKED**, with next action `hr_review:intake:held` and a blocker
`location_name_required`. It no longer marks it FAILED.

Expected effect on the shared tests, until §3 is decided:

- `test_uc01_adapter::test_missing_form_is_held_not_treated_as_approved`
  should pass again.
- Tests that expect intake **COMPLETE** (for example
  `test_case_opened_outcome_completes_intake_stage`, `test_orchestrator_e2e`
  and `test_uc02_real_orchestration`) will see **BLOCKED** instead. That is
  correct while no placement is sent.

## 3. The open decision (HR data owner)

Does the orchestrator's `OnboardingCaseInput.location` (tests use values
such as `"Dubai"`) mean the same as UC-01's `location_name` (for example
`"Dubai Office"`)? **Current decision: undecided. Do not map
automatically.**

## 4. The change, once HR decides

### 4a. `orchestration/adapters/uc01_intake.py`, `call_uc01_intake`

Add the placement and variant fields to the payload. Map only what HR
confirms. Use the field names that `CasePlacement` defines
(`src/sub_agent/models/case.py`):

```python
placement_src = state.get("uc01_placement") or {}   # however the confirmed source is carried
payload = {
    ...,                                   # existing keys unchanged
    "placement": ({
        "location_name": placement_src["location_name"],
        "department": placement_src.get("department"),
        "grade": placement_src.get("grade"),
        "nationality_group": placement_src.get("nationality_group"),
        "project_name": placement_src.get("project"),
        "onboarding_attributes": list(placement_src.get("attributes") or []),
    } if placement_src.get("location_name") else None),
    "declared_variants": trigger.get("declared_variants"),
}
```

If `location_name` is missing, either send `placement: None` (UC-01 returns
HELD, which becomes BLOCKED) or block before the call with `MISSING_DATA`.
Never default it, and never send `"Headquarters"` or a city name as a
stand-in.

If HR confirms that `location` **is** `location_name`, the source is
`OnboardingCaseInput.location`, `department`, `grade`, `nationality_group`,
`project` and `attributes` (`contracts.py:44-53`). They are already in
`uc02_role_context`. Otherwise, add a distinct `location_name` field to
`OnboardingCaseInput`.

### 4b. `orchestration/workers/uc_worker_runtime.py`, `_Uc01Handler.dispatch`

```python
from sub_agent.models import CasePlacement, VariantFlags  # type: ignore

placement = payload.get("placement")
variants = payload.get("declared_variants")
request = OpenCaseRequest(
    ...,                                   # existing arguments unchanged
    placement=CasePlacement.model_validate(placement) if placement else None,
    declared_variants=VariantFlags.model_validate(variants) if variants else None,
)
```

`CasePlacement` validates the values itself: it trims them, rejects a blank
`location_name`, maps blank optional fields to NULL, and applies the
`current_stage` and `onboarding_attributes` defaults.

## 5. Tests to add on the shared side

- Adapter: the payload carries placement when `location_name` is known, and
  carries `None` (or blocks with `MISSING_DATA`) when it is not.
- Worker handler: builds `CasePlacement` and `VariantFlags` from the
  payload. A missing placement gives the outcome `HELD` with
  `location_name_required`.
- Full orchestrator, with real workers: a case with confirmed placement
  completes intake and reaches UC-02's `plan_tasks`. A case without it is
  BLOCKED at intake and never reaches UC-02.

Run `tests/orchestration` with real workers. The UC `.venv`s are missing on
the current machine; point `UC01_WORKER_PYTHON` / `UC04_WORKER_PYTHON` /
`UC05_WORKER_PYTHON` at an interpreter that has each UC's dependencies, and
blank the Supabase and provider variables so no worker reaches a real
system.
