"""Restart recovery with REAL separate OS processes and a REAL SQLite file.

Each phase is a fresh ``python`` interpreter running ``demo/uc02_persistent_workflow.py``:
the writer exits completely before the verifier starts, so nothing can survive except what
was written to the file. (Recreating a service object in the same process would not count.)

Synthetic data only; the UC-03 reporter is a TEST-ONLY identity, not a real integration.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "demo" / "uc02_persistent_workflow.py"


def run_phase(db: Path, phase: str) -> tuple[int, dict, str]:
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--db", str(db), "--phase", phase],
        capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir(),
    )
    last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    return proc.returncode, json.loads(last), proc.stderr


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="uc02-restart-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = self.dir / "restart.sqlite3"

    def test_a_new_process_recovers_everything_the_first_process_wrote(self) -> None:
        code, written, err = run_phase(self.db, "write")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.db.exists())
        self.assertGreater(self.db.stat().st_size, 0)
        self.assertEqual(written["revision"], 1)
        self.assertTrue(written["failure_accepted"])

        code, verified, err = run_phase(self.db, "verify")  # a brand-new interpreter
        self.assertEqual(code, 0, f"{verified}\n{err}")
        failed = [name for name, ok in verified["checks"].items() if not ok]
        self.assertEqual(failed, [], failed)
        # The checks are the mandatory list: ids, tasks/owners/dependencies, completed and
        # failed states, timestamps and audit history, config version, replay idempotency,
        # tenant isolation, and the service continuing after restart.
        for name in ("case_found", "case_and_correlation_ids", "config_version", "tasks_and_dependencies",
                     "owner_assignment", "completed_with_evidence", "failure_state", "replan_survived",
                     "plan_revision_row", "approval_history", "audit_history", "audit_timestamps_and_ids",
                     "replay_is_duplicate", "replay_adds_no_audit_event", "tenant_isolation",
                     "service_continues_after_restart"):
            self.assertIn(name, verified["checks"])

    def test_the_audit_history_is_the_same_before_and_after_the_restart(self) -> None:
        import sqlite3

        code, written, err = run_phase(self.db, "write")
        self.assertEqual(code, 0, err)
        conn = sqlite3.connect(self.db)
        before = {row[0] for row in conn.execute("SELECT event_id FROM audit_event")}
        conn.close()
        self.assertEqual(len(before), written["audit_events"])
        code, verified, err = run_phase(self.db, "verify")
        self.assertEqual(code, 0, err)
        conn = sqlite3.connect(self.db)
        after = {row[0] for row in conn.execute("SELECT event_id FROM audit_event")}
        new = sorted(
            (json.loads(doc)["action"], json.loads(doc)["outcome"])
            for event_id, doc in conn.execute("SELECT event_id, doc FROM audit_event")
            if event_id not in before
        )
        conn.close()
        self.assertTrue(before <= after)  # nothing rewritten or lost; only appended to
        # Only what the verifier legitimately did: a read, a refused cross-tenant read, one completion.
        # The replayed status update added NOTHING.
        self.assertEqual(new, [("onboarding.access", "denied_isolation"),
                               ("onboarding.read", "read"), ("task.complete", "complete")])

    def test_a_missing_database_is_reported_not_silently_recreated_with_data(self) -> None:
        code, result, _ = run_phase(self.db, "verify")  # nothing was ever written
        self.assertNotEqual(code, 0)
        self.assertFalse(result.get("ok", False))


if __name__ == "__main__":
    unittest.main()
