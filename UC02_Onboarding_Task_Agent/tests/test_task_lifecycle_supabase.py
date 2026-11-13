"""Task lifecycle on the Supabase-only runtime over a MOCKED Supabase transport: parallel progress, dependency unblocking,
completion, due dates, overdue detection, the monitoring sweep, replay safety, mock access provisioning and restart recovery.
Claims are checked against what the stand-in database holds, not against what the stage printed. A mocked transport proves
the logic and the write shapes; the live database is verified separately (see docs/UC02_RUNTIME_AND_ACTIVATION.md)."""

from __future__ import annotations

from datetime import date, timedelta

from onboarding_task_management.persistence import PersistenceError
from onboarding_task_management.sub_agent import supabase_demo as demo
from tests.test_supabase_only_workflow import ENV, SPEC, NoMemoryTestCase, prepared


def stored_status(rest) -> dict[str, str]:
    rows = rest.select("onboarding_tasks", {"case_id": SPEC.case_id}, columns="task_id,status,status_summary")
    return {r["task_id"]: r["status"] for r in rows if "uc02" in (r.get("status_summary") or {})}


def short(task_id: str) -> str:
    return task_id.split("#", 1)[1]


class TaskLifecycleTests(NoMemoryTestCase):
    def test_an_unapproved_plan_cannot_progress_tasks(self) -> None:
        db, rest = prepared(approve=False)
        with self.assertRaises(PersistenceError):
            demo.tasks(ENV, rest=rest, spec=SPEC)

    def test_only_unblocked_tasks_run_first_and_they_run_together(self) -> None:
        db, rest = prepared()
        before = stored_status(rest)
        self.assertNotIn("complete", before.values())
        report = demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True)
        first = {t["task"] for t in report["waves"][0]["parallel_tasks"]}
        self.assertEqual(first, set(report["before"]["ready"]))
        after = stored_status(rest)
        self.assertEqual({t for t, s in after.items() if s == "complete"}, first)  # read back from the database
        for blocked in report["before"]["blocked"]:
            self.assertNotEqual(after[blocked], "complete")  # a dependent never started before its prerequisite
        self.assertTrue(report["newly_unblocked"])  # finishing the first wave made later work ready

    def test_every_wave_contains_only_tasks_whose_prerequisites_finished_earlier_and_siblings_run_in_parallel(self) -> None:
        db, rest = prepared()
        report = demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, waves=10)
        prerequisites = {t: set(deps) for t, deps in report["before"]["blocked"].items()}
        finished = set(report["before"]["complete"])
        for wave in report["waves"]:
            ids = {t["task"] for t in wave["parallel_tasks"]}
            for task_id in ids:
                self.assertLessEqual(prerequisites.get(task_id, set()), finished, task_id)
            finished |= ids
        self.assertGreaterEqual(len(report["waves"]), 3)  # the synthetic plan has real dependency depth
        self.assertTrue(any(len(w["parallel_tasks"]) > 1 for w in report["waves"]))  # and independent tasks run in parallel
        self.assertEqual(set(stored_status(rest).values()), {"complete"})
        self.assertEqual(report["after"]["blocked"], {})

    def test_access_and_other_external_tasks_complete_through_labelled_mock_status_updates(self) -> None:
        db, rest = prepared()
        report = demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, waves=10)
        by_task = {short(t["task"]): t["via"] for w in report["waves"] for t in w["parallel_tasks"]}
        self.assertEqual(by_task["system_access"], "MOCK uc04_equipment_access status update")  # mock access provisioning
        self.assertEqual(by_task["laptop_request"], "MOCK uc04_equipment_access status update")
        self.assertTrue(by_task["orientation_slot"].startswith("MOCK uc05"))
        self.assertTrue(by_task["hr_record_create"].startswith("owner role"))
        self.assertTrue(all(t["accepted"] for w in report["waves"] for t in w["parallel_tasks"]))

    def test_running_the_stage_after_everything_is_complete_does_nothing(self) -> None:
        db, rest = prepared()
        demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, waves=10)
        stored = stored_status(rest)
        again = demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, waves=10)
        self.assertEqual(again["waves"], [])  # nothing to progress, nothing re-completed
        self.assertEqual(stored_status(rest), stored)
        self.assertEqual(again["sweep"]["newly_recorded_escalations"], 0)

    def test_due_dates_are_stored_and_overdue_is_derived_from_the_clock(self) -> None:
        db, rest = prepared()
        report = demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, as_of=date(2026, 11, 4), waves=1)
        due = {t: date.fromisoformat(d) for t, d in report["due_dates"].items()}
        self.assertEqual(len(due), len(stored_status(rest)))
        self.assertEqual(report["overdue_at_as_of"], [])  # nothing is due before the plan was made
        far = max(due.values()) + timedelta(days=3)
        late = demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, as_of=far, waves=0)
        open_ids = {t for t, s in stored_status(rest).items() if s != "complete"}
        self.assertTrue(open_ids)
        self.assertEqual(set(late["overdue_at_as_of"]), open_ids)  # every unfinished task is overdue on that date
        self.assertTrue(late["sweep"]["escalations"] or late["sweep"]["reminder_drafts"])
        self.assertIn("nothing was sent", late["sweep"]["delivery"])

    def test_the_sweep_records_each_escalation_once(self) -> None:
        db, rest = prepared()
        far = date(2027, 6, 1)
        first = demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, as_of=far, waves=0)
        again = demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, as_of=far, waves=0)
        self.assertGreater(first["sweep"]["newly_recorded_escalations"] + first["sweep"]["newly_recorded_reminders"], 0)
        self.assertEqual((again["sweep"]["newly_recorded_escalations"], again["sweep"]["newly_recorded_reminders"]), (0, 0))

    def test_progress_survives_a_restart_and_a_replayed_event_neither_undoes_nor_duplicates_it(self) -> None:
        db, rest = prepared()
        demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, waves=2)
        before = stored_status(rest)
        self.assertIn("complete", before.values())
        report = demo.recover(ENV, rest=rest, spec=SPEC, mock_downstream=True)
        self.assertEqual(report["event_replay"], "duplicate")
        self.assertFalse(report["duplicate_tasks"])
        self.assertEqual(stored_status(rest), before)
        status = demo.verify(ENV, rest=rest, spec=SPEC)
        self.assertEqual(sum(status["task_status_counts"].values()), status["tasks_stored"])
        self.assertEqual(status["task_status_counts"].get("complete"), sum(1 for s in before.values() if s == "complete"))

    def test_the_stage_writes_nothing_to_uc01_tables(self) -> None:
        db, rest = prepared()
        owned = ("onboarding_cases", "offers", "onboarding_forms", "offer_forms")
        before = {t: [dict(r) for r in rest.select(t, {})] for t in owned}
        demo.tasks(ENV, rest=rest, spec=SPEC, mock_downstream=True, waves=3)
        self.assertEqual(before, {t: [dict(r) for r in rest.select(t, {})] for t in owned})
