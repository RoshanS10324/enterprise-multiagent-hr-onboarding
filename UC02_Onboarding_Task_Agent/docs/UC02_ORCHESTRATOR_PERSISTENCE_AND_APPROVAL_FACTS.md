# UC-02: orchestrator persistence and offer/form approval facts

Status: **PROPOSAL.** Nothing here changes shared code, other use cases, schemas, migrations or RLS.
Everything below needs the decisions listed at the end.

## A. The orchestrator keeps UC-02 in memory

### Facts (read from code)
- `HR-Onboarding-Common/.../adapters/uc02_tasks.py` builds `UC02SubAgent()` and caches it in
  `deps._uc02_sub_agent`. `UC02SubAgent()` with no argument creates `OnboardingService(build_registry())`:
  no store, no audit sink, no approvals.
- The orchestrator's own case store (`InMemoryCaseStateStore` / optional `SqliteCaseStateStore`) keeps the
  orchestrator's *projection* (`task_plan`, stage status). After a restart it resumes from that projection and
  **skips** `plan_tasks` because the stage is COMPLETE.
- Result of a restart today: the orchestrator believes a plan exists, but UC-02's records, audit trail and approvals
  are gone, so later UC-02 calls find no onboarding. This is an inconsistency, not just a missing feature.

### What already exists in UC-02 (reuse, do not rebuild)
- `OnboardingStore` over a `TableClient` port, with `InMemoryTableClient` and `SqliteTableClient`.
- `sub_agent/runtime.build_service(environ)`: picks the store from `UC02_STORE_MODE` / `UC02_SQLITE_PATH`, turns the
  HR plan gate on, and **never falls back to memory** if SQLite was requested.
- Demonstrated (not shipped): `OrchestratorDurabilityTests` in `UC02_Onboarding_Task_Agent/tests/test_orchestrated_e2e.py`
  fills `deps._uc02_sub_agent` from outside with a SQLite-backed service; the orchestrated plan then survives a restart.

### Smallest compatible change (for the orchestrator owner to approve)
In `uc02_tasks.py`, replace the one construction site:

```python
# today
sub_agent = deps._uc02_sub_agent or UC02SubAgent()
# proposed
sub_agent = deps._uc02_sub_agent or UC02SubAgent(service=build_service())
```

`build_service()` reads the environment: with no `UC02_STORE_MODE` it behaves exactly as today (no durable store),
so existing deployments and tests are unaffected. The service identities for UC-03/04/05 reports
(`source_principals`) are a separate, already-open decision.

### Why this is safe
- Idempotent: if the orchestrator re-runs `plan_tasks` after a crash between UC-02's save and its own save,
  UC-02 returns the existing onboarding (`created=false`); it never creates a second one.
- No new contract: the adapter's inputs and outputs are unchanged.
- Explicit failure: a SQLite mode that cannot open raises `PersistenceError`; the stage reports it.

### Not part of this proposal (needs TL)
- Production storage. The approved production database is Azure Cosmos DB and the shared development database is
  Supabase PostgreSQL. A `TableClient` for either is a new adapter behind the same port and needs the schema decisions
  in the database review (shared `case_id`, columns for new fields, status vocabulary, RLS) first.
- Where the orchestrator gets its storage configuration from (environment, `worker_config.yaml`, a settings object).
- Whether UC-02 and the orchestrator should share one transaction boundary (they do not today).

### Tests to add once approved
- Adapter test: with `UC02_STORE_MODE=sqlite`, planning writes a row; with no mode, it writes none.
- Restart test through the real orchestrator (real workers): plan, restart both, resume, UC-02 record present.
- Failure test: unopenable store -> stage not COMPLETE and the error is visible.

## B. The offer-acceptance and form-approval timestamps

### What UC-02 needs
`offer.accepted_at`, `form.approved_at` and the approver's role, as **facts verified upstream**.
UC-02 refuses to plan without them and no longer assumes them.

### Who owns them (evidence)
**UC-01 (Employee Record Creation).**
- Offer: `UC01.../models/offer.py` has `OfferRecord.accepted_at` and `OfferAcceptedEvent.accepted_at`. The module is
  labelled a dev simulator; the real trigger and event source are UC-01's open question OQ-01 / Q-11.
- Form: `models/onboarding_form.py` has `status`, `approved_by`, `approved_at`, and rejects an APPROVED form without
  an approver and a timestamp. UC-01's start gate refuses to open a case without an APPROVED form.
- Live shared tables carry the same facts: `offers.accepted_at`, `onboarding_forms.approved_by` / `approved_at`.

### Where the chain breaks today
1. UC-01's `open_onboarding_case` takes `offer_accepted: bool` and returns a `Case` / outcome. **It returns neither
   timestamp, nor the approver.**
2. The orchestrator's `OnboardingCaseInput` has no such fields (UC-02's adapter now reads three optional ones:
   `offer_accepted_at`, `form_approved_at`, `form_approved_by_role`, from the caller).
3. `orchestrator_agent.intake_inputs_for` builds the UC-01 form with `approved_by = hr_officer_id` and
   `approved_at = now`. That is an orchestrator-invented approval stamp, not an upstream fact.

### Recommended owner and change
- **UC-01 owns the facts.** It should return, in its open-case result or a read operation: the offer's `accepted_at`,
  the form's `approved_by`, `approved_at` and the approver's role.
- **The orchestrator carries them.** `uc02_tasks.py` should read them from the UC-01 result held in state, not from
  caller input, and `intake_inputs_for` should stop stamping `approved_at=now`.
- **UC-02 keeps validating.** `validate_preconditions` already checks acceptance, timestamp, submission,
  completeness, approval and an HR approver role. No change needed there.

### Interim behaviour (already in place)
Caller supplies the three optional fields; if absent, planning is BLOCKED with `MISSING_DATA` and nothing is created.
Tested in `test_uc02_adapter.py` and `test_uc02_real_orchestration.py`.

## Decisions required
| # | Decision | Owner |
|---|---|---|
| 1 | Approve the one-line adapter change in section A | Orchestrator owner / TL |
| 2 | Storage configuration source for the orchestrator | Orchestrator owner / TL |
| 3 | Production adapter (Cosmos) and shared-dev adapter (Supabase) and their schema decisions | TL, UC-01/03/04/05 owners |
| 4 | UC-01 returns offer `accepted_at` and form approval `approved_by` / `approved_at` / role | UC-01 owner |
| 5 | Orchestrator reads them from UC-01 and stops fabricating the form approval stamp | Orchestrator owner |
| 6 | How UC-01 represents the approver's role so UC-02 can map it to its HR roles | UC-01 owner / TL |
