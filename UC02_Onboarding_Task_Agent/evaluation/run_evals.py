"""UC-02 evaluation runner.

Each scenario drives the **real** workflow — the real service, the real agent
loop, the real policy — and asserts on observable state. Nothing here prints
a predetermined PASS.

Three outcomes, never conflated:

``PASS``  the assertion ran and held
``FAIL``  the assertion ran and did not hold
``SKIP``  the scenario could not run (missing fixture, absent dependency).
          A skip is reported separately and never counted as a pass; the exit
          code is non-zero if anything failed, and skips are listed loudly.

    python evaluation/run_evals.py
    python evaluation/run_evals.py --verbose

Once the package is installed, the console script ``uc02-evals`` does the
same thing.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Runnable directly (`python evaluation/run_evals.py`), which never loads the
# repository conftest, so put `src/` on the path here.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from onboarding_task_management.contracts import DecisionOutcome, Role
from onboarding_task_management.errors import (
    DataIsolationError,
    EvidenceRequiredError,
    MissingConfigurationError,
    PreconditionError,
)
from onboarding_task_management.sub_agent.adapter import (
    UC02SubAgent,
    WorkflowError,
    WorkflowRequest,
    available_fixtures,
)
from onboarding_task_management.sub_agent.connectors.outlook.base import (
    DeliveryStatus,
    OutlookMessage,
)
from onboarding_task_management.sub_agent.fakes import make_mock_outlook

AS_OF = date(2026, 10, 26)
LATE = date(2026, 12, 31)


class Skip(Exception):
    """Raised by a scenario that cannot run here. Never counted as a pass."""


@dataclass
class Result:
    name: str
    outcome: str          # PASS | FAIL | SKIP
    detail: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, r: Result) -> None:
        self.results.append(r)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def failed(self) -> bool:
        return self.count("FAIL") > 0


# ---------------------------------------------------------------------------
# Scenarios. Each raises AssertionError to fail, or Skip to skip.
# ---------------------------------------------------------------------------


def _fixture(name: str) -> str:
    if name not in available_fixtures():
        raise Skip(f"fixture {name} is not present")
    return name


def scenario_valid_onboarding() -> str:
    sa = UC02SubAgent()
    r = sa.run(WorkflowRequest(fixture=_fixture("01_valid_onboarding.json")))
    assert r.created, "a fresh case should be created"
    assert r.checklist["total"] > 0, "a valid onboarding must produce tasks"
    assert r.agent_run["status"] in {"acted", "observed", "proposed"}, r.agent_run
    return f"{r.checklist['total']} tasks, run={r.agent_run['status']}"


def scenario_offer_not_accepted() -> str:
    sa = UC02SubAgent()
    try:
        sa.run(WorkflowRequest(fixture=_fixture("02_offer_not_accepted.json")))
    except PreconditionError as exc:
        assert "offer" in exc.message.lower(), exc.message
        return f"refused: {exc.message}"
    raise AssertionError("an unaccepted offer must not produce a plan")


def scenario_form_not_approved() -> str:
    sa = UC02SubAgent()
    try:
        sa.run(WorkflowRequest(fixture=_fixture("03_form_not_approved.json")))
    except PreconditionError as exc:
        return f"refused: {exc.message}"
    raise AssertionError("an unapproved form must not produce a plan")


def scenario_missing_client_config() -> str:
    sa = UC02SubAgent()
    try:
        sa.run(WorkflowRequest(fixture=_fixture("04_missing_client_config.json")))
    except MissingConfigurationError as exc:
        return f"refused: {exc.message}"
    raise AssertionError("a client with no configuration must not produce a plan")


def scenario_missing_input() -> str:
    sa = UC02SubAgent()
    for bad, why in (
        (WorkflowRequest(), "neither trigger nor fixture"),
        (WorkflowRequest(trigger={}, fixture="01_valid_onboarding.json"), "both"),
        (WorkflowRequest(fixture="no_such_fixture.json"), "unknown fixture"),
    ):
        try:
            sa.run(bad)
        except WorkflowError:
            continue
        raise AssertionError(f"{why} should have been refused")
    return "3 invalid request shapes refused"


def scenario_dynamic_checklist() -> str:
    """Different employee contexts must produce different checklists."""
    sa = UC02SubAgent()
    local = sa.run(WorkflowRequest(fixture=_fixture("14_local_hire_no_extras.json")))
    reloc = sa.run(WorkflowRequest(fixture=_fixture("07_full_relocation.json")))
    local_keys = {t["template_key"] for t in local.checklist["tasks"]}
    reloc_keys = {t["template_key"] for t in reloc.checklist["tasks"]}
    assert local_keys != reloc_keys, "contexts produced identical checklists"
    assert local_keys < reloc_keys, "local hire should be a strict subset"
    assert "visa_pack_handover" in reloc_keys, "relocation must include visa tasks"
    assert "visa_pack_handover" not in local_keys, "local hire must not"
    return f"local={len(local_keys)} tasks, relocation={len(reloc_keys)} tasks"


def scenario_dependencies_and_due_dates() -> str:
    sa = UC02SubAgent()
    r = sa.run(WorkflowRequest(fixture=_fixture("07_full_relocation.json")))
    tasks = {t["template_key"]: t for t in r.checklist["tasks"]}
    handover = tasks.get("visa_pack_handover")
    assert handover is not None, "expected visa_pack_handover in this plan"
    assert handover["blocked_by"] == ["collect_documents"], handover["blocked_by"]
    assert handover["status"] == "blocked", handover["status"]
    dues = {t["due_date"] for t in r.checklist["tasks"]}
    assert len(dues) > 1, "due dates should vary by configured lead time"
    return f"prerequisite enforced; {len(dues)} distinct due dates"


def scenario_unauthorized_cross_client() -> str:
    sa = UC02SubAgent()
    r = sa.run(WorkflowRequest(fixture=_fixture("01_valid_onboarding.json")))
    from onboarding_task_management.contracts import Actor

    intruder = Actor(actor_id="U-X", role=Role.HR_OPS, client_id="CLIENT-BETA")
    try:
        sa.service.checklist(intruder, r.onboarding_id, AS_OF)
    except DataIsolationError:
        return "cross-client read denied"
    raise AssertionError("another client was able to read the case")


def scenario_evidence_required() -> str:
    sa = UC02SubAgent()
    r = sa.run(WorkflowRequest(fixture=_fixture("08_evidence_required.json")))
    from onboarding_task_management.contracts import Actor

    actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=r.client_id)
    evidence_tasks = [
        t for t in sa.service.get(actor, r.onboarding_id).tasks if t.evidence_required
    ]
    if not evidence_tasks:
        raise Skip("this fixture produced no evidence-bearing task")
    # Walk the dependency chain first. Otherwise the refusal would come from
    # the prerequisite rule and this scenario would not be testing evidence.
    target = evidence_tasks[0]
    record = sa.service.get(actor, r.onboarding_id)
    by_id = {t.task_id: t for t in record.tasks}

    def complete_prereqs(task) -> None:
        for dep_id in task.depends_on:
            dep = by_id.get(dep_id)
            if dep is None or dep.status.value == "complete":
                continue
            complete_prereqs(dep)
            sa.service.complete_task(
                actor, r.onboarding_id, dep.task_id,
                "EVIDENCE-PREREQ" if dep.evidence_required else None,
            )

    complete_prereqs(target)

    try:
        sa.service.complete_task(actor, r.onboarding_id, target.task_id)
    except EvidenceRequiredError as exc:
        # And the same task completes once evidence is supplied.
        done = sa.service.complete_task(
            actor, r.onboarding_id, target.task_id, "EVIDENCE-REF-1"
        )
        assert done.evidence_ref == "EVIDENCE-REF-1", done.evidence_ref
        return (
            f"{target.template_key}: refused without evidence ({exc.code}), "
            "accepted with it"
        )
    raise AssertionError(
        f"{target.template_key} completed without its required evidence"
    )


def scenario_approval_gate() -> str:
    sa = UC02SubAgent()
    r = sa.run(WorkflowRequest(fixture=_fixture("01_valid_onboarding.json")))
    from onboarding_task_management.contracts import Actor
    from onboarding_task_management.errors import ApprovalRequiredError

    actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=r.client_id)
    approver = Actor(actor_id="U-HR-2", role=Role.HR_OPS, client_id=r.client_id)
    req = sa.service.request_change(actor, r.onboarding_id, joining_date=date(2026, 11, 30))
    try:
        sa.service.apply_change(actor, req.request_id)
        raise AssertionError("a gated change applied without approval")
    except ApprovalRequiredError:
        pass
    sa.service.decide_change(approver, req.request_id, DecisionOutcome.APPROVED)
    outcome = sa.service.apply_change(actor, req.request_id)
    assert outcome.revision == 1, outcome.revision
    return "change blocked until a second person approved"


def scenario_mock_outlook() -> str:
    outlook = make_mock_outlook()
    sa = UC02SubAgent(outlook=outlook)
    r = sa.run(
        WorkflowRequest(
            fixture=_fixture("01_valid_onboarding.json"),
            as_of=LATE,
            prepare_reminders=True,
        )
    )
    assert r.reminders["real_email_sent"] is False, "the mock must not send"
    assert r.reminders["draft_delivery_status"] == ["draft_not_sent"], r.reminders
    blocked = [
        x for x in r.reminders["receipts"]
        if x["status"] == DeliveryStatus.BLOCKED_NO_APPROVAL.value
    ]
    assert blocked, "delivery without an approval id should have been blocked"

    # With an approval, delivery is simulated; a retry must not duplicate.
    msg = OutlookMessage(
        idempotency_key="EVAL-1", onboarding_id=r.onboarding_id,
        client_id=r.client_id, recipient_role="hr_ops", subject_en="s",
        approval_id="APR-EVAL",
    )
    first, second = outlook.send(msg), outlook.send(msg)
    assert first.status is DeliveryStatus.SIMULATED_SENT, first
    assert second.status is DeliveryStatus.DUPLICATE_IGNORED, second
    assert outlook.sent_count() == 1, outlook.sent_count()
    return f"{len(blocked)} blocked without approval; retry deduplicated"


def scenario_fake_llm_deterministic() -> str:
    sa = UC02SubAgent()
    a = sa.run(WorkflowRequest(fixture=_fixture("01_valid_onboarding.json")))
    sb = UC02SubAgent()
    b = sb.run(WorkflowRequest(fixture="01_valid_onboarding.json"))
    assert a.advice == b.advice, "identical input produced different advice"
    assert a.advice["source"].startswith("fake_llm:"), a.advice["source"]
    return f"identical advice from two runs; source={a.advice['source']}"


def scenario_fake_llm_cannot_override_rules() -> str:
    """A hallucinating model must be overruled, and must change nothing."""
    sa = UC02SubAgent()
    r = sa.run(
        WorkflowRequest(
            fixture=_fixture("01_valid_onboarding.json"), llm_behaviour="hallucinate"
        )
    )
    assert r.agent_run["degraded"], "a hallucination should degrade the run"
    assert "not in this onboarding" in r.agent_run["degraded_reason"], r.agent_run
    return "invented target rejected by the rules engine"


def scenario_fake_llm_failure_is_honest() -> str:
    sa = UC02SubAgent()
    r = sa.run(
        WorkflowRequest(fixture=_fixture("01_valid_onboarding.json"), llm_behaviour="error")
    )
    assert r.agent_run["degraded"], "a gateway failure must be reported"
    assert r.agent_run["model"] is None, "a failed call must not claim a model"
    return f"degraded: {r.agent_run['degraded_reason'][:48]}"


def scenario_api_validation() -> str:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise Skip("fastapi TestClient is not installed") from None
    from onboarding_task_management.sub_agent.api import app, reset_sub_agent

    reset_sub_agent()
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.post("/v1/onboarding/workflow", json={}).status_code == 400
    assert client.post(
        "/v1/onboarding/workflow", json={"fixture": "nope.json"}
    ).status_code == 400
    assert client.post(
        "/v1/onboarding/workflow", json={"fixture": "02_offer_not_accepted.json"}
    ).status_code == 422
    ok = client.post("/v1/onboarding/workflow", json={"fixture": "01_valid_onboarding.json"})
    assert ok.status_code == 200, ok.status_code
    return "health 200; 400/400/422 on bad input; 200 on valid"


def scenario_api_idempotency() -> str:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise Skip("fastapi TestClient is not installed") from None
    from onboarding_task_management.sub_agent.api import app, reset_sub_agent

    reset_sub_agent()
    client = TestClient(app)
    body = {"fixture": "01_valid_onboarding.json"}
    first = client.post("/v1/onboarding/workflow", json=body).json()
    second = client.post("/v1/onboarding/workflow", json=body).json()
    assert first["created"] is True and second["created"] is False, "not idempotent"
    assert first["onboarding_id"] == second["onboarding_id"], "id changed on repeat"
    assert second["checklist"]["total"] == first["checklist"]["total"]
    return "repeat request reused the same case"


SCENARIOS = [
    ("valid_onboarding", scenario_valid_onboarding),
    ("offer_not_accepted", scenario_offer_not_accepted),
    ("form_not_approved", scenario_form_not_approved),
    ("missing_client_config", scenario_missing_client_config),
    ("missing_or_invalid_input", scenario_missing_input),
    ("dynamic_checklist", scenario_dynamic_checklist),
    ("dependencies_and_due_dates", scenario_dependencies_and_due_dates),
    ("unauthorized_cross_client", scenario_unauthorized_cross_client),
    ("evidence_required", scenario_evidence_required),
    ("approval_gate", scenario_approval_gate),
    ("mock_outlook_reminders", scenario_mock_outlook),
    ("fake_llm_deterministic", scenario_fake_llm_deterministic),
    ("fake_llm_cannot_override_rules", scenario_fake_llm_cannot_override_rules),
    ("fake_llm_failure_is_honest", scenario_fake_llm_failure_is_honest),
    ("api_validation_and_errors", scenario_api_validation),
    ("api_repeated_request_idempotency", scenario_api_idempotency),
]


# The extended synthetic-profile scenarios (evaluation/extended_scenarios.py) are part of the suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extended_scenarios import EXTENDED_SCENARIOS  # noqa: E402

SCENARIOS = SCENARIOS + EXTENDED_SCENARIOS


def run(verbose: bool = False) -> Report:
    report = Report()
    for name, fn in SCENARIOS:
        try:
            detail = fn()
            report.add(Result(name, "PASS", detail))
        except Skip as exc:
            report.add(Result(name, "SKIP", str(exc)))
        except AssertionError as exc:
            report.add(Result(name, "FAIL", str(exc) or "assertion failed"))
        except Exception as exc:  # noqa: BLE001 - any error IS a failure here
            detail = f"{type(exc).__name__}: {exc}"
            if verbose:
                detail += "\n" + traceback.format_exc()
            report.add(Result(name, "FAIL", detail))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UC-02 evaluation suite")
    parser.add_argument("--verbose", action="store_true", help="include tracebacks")
    args = parser.parse_args(argv)

    print("=" * 78)
    print("  UC-02 EVALUATION  —  fake LLM, mock Outlook, no database")
    print("=" * 78)
    report = run(args.verbose)
    for r in report.results:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[r.outcome]
        print(f"  [{mark}] {r.name:36} {r.detail}")

    passed, failed, skipped = (report.count(x) for x in ("PASS", "FAIL", "SKIP"))
    print("-" * 78)
    print(f"  {passed} passed, {failed} failed, {skipped} skipped "
          f"(of {len(report.results)} scenarios)")
    if skipped:
        print("  SKIPPED scenarios did NOT pass:")
        for r in report.results:
            if r.outcome == "SKIP":
                print(f"    - {r.name}: {r.detail}")
    print("=" * 78)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
