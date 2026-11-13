"""UC05_BUDDY_SOURCE=supabase: UC05's buddy candidates from the shared buddy_profiles table.

UC05's local buddy pool (its mock mode) holds only synthetic test buddies, so a real client such as CLIENT-ALPHA has
none and buddy assignment stops with NO_BUDDY_CANDIDATES. With this setting the UC05 worker reads the client's buddies,
read-only, from Supabase buddy_profiles -- the HR-maintained directory UC05's own Supabase mode reads -- with UC05's
rule: active profiles of this client, department as team, none beyond max_active_buddies (counting confirmed
assignments in Supabase and the buddies this UC05 assigned locally). Never a synthetic buddy for a real client.

The pool runs in UC05's own interpreter against UC05's fake PostgREST (tests/fake_postgrest.py): no network, no live
database."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap

import pytest
from _helpers import UC_ROOTS, require_uc, venv_python
from _lifecycle import start_uc05_worker
from _uc05_fakes import FakeUc05Worker, session, snapshot
from test_uc05_adapter import _after, _deps

from hr_onboarding_common.orchestration.adapters.uc05_orientation import call_uc05_buddy_assignment
from hr_onboarding_common.orchestration.state import Stage
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError

COMMON_SRC = UC_ROOTS["uc05"].parent / "HR-Onboarding-Common" / "src"

POOL_SCRIPT = textwrap.dedent('''
    import json
    from types import SimpleNamespace
    from pydantic import SecretStr
    from tests.fake_postgrest import FakePostgrest
    from sub_agent.db.postgrest import PostgrestClient
    from hr_onboarding_common.orchestration.workers.uc_worker_runtime import _SupabaseBuddyPool

    db = FakePostgrest()
    def profile(emp, *, client="CLIENT-ALPHA", dept="Engineering", cap=2, active=True):
        return {"buddy_id": f"b-{emp}-{client}", "client_id": client, "employee_id": emp, "full_name": f"Name {emp}",
                "department": dept, "location": "Chennai", "max_active_buddies": cap, "is_active": active,
                "profile_payload": {}}
    db.seed("buddy_profiles", profile("EMP-B1"), profile("EMP-B2", active=False), profile("EMP-B3", cap=1),
            profile("EMP-B4", cap=1), profile("EMP-B5", dept="Finance"), profile("EMP001", client="CLIENT_INDIA"),
            # Template text and dummy values are never eligible (the row once seeded unedited in buddy_profiles):
            {**profile("x"), "buddy_id": "b-template", "employee_id": "<colleague employee number>",
             "full_name": "<colleague full name>"},
            {**profile("EMP-B9"), "buddy_id": "b-dummy", "full_name": "Dummy Buddy"})
    db.seed("buddy_assignments", {"assignment_id": "a1", "case_id": "c1", "client_id": "CLIENT-ALPHA",
                                  "buddy_employee_id": "EMP-B3", "status": "CONFIRMED", "match_score": 1,
                                  "matching_criteria": {}, "assigned_at": None, "confirmed_at": None})

    class Local:  # UC05's local repository: one joiner already has EMP-B4 as buddy
        def list_onboarding_records(self, *, client_id=None):
            return [SimpleNamespace(buddy_id="EMP-B4"), SimpleNamespace(buddy_id=None)]

    client = PostgrestClient("https://fake.supabase.co", SecretStr("service-role-test"), transport=db.transport)
    pool = _SupabaseBuddyPool(Local(), client=client)
    alpha = pool.list_buddy_candidates("CLIENT-ALPHA")
    print(json.dumps({
        "alpha": [[c.employee_id, c.team, c.client_id] for c in alpha],
        "ignored": sorted(pool.ignored),
        "template_profile": pool.profile("CLIENT-ALPHA", "<colleague employee number>"),
        "beta": [c.employee_id for c in pool.list_buddy_candidates("CLIENT-BETA")],
        "methods": sorted({m for m, _, _ in db.requests}),
        "untenanted": [t for m, t, p in db.requests
                       if m == "GET" and dict(p).get("limit") != "0" and ("client_id", "eq.CLIENT-ALPHA") not in p
                       and ("client_id", "eq.CLIENT-BETA") not in p],
    }))
''')


def test_the_pool_reads_this_clients_active_buddies_with_capacity_and_never_writes():
    require_uc("uc05")
    root = UC_ROOTS["uc05"]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root), str(COMMON_SRC)])}
    out = subprocess.run([str(venv_python(root)), "-c", POOL_SCRIPT], cwd=root, env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    # B2 inactive; B3 full (confirmed in Supabase); B4 full (assigned locally); EMP001 belongs to another client.
    assert got["alpha"] == [["EMP-B1", "Engineering", "CLIENT-ALPHA"], ["EMP-B5", "Finance", "CLIENT-ALPHA"]]
    assert got["beta"] == []  # nothing configured: UC05 reports NO_BUDDY_CANDIDATES, never a synthetic buddy
    assert got["ignored"] == ["b-dummy", "b-template"]  # placeholder/dummy profiles skipped, never offered
    assert got["template_profile"]["valid"] is False  # and never presented as a valid buddy
    assert got["methods"] == ["GET"]  # read-only
    assert got["untenanted"] == []  # every row read is filtered on the client


@pytest.mark.parametrize("env", [
    {"UC05_BUDDY_SOURCE": "everyone"},
    {"UC05_BUDDY_SOURCE": "supabase", "UC05_DATABASE_MODE": "supabase"},
    {"UC05_BUDDY_SOURCE": "supabase", "SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": ""},
])
def test_a_misconfigured_buddy_source_stops_the_worker_instead_of_falling_back(env):
    with pytest.raises(WorkerCrashedError):
        worker = start_uc05_worker(**env)
        worker.stop()


@pytest.mark.parametrize("source, says", [
    ("supabase", "Supabase buddy_profiles table: add an active profile for this client"),
    ("local", "holds only its synthetic test buddies; set UC05_BUDDY_SOURCE=supabase"),
])
def test_no_candidates_says_where_uc05_looked_and_what_to_add(source, says):
    worker = FakeUc05Worker(case_snapshot=snapshot(sessions=[session("induction", "confirmed")]),
                            propose_buddy={"outcome": "no_candidates", "buddy_source": source,
                                           "detail": "no buddy candidates are configured for CLIENT-TEST-A"})
    result = call_uc05_buddy_assignment(_after(Stage.ORIENTATION_SCHEDULING), _deps(worker))
    (b,) = [b for b in result["blockers"] if b["code"] == "NO_BUDDY_CANDIDATES"]
    assert says in b["reason"] and b["required_data"] == "buddy_profiles" and b["resolution"] == "external"
