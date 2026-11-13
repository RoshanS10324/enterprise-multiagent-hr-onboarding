# `supabase/` — local test stack only

**Do not run `supabase db push` from this directory while linked to the
shared `Onboarding_Agent` project.**

`migrations/` recreates the UC-01-relevant subset of the live schema so
`supabase start` can stand up an equivalent database locally. Those
tables **already exist** in the shared project; pushing them there would
fail at best and conflict with other use cases at worst.

Proposed changes to the shared project live in
`docs/schema/proposed/d4_d5_additive.sql`, deliberately **outside** this
directory so the CLI cannot apply unapproved DDL. They require sign-off
on D4/D5 (`docs/uc01-supabase-decisions.md`) and should be applied by
the DBA through the normal change process.

Setup and usage: `docs/local-test-env.md`.

---

Note: this directory is also why `import supabase` resolves to an empty
namespace package when running from the UC-01 root. UC-01 deliberately
does **not** depend on `supabase-py` (its PostgREST layer is one
implicit transaction per call), so this is currently harmless — but it
would shadow the real library if anyone ever installed it.
