# UC-01 audit hash chain — persistence design

Resolves review F1/F5/F6. **Design only — no SQL applied, nothing
database-verified.**

## 1. The constraint the code already imposes

`verify_chain` ([audit_integrity.py:196](../../src/sub_agent/audit_integrity.py#L196))
asserts `entry.sequence == index` over the entries it is given, and
`verify_audit_integrity` ([case_status.py:234](../../src/sub_agent/case_status.py#L234))
passes `deps.audit_log.entries()` — the **whole log**, AUDITOR-only,
"because the whole log spans every client". `InMemoryAuditLog.append`
uses `sequence = len(self._entries)`.

So the chain is **global, contiguous, zero-based**. Three consequences
that are easy to miss:

1. **A gap is a permanent failure.** Not a warning — every subsequent
   entry reports `SEQUENCE_BREAK`. An allocation that is consumed and
   then not committed corrupts verification forever.
2. **`prev_hash` is computed in Python**, not SQL: `entry_digest` hashes
   a canonical JSON dump of the Pydantic model. The database cannot
   derive it, so the sequence and the tail hash must be *read into the
   application* before the row can be built.
3. **Serialization is inherent.** Appends cannot proceed in parallel.

## 2. Why this cannot work over PostgREST

The append is necessarily three steps:

```
read tail (max sequence, its entry_hash)  ->  compute digest in Python  ->  insert
```

Over PostgREST each step is its own transaction
([client.py](../../src/sub_agent/adapters/supabase/client.py) states this).
Two appenders interleave and compute the same `sequence`; the unique
index rejects one, and the rejected one must retry — but a crash
between read and insert leaves a **consumed-but-absent** sequence, i.e.
a permanent gap.

**Therefore D4 is gated on D6.** `UnsupportedAuditLog` must keep
refusing until a transactional connection exists. This is the single
reason UC-01 cannot persist anything today — every flow writes audit
first.

## 3. Proposed design (requires D6 option A, psycopg)

```sql
BEGIN;
  SELECT pg_advisory_xact_lock(hashtext('uc01_audit_chain'));
  SELECT sequence, entry_hash
    FROM uc01_audit_log
   ORDER BY sequence DESC
   LIMIT 1;                      -- NULL/none => genesis
  -- application computes chain_entry(entry, seq+1, prev_hash)
  INSERT INTO uc01_audit_log (...) VALUES (...);
COMMIT;
```

**Why an advisory lock rather than retry-on-conflict.** The transaction
lock is released at commit *or rollback*, so a failed append leaves no
row **and** no consumed number — gaplessness falls out of the design
instead of depending on correct retry logic. It also removes the
thundering-herd retries a hot unique index would produce. The unique
index on `sequence` stays as a backstop, not as the mechanism.

**Why not a counter table.** A counter is a second source of truth that
can drift from `max(sequence)`. `max(sequence)` under the lock cannot.

**Cost, stated plainly:** one global lock serializes every UC-01 audit
append. `open_onboarding_case` writes ~8 audit rows, so this lock is the
throughput ceiling for case creation. If that proves unacceptable, the
alternative is **changing `verify_chain` to support per-client chains**
(decision D4 option C) — a change to an integrity control, needing its
own review and test updates. It is not something to do silently for
performance.

## 4. Pre-case entries (F6)

`START_GATE_EVALUATED` and `DUPLICATE_CHECK_EVALUATED` are written
before any case exists; the model allows `case_id IS NULL` provided
`request_id` is set. A **global** chain handles these naturally — they
occupy sequence positions like any other entry. This is a further
argument against the per-case design: a per-case chain has nowhere to
put them.

The RLS read policy must still admit them, which the draft does
(`case_id IS NULL OR case_id IN (...)`).

## 5. Verification must see the complete log

`verify_chain` on a filtered subset reports violations on an intact log.
Two things follow:

- **Table choice.** On a shared `audit_events`, "the complete log" is
  UC-01's subset, which needs a marker column and a filter everyone
  remembers to apply. On a dedicated `uc01_audit_log` it is simply
  "every row". **Recommend the dedicated table** (decision D4 option B).
- **RLS conflict (open).** The draft's `audit_log_select` policy scopes
  rows by tenant, so `uc01_app` can never see the complete log — and
  verification would fail against a perfectly intact chain. The AUDITOR
  path needs either a role exempt from that policy or a
  `SECURITY DEFINER` verification function. **Not resolved here; it is a
  security decision.**

## 6. Tail truncation (unresolved by design)

`verify_chain`'s own docstring says it: dropping entries off the tail
leaves a shorter but internally valid chain. No in-database control
fixes this — a trigger cannot stop the table owner, and a high-water
mark stored in the same database is deleted by the same actor.

**Proposal:** after each commit (or on a short timer), publish
`(max_sequence, entry_hash)` to an **external** append-only sink the
application cannot rewrite — object storage with versioning/object-lock,
or a separate account's log stream. Verification then compares the
in-database head against the external high-water mark; a lower
in-database sequence proves truncation.

This is an infrastructure decision with a cost, and it is the only thing
that makes "append-only" a real claim rather than a detectable-if-
you're-lucky one. **Owner: security.**

## 7. What is still not decided

| Question | Owner |
|---|---|
| Shared `audit_events` + marker, vs dedicated `uc01_audit_log` | DBA + security |
| How AUDITOR verification sees the complete log under RLS | Security |
| Whether to accept the global-lock throughput ceiling, or change `verify_chain` | Lead |
| External high-water mark sink, or accept undetectable truncation | Security |
