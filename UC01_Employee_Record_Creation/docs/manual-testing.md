# UC-01 manual test runbook

Every command below was run against a live dev server and produced the
output shown. Synthetic data only.

## 0. Start the server

State lives in memory (`adapters/memory.py`), so **one server process = one
test session**. Restarting wipes every case, approval and audit entry.

```bash
uv sync
uv run uvicorn sub_agent.api:app --host 127.0.0.1 --port 8001
```

Leave it running in its own terminal. In a second terminal:

```bash
curl -s http://127.0.0.1:8001/health
# {"status":"ok"}
```

Interactive docs: <http://127.0.0.1:8001/docs>

### Headers

| Kind | Headers |
| --- | --- |
| System | `X-System-Key: change-me` (from `UC01_SYSTEM_API_KEY` in `.env`) |
| Human | `X-User-Id`, `X-User-Role`, `X-Client-Ids` |

Shell helpers used throughout (Git Bash, not PowerShell):

```bash
API=http://127.0.0.1:8001
SYS='-H X-System-Key:change-me -H Content-Type:application/json'
OFF='-H X-User-Id:officer-1 -H X-User-Role:HR_OFFICER -H X-Client-Ids:acme'
MGR='-H X-User-Id:manager-1 -H X-User-Role:HR_MANAGER -H X-Client-Ids:acme'
AUD='-H X-User-Id:aud-1 -H X-User-Role:AUDITOR -H X-Client-Ids:acme'
CLI='-H X-User-Id:cli-1 -H X-User-Role:CLIENT_USER -H X-Client-Ids:acme'
JSON='-H Content-Type:application/json'
```

There is no `jq` on this machine; pipe to `python -m json.tool` to pretty-print.

---

## Part A - the happy path, step by step

The record-creation path, end to end: open the case, change a detail,
approve it, prepare both HRIS records, write them. No source comparison
here - that is Part B.

### A1. Open a case (start gate + duplicate check + create)

```bash
curl -s -X POST $SYS $API/internal/onboarding-requests -d '{
  "client_id": "acme",
  "hr_officer_id": "officer-1",
  "offer_accepted": true,
  "candidate_name": "Asha Varma",
  "candidate_email": "asha.varma@example.invalid",
  "designation": "Software Engineer",
  "joining_date": "2026-04-01",
  "work_location_type": "OFFICE",
  "salary_and_terms": "Base: 118000 SYNTHETIC/yr",
  "form_status": "APPROVED",
  "approved_by": "manager-1",
  "approved_at": "2026-01-10T09:00:00Z"
}'
```

```json
{"outcome":"CASE_OPENED","case_id":"<CASE>","employee_no":"EMP-000001",
 "approvals_created":[],"existing_case_id":null,"reasons":[]}
```

Save it: `CASE=<case_id from above>`

**What to check:** `outcome=CASE_OPENED`, an `employee_no` was allocated, no
approvals raised (no variants, no duplicates).

### A2. Read the status

```bash
curl -s $OFF $API/cases/$CASE/status
```

`stage` is `READY_TO_PREPARE`, `form_version_in_force` is `1`,
`open_discrepancies` and `hris` are empty.

Nothing is blocking the record, so the next step is to prepare it (A5).
A3 and A4 show the one thing that can legitimately change the approved
details first: a form change.

### A3. Propose a form change (version 2)

The joiner asks to start a week later. Details are never edited in
place - a new form version is proposed and approved.

```bash
curl -s -X POST $OFF $JSON $API/cases/$CASE/form-changes -d '{
  "change_reason": "joiner asked to start a week later",
  "joining_date": "2026-04-06",
  "approver_id": "manager-1"
}'
```

```json
{"new_form_id":"...","new_version":2,"approval_id":"<FORM_AP>",
 "changed_fields":["joining_date"],"warnings":["joining date is in the past"]}
```

Save `FORM_AP`. Note `requested_by` is never read from the body - it always
comes from `X-User-Id` (sending it is a 422, not a silent ignore).

### A4. Approve the form change - and watch the policy layer first

```bash
# (a) wrong role: HC_01_FORM is HR_MANAGER/CLIENT_USER only
curl -s -X POST $OFF $JSON $API/form-changes/$FORM_AP/decision -d '{"approve":true}'
# 403 {"error":"policy_violation","code":"ROLE_NOT_ALLOWED"}

# (b) the proposer approving their own request
curl -s -X POST -H X-User-Id:officer-1 -H X-User-Role:CLIENT_USER \
  -H X-Client-Ids:acme $JSON $API/form-changes/$FORM_AP/decision -d '{"approve":true}'
# 403 {"error":"policy_violation","code":"SELF_APPROVAL"}

# (c) a different client's manager
curl -s -X POST -H X-User-Id:manager-9 -H X-User-Role:HR_MANAGER \
  -H X-Client-Ids:globex $JSON $API/form-changes/$FORM_AP/decision -d '{"approve":true}'
# 404 {"error":"case_not_found"}   <- NOT 403: existence must not leak

# (d) the real approver
curl -s -X POST $MGR $JSON $API/form-changes/$FORM_AP/decision -d '{"approve":true}'
# 200 {"approved":true,"form_version":2,"case_id":"..."}
```

**Now re-read the status.** `form_version_in_force` is `2` and
`joining_date` is `2026-04-06` - the case was updated by the cascade in
`approve_form_change`.

### A5. Prepare the HRIS records

```bash
curl -s -X POST $OFF $API/cases/$CASE/hris/prepare
# {"outcome":"PREPARED","record_ids":[<people>,<payroll>],"approval_id":"<HRIS_AP>"}
```

Status now reads `stage: HRIS_APPROVAL_PENDING`, with both
`ZOHO_PEOPLE` and `ZOHO_PAYROLL` at `PREPARED`.

**Run prepare again** - `ALREADY_PREPARED`, with the same record ids.

### A6. Try to write before approval

```bash
curl -s -X POST $MGR $API/cases/$CASE/hris/write
# 409 {"error":"conflict"}
```

### A7. Approve, then write

```bash
curl -s -X POST $MGR $JSON $API/hris-approvals/$HRIS_AP/decision -d '{"approve":true}'
# both records -> "status":"APPROVED"

curl -s -X POST $MGR $API/cases/$CASE/hris/write
# {"written":[people,payroll],"failed":[],"completed":true,"blocked_code":null}
```

`written` is ordered people-then-payroll. Status is now
`stage: RECORD_CREATED`, both systems `WRITTEN`, with an `external_ref`.

### A8. Prove the write is idempotent

```bash
curl -s -X POST $MGR $API/cases/$CASE/hris/write
# {"written":[],"skipped_already_written":[people,payroll],"completed":true}
```

### A9. Read the audit trail

```bash
curl -s $MGR $API/cases/$CASE/audit \
  | python -c "import sys,json;[print(f\"{i+1:2}. {e['action']:<38} {e['actor_type']:<6} {e['actor_id']}\") for i,e in enumerate(json.load(sys.stdin))]"
```

Expected, in order:

```
 1. CASE_OPENED                            AGENT  uc01-open-case
 2. FORM_LINKED                            AGENT  uc01-open-case
 3. CASE_OPENED_EVENT_PUBLISHED            AGENT  uc01-case-events
 4. FORM_CHANGE_PROPOSED                   HUMAN  officer-1
 5. POLICY_DENIED                          HUMAN  officer-1
 6. FORM_CHANGE_APPROVED                   HUMAN  manager-1
 7. FORM_SUPERSEDED                        AGENT  uc01-form-changes
 8. HRIS_PREPARED                          HUMAN  officer-1
 9. HRIS_WRITE_APPROVED                    HUMAN  manager-1
10. HRIS_WRITE_SUCCEEDED                   AGENT  uc01-hris-write
11. HRIS_WRITE_SUCCEEDED                   AGENT  uc01-hris-write
12. HRIS_WRITE_SKIPPED_ALREADY_WRITTEN     AGENT  uc01-hris-write
```

Things to verify by eye:

- **No `INVARIANT_VIOLATION` entry.** `after_hris_write` runs every check
  (including the salary/email leak scan) after steps 10-11; a clean trail
  means they all passed.
- Entry 5 is the `SELF_APPROVAL` denial. The `ROLE_NOT_ALLOWED` denial from
  A4(a) is **deliberately absent** - it is logged as `ACCESS_DENIED` with no
  `case_id`, so it never appears in a case trail.
- Grep the raw JSON for `118000` and for `asha.varma` - neither appears.

### A10. Change a detail after the record was written

A promotion lands after the employee exists in Zoho. The change must
reach the target system **without** creating a second employee.

```bash
curl -s -X POST $OFF $JSON $API/cases/$CASE/form-changes -d '{
  "change_reason": "promotion",
  "designation": "Senior Software Engineer",
  "approver_id": "manager-1"
}'
curl -s -X POST $MGR $JSON $API/form-changes/<FORM_AP2>/decision -d '{"approve":true}'

curl -s -X POST $OFF $API/cases/$CASE/hris/prepare
# {"outcome":"PREPARED_UPDATE","record_ids":[...],"approval_id":"<HRIS_AP2>"}
```

`PREPARED_UPDATE`, not `PREPARED`: these records carry
`updates_external_ref` pointing at the employees already written.

```bash
curl -s -X POST $MGR $JSON $API/hris-approvals/<HRIS_AP2>/decision -d '{"approve":true}'
curl -s -X POST $MGR $API/cases/$CASE/hris/write
# {"written":[people,payroll],"failed":[],"completed":true,"blocked_code":null}
```

Status returns to `RECORD_CREATED` with both systems `WRITTEN`. The tail
of the audit trail shows each old record being retired as the new one
lands:

```
FORM_SUPERSEDED
HRIS_PREPARED
HRIS_WRITE_APPROVED
HRIS_RECORD_SUPERSEDED
HRIS_WRITE_SUCCEEDED
HRIS_RECORD_SUPERSEDED
HRIS_WRITE_SUCCEEDED
```

**What to check:** the superseded records are `CORRECTED`, not `WRITTEN`,
so there is exactly one live record per system and the
`DUPLICATE_WRITTEN_SYSTEM` invariant stays clean. The external
references are unchanged - the same two employees were updated, not
replaced.

---

## Part B - flagging discrepancies against the offer and other HR records

This is UC-01's discrepancy flagging: the approved onboarding form
compared against the **offer and other HR records**, with every
difference raised for HR to approve.

Identity-document checks (passport and the like) are **not** UC-01's
job - they belong to the document-validation use case. `source` is free
text and gets no special handling here, whatever it is named.

Start from a **fresh case** (Part A's is already written). Re-run A1 and
save the new id as `CASE2`.

### B1. Compare against another HR record

```bash
curl -s -X POST $OFF $JSON $API/cases/$CASE2/compare -d '{
  "snapshots": [{
    "source": "hr_system",
    "values": {"candidate_name": "Asha V. Varma", "joining_date": "2026-04-06"}
  }]
}'
```

```json
{"new_discrepancies":["<D_NAME>","<D_DATE>"],"already_known":0,
 "approvals_created":["<A1>","<A2>"]}
```

**Run it a second time, unchanged** - you should get
`{"new_discrepancies":[],"already_known":2,...}`. That is the HMAC
fingerprint dedupe in `compare_sources.py`.

**Check the form was not touched:** re-read the status. `stage` is
`DISCREPANCIES_OPEN`, `joining_date` is still `2026-04-01`, and
`open_discrepancies` names the two fields and the source. The form is
the source of truth; compare only flags.

### B2. Prove the write is blocked while discrepancies are open

```bash
curl -s -X POST $OFF $API/cases/$CASE2/hris/prepare
# {"outcome":"BLOCKED_OPEN_DISCREPANCIES", "blockers":["<D_NAME>","<D_DATE>"]}
```

### B3. Resolve the discrepancies two different ways

```bash
# keep the form value -> RESOLVED, nothing else happens
curl -s -X POST $OFF $JSON $API/discrepancies/<D_NAME>/decision \
  -d '{"decision":"KEEP_FORM_VALUE"}'

# accept as a change -> ACCEPTED_AS_CHANGE, unlocks a form-change proposal
curl -s -X POST $OFF $JSON $API/discrepancies/<D_DATE>/decision \
  -d '{"decision":"ACCEPT_AS_CHANGE"}'
```

**Check:** the status still shows `joining_date: 2026-04-01` and
`form_version_in_force: 1`. `ACCEPT_AS_CHANGE` records a decision; it
does **not** edit the form. With nothing open, `stage` returns to
`READY_TO_PREPARE`.

To carry the accepted change through, propose a form change as in A3,
passing `"source_discrepancy_id": "<D_DATE>"` to link the two.

The audit trail for this case:

```
 1. CASE_OPENED                        AGENT  uc01-open-case
 2. FORM_LINKED                        AGENT  uc01-open-case
 3. CASE_OPENED_EVENT_PUBLISHED        AGENT  uc01-case-events
 4. DISCREPANCY_FLAGGED                HUMAN  officer-1
 5. DISCREPANCY_FLAGGED                HUMAN  officer-1
 6. COMPARE_COMPLETED                  HUMAN  officer-1
 7. COMPARE_COMPLETED                  HUMAN  officer-1
 8. HRIS_PREPARE_BLOCKED               AGENT  uc01-hris-prepare
 9. DISCREPANCY_DECIDED                HUMAN  officer-1
10. DISCREPANCY_DECIDED                HUMAN  officer-1
```

Entries 6 and 7 are one `COMPARE_COMPLETED` per compare call, including
the second, deduped one.

---

## Part C - redaction and visibility

```bash
# AUDITOR sees the case but not the name
curl -s $AUD $API/cases/$CASE/status | python -m json.tool | grep candidate_name
# "candidate_name": null

# CLIENT_USER may not read an audit trail at all
curl -s $CLI $API/cases/$CASE/audit
# 403 {"error":"access_denied"}

# a case that does not exist, and a case you may not see, look identical
curl -s $OFF $API/cases/does-not-exist/status        # 404 case_not_found
curl -s -H X-User-Id:x -H X-User-Role:HR_MANAGER -H X-Client-Ids:globex \
     $API/cases/$CASE/status                          # 404 case_not_found

# an HR_OFFICER who is not the assigned officer
curl -s -H X-User-Id:officer-2 -H X-User-Role:HR_OFFICER -H X-Client-Ids:acme \
     $API/cases/$CASE/status                          # 404 case_not_found

# no headers at all
curl -s $API/cases/$CASE/status                       # 401 unauthorized

# bad system key
curl -s -X POST -H X-System-Key:wrong $JSON \
     $API/internal/onboarding-requests -d '{}'        # 401 unauthorized
```

**The salary-leak probe.** Send a deliberately invalid body containing a
salary and confirm the 422 does not echo it back:

```bash
curl -s -X POST $SYS $API/internal/onboarding-requests \
  -d '{"client_id":"acme","salary_and_terms":"Base: 999999 SECRET"}'
# {"error":"validation_error","errors":[{"loc":[...],"type":"missing"}, ...]}
```

Only `loc` and `type` - FastAPI's default handler would have included
`input`. That is `error_handlers.py` doing its job.

---

## Part D - the start gate's other exits

Each returns without creating a case. Start from the A1 body and change one
thing at a time.

| Test | Change | Expected |
| --- | --- | --- |
| Duplicate open case | reuse `asha.varma@example.invalid` | `LINKED_EXISTING` + `existing_case_id` |
| Offer not accepted (EC-19) | `"offer_accepted": false` | `HELD`, reason `offer not accepted` |
| Hiring freeze (EC-20) | `"hiring_freeze": true` | `PAUSED` |
| Form not approved | `"form_status": "DRAFT"`, drop `approved_by`/`approved_at` | `HELD`, reason `form not approved` |
| Declared minor (EC-21) | `"declared_variants": {"is_minor": true}` | `CASE_OPENED` + one `approvals_created` |

Verified duplicate response:

```json
{"outcome":"LINKED_EXISTING","existing_case_id":"<CASE>",
 "reasons":["matched an existing open case by email or id number"]}
```

### The variant gate (EC-21)

Using the case from the "declared minor" row above (call it `$C2`, and the
single entry of its `approvals_created` `$VAR_AP`):

```bash
curl -s -X POST $OFF $API/cases/$C2/hris/prepare
# {"outcome":"BLOCKED_PENDING_VARIANT_DECISION","blockers":["<VAR_AP>"]}

curl -s $OFF $API/approvals/pending
# [{"approval_id":"<VAR_AP>","checkpoint":"HC_12_VARIANT",
#   "subject_ref_kind":"is_minor","requested_to":"officer-1",...}]

curl -s -X POST $OFF $JSON $API/variant-approvals/$VAR_AP/decision \
  -d '{"approve":true,"note":"guardian consent on file"}'
# {"status":"APPROVED","decided_by":"officer-1",...}

curl -s -X POST $OFF $API/cases/$C2/hris/prepare
# {"outcome":"PREPARED",...}
```

**Rejecting instead** (`"approve":false`) puts the case in `ON_HOLD`;
status then reads `stage: CASE_INACTIVE` and prepare returns
`BLOCKED_CASE_NOT_ACTIVE`. Worth running on a throwaway case.

---

## Part E - the email intake simulator

Dev-only routes, available because `APP_ENV=dev`.

### D1. Seed an offer whose form is not yet approved

```bash
curl -s -X POST $SYS $API/dev/seed -d '{
  "offer_ref": "OFFER-1001",
  "client_id": "acme",
  "hr_officer_id": "officer-1",
  "candidate_name": "Taylor Synthetic Candidate",
  "candidate_email": "taylor.synthetic.candidate@example.test",
  "designation": "Analyst",
  "joining_date": "2026-05-01",
  "work_location_type": "OFFICE",
  "salary_and_terms": "Base: 90000 SYNTHETIC/yr",
  "form_status": "AWAITING_APPROVAL"
}'
```

### D2. Send the acceptance email

`.eml` files have newlines, so build the JSON with Python rather than
fighting shell quoting:

```bash
python -c "import json; print(json.dumps({'raw_email': open('demo/emails/acceptance_ok.eml').read()}))" > /tmp/e.json
curl -s -X POST $SYS $API/internal/inbound-email -d @/tmp/e.json
```

```json
{"outcome":"WAITING_FOR_FORM","pending_id":"...","reasons":["form not approved"]}
```

The offer is now `ACCEPTED` but the case is parked:

```bash
curl -s $OFF "$API/internal/pending-starts?client_id=acme"
# [{"pending_id":"...","offer_ref":"OFFER-1001","reasons":["form not approved"],"status":"WAITING"}]
```

### D3. Approve the form - the parked case opens

```bash
curl -s -X POST $SYS $API/dev/approve-form \
  -d '{"client_id":"acme","offer_ref":"OFFER-1001","approved_by":"manager-1"}'
# {"outcome":"CASE_OPENED","case_id":"...","employee_no":"EMP-000002"}
```

From here it is the same Part A flow (compare -> prepare -> approve -> write).

### D4. The rejection ladder

```bash
# replay the same accepted email
curl -s -X POST $SYS $API/internal/inbound-email -d @/tmp/e.json
# {"outcome":"DUPLICATE_ACCEPTANCE"}
```

```bash
for f in acceptance_wrong_sender acceptance_no_ref; do
  python -c "import json; print(json.dumps({'raw_email': open('demo/emails/$f.eml').read()}))" > /tmp/e.json
  curl -s -X POST $SYS $API/internal/inbound-email -d @/tmp/e.json; echo
done
# {"outcome":"REJECTED","rejection_code":"SENDER_MISMATCH"}
# {"outcome":"REJECTED","rejection_code":"NO_OFFER_REF"}
```

**Decline detection** needs a fresh offer, because the `ACCEPTED` check runs
before intent classification:

```bash
curl -s -X POST $SYS $API/dev/seed -d '{"offer_ref":"OFFER-1002","client_id":"acme",
 "hr_officer_id":"officer-1","candidate_name":"Jo Synthetic",
 "candidate_email":"jo.synthetic@example.test","designation":"Analyst",
 "joining_date":"2026-05-01","work_location_type":"OFFICE",
 "salary_and_terms":"Base: 90000 SYNTHETIC/yr","form_status":"APPROVED",
 "approved_by":"manager-1","approved_at":"2026-01-10T09:00:00Z"}'

python -c "
import json
raw='From: Jo Synthetic <jo.synthetic@example.test>\nSubject: Re: offer [OFFER-1002] - I must decline\n\nbody ignored\n'
print(json.dumps({'raw_email': raw}))" > /tmp/d.json
curl -s -X POST $SYS $API/internal/inbound-email -d @/tmp/d.json
# {"outcome":"NEEDS_HR_REVIEW","review_code":"POSSIBLE_DECLINE_OR_NEGOTIATION"}
```

Change the subject to drop the word "accept" entirely and you get
`NO_ACCEPTANCE_SIGNAL` instead. Note the body is never read - put
"I accept" in the body only and it still goes to review.

---

## Part F - what you cannot reach over HTTP

Three behaviours have no endpoint. Test them these ways instead.

**The write kill switch.** Read at boot, so it needs a restart (which wipes
state - set it up, then test):

```bash
UC01_HRIS_WRITES_ENABLED=false uv run uvicorn sub_agent.api:app --port 8001
```

Rebuild a case through A1-A7; the write returns
`{"completed":false,"blocked_code":"WRITES_DISABLED"}`.

**Gateway failure / retry.** `StubHrisGateway.fail_next(n)` is in-process
only:

```bash
uv run pytest tests/test_hris_write.py -v
uv run python evaluation/run_evals.py    # scenario gateway_failure_retry
```

**`PAYLOAD_MISMATCH` (the AC-15 field-for-field check).** It requires
corrupting a stored record directly; covered by `tests/test_hooks.py`.

Full regression, both suites at once:

```bash
uv run pytest -q
uv run python evaluation/run_evals.py   # writes evaluation/uc01/results.json
```

---

## Quick reference: what should block what

| You do this | While this is true | You get |
| --- | --- | --- |
| prepare | case ON_HOLD/WITHDRAWN/CLOSED | `BLOCKED_CASE_NOT_ACTIVE` |
| prepare | older version already WRITTEN | `PREPARED_UPDATE` (in-place update) |
| prepare | older version WRITTEN with no `external_ref` | `BLOCKED_UPDATE_NEEDED` |
| prepare | any OPEN discrepancy | `BLOCKED_OPEN_DISCREPANCIES` |
| prepare | pending HC_12 (not `possible_duplicate`) | `BLOCKED_PENDING_VARIANT_DECISION` |
| prepare | a form version AWAITING_APPROVAL | `BLOCKED_FORM_CHANGE_PENDING` |
| prepare | this version's write was rejected | `BLOCKED_HRIS_REJECTED` |
| write | records still PREPARED | 409 conflict |
| decide | your role is not in the policy | 403 `ROLE_NOT_ALLOWED` |
| decide | you raised the request | 403 `SELF_APPROVAL` |
| decide | you are not the assigned officer | 403 `NOT_ASSIGNED_OFFICER` |
| anything | wrong client, or no such case | 404 `case_not_found` |
