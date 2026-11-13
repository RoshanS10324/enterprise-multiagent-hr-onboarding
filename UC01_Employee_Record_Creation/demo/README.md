# demo

A narrated, runnable walkthrough of UC-01's offer-acceptance-email
trigger simulator end to end: an inbound acceptance email to a created
employee record.

**[Proposed] Dev simulator only.** The email trigger, its transport, and
who creates/approves the real onboarding form are open question
OQ-01/Q-11 - see `src/sub_agent/intake/email_intake.py` and
`docs/known-gaps.md`.

## Running it

```
uv sync
uv run python -m demo.full_flow
```

It builds a fresh in-memory container (synthetic data only, no network
calls) and runs three paths, printing one line per step:

- **Path A** - the onboarding form is already approved when the
  acceptance email arrives: the case opens immediately, an HR-system
  snapshot flags a name discrepancy, HR keeps the form value, and the
  case runs through to a written HRIS record.
- **Path B** - the acceptance email arrives before the form is approved:
  the case is held `WAITING_FOR_FORM`, then starts automatically once the
  form is approved (simulating `POST /dev/approve-form`).
- **Path C** - guardrails: a wrong-sender email is rejected, an email
  with no offer reference is rejected, a decline/negotiation email from
  the correct sender is routed to `NEEDS_HR_REVIEW` with no case created,
  and the same acceptance email sent twice is recognized as a duplicate,
  not a second case.

It exits 0 and prints `ALL PATHS OK` if every check passes, or exits 1 at
the first failure. It also asserts the synthetic salary text never
appears in anything it prints.

## Doing the same thing by hand

The source emails are in `demo/emails/`:

- `acceptance_ok.eml` - a legitimate acceptance from the candidate, with
  `[OFFER-1001]` in the subject.
- `acceptance_wrong_sender.eml` - the same subject, but From a different
  address than the offer's candidate.
- `acceptance_no_ref.eml` - a subject with no offer reference at all.
- `decline.eml` - from the correct sender, but the subject says "I must
  decline": a `NEEDS_HR_REVIEW` outcome (code
  `POSSIBLE_DECLINE_OR_NEGOTIATION`), not an acceptance. The subject is
  the only thing inspected for this - the body is never read or stored,
  so a subject this plain is deliberately the whole story.

With the API running (see the repository [README.md](../README.md)) and
`UC01_SYSTEM_API_KEY` set, you can drive the same flow over HTTP:

```
# 1. Seed an offer and its onboarding form (dev only)
curl -X POST http://localhost:8000/dev/seed \
  -H "X-System-Key: $UC01_SYSTEM_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "client_id": "client-manual-001", "offer_ref": "OFFER-1001",
    "hr_officer_id": "hr-officer-manual-1",
    "candidate_name": "Taylor Synthetic Candidate",
    "candidate_email": "taylor.synthetic.candidate@example.test",
    "designation": "Software Engineer", "joining_date": "2026-04-01",
    "work_location_type": "OFFICE",
    "salary_and_terms": "Base: 100000 SYNTHETIC/yr; benefits: standard",
    "form_status": "APPROVED",
    "approved_by": "hr-officer-manual-approver",
    "approved_at": "2026-01-01T00:00:00+00:00"
  }'

# 2. Send the acceptance email (paste the .eml content as raw_email)
curl -X POST http://localhost:8000/internal/inbound-email \
  -H "X-System-Key: $UC01_SYSTEM_API_KEY" -H "Content-Type: application/json" \
  -d "{\"raw_email\": \"$(sed 's/"/\\"/g' demo/emails/acceptance_ok.eml | awk '{printf "%s\\n", $0}')\"}"

# 3. Check the result
curl http://localhost:8000/cases/<case_id>/status \
  -H "X-User-Id: hr-officer-manual-1" -H "X-User-Role: HR_OFFICER" \
  -H "X-Client-Ids: client-manual-001"
```

For Path B by hand: seed with `"form_status": "DRAFT"`, send the
acceptance email (expect `WAITING_FOR_FORM`), then call
`POST /dev/approve-form` with `{"client_id", "offer_ref", "approved_by"}`
to simulate HR approving the form - the case opens automatically.

[Proposed] offer refs are unique across clients: `/dev/seed` returns 409
if another client already seeded the same `offer_ref`, so pick a fresh
one per client when trying this by hand.

`GET /internal/pending-starts?client_id=...` (as an `HR_OFFICER` or
`HR_MANAGER` of that client) lists any offers still waiting on their
form.
