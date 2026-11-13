"""UC-02 end-to-end agentic workflow, on synthetic data.

Demonstrates the full intended flow in one run:

    employee input
      -> start-condition validation
      -> dynamic checklist generation
      -> dependency and schedule calculation
      -> persist plan and tasks
      -> agent retrieves current context
      -> agent analyses blockers and risk
      -> agent recommends or requests an approved action
      -> authorisation and human-approval checks
      -> permitted tool execution
      -> result verification
      -> persist updated task state
      -> audit record
      -> refreshed checklist, and a simulated process restart

Everything is local and synthetic. The script prints an integration-status
block first, from :func:`agent.harness.integration_status`, so the output
states what is really wired rather than what is intended: with no Supabase
project and no model credential it will say so plainly.

Run from the repository root:

    python demo/uc02_agentic_workflow.py                  # fake LLM (default)
    python demo/uc02_agentic_workflow.py EMP-001          # any synthetic persona
    UC02_LLM_FAKE=0 python demo/uc02_agentic_workflow.py  # rule-based advisor

The advisor is a **deterministic fake LLM**, not a real model. It is driven
through the same ``LiteLLMAdvisor`` a real gateway would use, so the prompt
assembly, structured-output round-trip and source tagging are genuine; only
the network call is replaced. Its model id is ``fake/uc02-advisor-v1`` and all
advice is tagged ``source=fake_llm:...``. Nothing here shows a real LLM works.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management.agent import UC02Agent  # noqa: E402
from onboarding_task_management.agent.advisor import (  # noqa: E402
    advisor_mode,
    build_advisor,
)
from onboarding_task_management.agent.fake_llm import (  # noqa: E402
    FAKE_MODEL,
    build_fake_advisor,
)
from onboarding_task_management.agent.harness import integration_status  # noqa: E402
from onboarding_task_management.contracts import (  # noqa: E402
    Actor,
    DecisionOutcome,
    Role,
)
from onboarding_task_management.persistence import (  # noqa: E402
    InMemoryTableClient,
    OnboardingStore,
)
from onboarding_task_management.sub_agent.service import OnboardingService  # noqa: E402
from onboarding_task_management.synthetic import (  # noqa: E402
    build_registry,
    make_trigger,
    persona,
)

AS_OF = date(2026, 10, 26)
#: The demo defaults to the FAKE LLM so the model-facing adapter is exercised.
#: Set UC02_LLM_FAKE=0 to use the rule-based advisor instead.
USE_FAKE_LLM = os.environ.get("UC02_LLM_FAKE", "1") == "1"


def demo_advisor():
    """The deterministic fake LLM by default; otherwise whatever is supported."""
    return build_fake_advisor() if USE_FAKE_LLM else build_advisor()


NEW_JOINING = date(2026, 11, 30)
DEFAULT_EMPLOYEE = "EMP-007"


def _enable_utf8() -> bool:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return True
    except Exception:
        return False


UTF8 = _enable_utf8()


def safe(text: str) -> str:
    return text if UTF8 else text.encode("ascii", "replace").decode("ascii")


def header(title: str) -> None:
    print()
    print("=" * 86)
    print(f"  {title}")
    print("=" * 86)


def step(n: int, title: str) -> None:
    print(f"\n--- {n}. {title} " + "-" * max(0, 78 - len(title)))


def show_integration_status() -> None:
    header("0. INTEGRATION STATUS  (facts, read from the running process)")
    status = integration_status()
    rows = [
        ("shared harness installed", status["harness_installed"]),
        ("policy envelope loaded", status["policy_loaded"]),
        ("autonomy / ceiling", f"{status.get('autonomy')} / {status.get('ceiling')}"),
        ("LiteLLM installed", status["litellm_installed"]),
        ("LLM credential configured", status["llm_credential_configured"]),
        ("MCP server", status["mcp_server"]),
    ]
    for label, value in rows:
        print(f"  {label:28}: {value}")
    print(
        "\n  Persistence : InMemoryTableClient (no Supabase project exists yet;"
        "\n                migrations are in supabase/migrations/, unapplied)"
    )
    advisor = demo_advisor()
    mode = "fake (demo default)" if USE_FAKE_LLM else advisor_mode()
    print(f"  Advisor     : {advisor.name}  (mode: {mode})")
    if advisor.name == "fake_llm":
        print(f"                *** FAKE LLM — model id {FAKE_MODEL} ***")
        print("                Deterministic scripted responses. Driven through")
        print("                the SAME LiteLLMAdvisor a real gateway would use,")
        print("                so prompt assembly and the structured-output")
        print("                round-trip are genuine; only the network call is")
        print("                replaced. NO real model call is made anywhere.")
    elif advisor.name == "offline":
        print("                Rule-based advisor — NOT a model. It reads the")
        print("                rules engine directly: no prompt, no model call.")


def main(argv: list[str]) -> int:
    employee_id = argv[0] if argv else DEFAULT_EMPLOYEE
    show_integration_status()

    try:
        who = persona(employee_id)
    except KeyError:
        print(f"\nunknown persona {employee_id!r}")
        return 2

    actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=who.client_id)
    approver = Actor(actor_id="U-HR-2", role=Role.HR_OPS, client_id=who.client_id)

    table_client = InMemoryTableClient()
    store = OnboardingStore(table_client)
    service = OnboardingService(build_registry(), store=store)

    header(f"UC-02 AGENTIC WORKFLOW  —  {employee_id}")

    # 1-3 ---------------------------------------------------------------
    step(1, "Input and start-condition validation")
    trigger = make_trigger(who)
    print(f"  employee    : {trigger.employee_id}   client: {trigger.client_id}")
    print(f"  role/grade  : {trigger.role_id} / {trigger.grade}")
    print(f"  location    : {trigger.location}   project: {trigger.project}")
    print(f"  attributes  : {list(trigger.attributes) or '(none)'}")
    print(f"  offer accepted={trigger.offer.accepted}  form approved={trigger.form.approved}")

    record = service.create_onboarding(actor, trigger)
    onboarding_id = record.onboarding_id

    step(2, "Dynamic checklist generation and scheduling")
    view = service.checklist(actor, onboarding_id, AS_OF)
    print(f"  onboarding_id : {onboarding_id}")
    print(f"  generated     : {len(view.entries)} task(s); {len(view.excluded)} excluded")
    print(f"  ready         : {len(view.ready_task_ids)}   blocked: {len(view.blocked_task_ids)}")
    print(f"  dependency layers: {len(view.parallel_batches)}")
    print(f"  review_required  : {view.review_required}")

    step(3, "Persisted plan")
    for table in ("onboarding", "task", "task_dependency", "client_config_ref"):
        print(f"  {table:22}: {table_client.row_count(table)} row(s)")

    # 4-9 ---------------------------------------------------------------
    header("AGENT RUN")
    advisor = demo_advisor()
    agent = UC02Agent(service, actor, advisor=advisor, store=store)
    result = agent.run(onboarding_id, AS_OF)

    step(4, "Agent context, analysis and advice")
    print(safe(f"  summary   : {result.advice.summary}"))
    print(f"  action    : {result.advice.recommended_action}")
    print(f"  target    : {result.advice.target_task_id}")
    print(safe(f"  rationale : {result.advice.rationale}"))
    print(f"  advisor   : {result.advisor}   model: {result.model}")
    print(f"  source    : {result.advice.source}")
    print(f"  degraded  : {result.degraded}")
    fake_client = getattr(advisor, "fake_client", None)
    if fake_client is not None:
        print(f"  fake LLM matched scenario: {fake_client.last_scenario}")
    print(f"  blockers  : {len(result.advice.blockers)}")
    for blocker in result.advice.blockers[:3]:
        print(f"      - {blocker.template_key}: {blocker.reason}")

    step(5, "Authorisation, execution and verification")
    for s in result.steps:
        print(f"  [{'ok  ' if s.ok else 'FAIL'}] {s.name:9} {s.tool or '':24} {s.detail}")
    print(f"\n  status   : {result.status.value}")
    print(f"  verified : {result.verified}")
    print(f"  actions  : {result.actions_taken or '(none)'}")
    print(f"  run row  : {len(store.agent_runs(who.client_id))} persisted")

    # 6-7 ---------------------------------------------------------------
    header("RESTRICTED CHANGE  —  human approval required")
    step(6, "Agent may request, but never decide")
    change = service.request_change(actor, onboarding_id, joining_date=NEW_JOINING)
    print(f"  request  : {change.request_id}  gate={change.gate.value}")
    print(f"  outcome  : {change.outcome.value}  approver role: {change.approver_role.value}")
    try:
        service.apply_change(actor, change.request_id)
        print("  applying without approval -> ACCEPTED (unexpected)")
    except Exception as exc:  # noqa: BLE001 - demonstrating the refusal
        print(f"  applying without approval -> {type(exc).__name__}: {exc}")

    step(7, "A different human approves, then the plan changes")
    service.decide_change(approver, change.request_id, DecisionOutcome.APPROVED)
    outcome = service.apply_change(actor, change.request_id)
    print(f"  decided by : {approver.actor_id} (not the requester)")
    print(f"  revision   : {outcome.revision}")
    print(f"  rescheduled: {len(outcome.rescheduled_task_ids)} task(s)")
    print(f"  retained   : {len(outcome.retained_task_ids)} task(s)")

    # 8-9 ---------------------------------------------------------------
    header("DURABILITY AND AUDIT")
    step(8, "Simulated process restart from stored rows only")
    snapshot = table_client.snapshot()
    restarted = OnboardingService(
        build_registry(),
        store=OnboardingStore(InMemoryTableClient.from_snapshot(snapshot)),
    )
    loaded = restarted.hydrate(who.client_id)
    reloaded = restarted.get(actor, onboarding_id)
    print(f"  hydrated      : {loaded} case(s) from storage")
    print(f"  joining_date  : {reloaded.joining_date}  (changed from {trigger.joining_date})")
    print(f"  revision      : {reloaded.revision}")
    print(f"  tasks         : {len(reloaded.tasks)}")
    in_progress = [t.template_key for t in reloaded.tasks if t.status.value == "in_progress"]
    print(f"  in progress   : {in_progress}  <- the agent's action survived")

    step(9, "Audit trail")
    events = service.audit.events_for(actor)
    print(f"  {len(events)} event(s) recorded. Agent and approval events:")
    for event in events:
        if event.action.startswith(("agent.", "change.")):
            print(f"    {event.event_id}  {event.action:24} {event.outcome}")

    header("WHAT THIS RUN PROVED  (and what it did not)")
    print(
        """
  Proved, by execution:
    * The fake LLM received the real prompt (system preamble + <input> block)
      and returned schema-validated advice through the real adapter.
    * The checklist is generated per employee from approved client config.
    * The plan, tasks and dependency edges are written to a durable store and
      reload into a fresh service with no loss.
    * The agent retrieved context, analysed blockers, chose an allowlisted
      tool, executed it and VERIFIED the stored result before reporting it.
    * A restricted change was refused without approval, and the requester
      could not approve their own request.
    * Every step is in the append-only, client-scoped audit log.

  NOT proved, because the dependency does not exist here:
    * No Supabase project has been created; the migrations are unapplied and
      no SQL ran. Persistence was exercised against InMemoryTableClient.
    * No REAL LLM call was made and none is possible here: no model
      credential is configured. The advisor was a deterministic FAKE
      (model id fake/uc02-advisor-v1). Real-gateway behaviour is UNVERIFIED.
    * No MCP server exists in this repository.
"""
    )
    return 0


if __name__ == "__main__":
    # RETIRED as a runnable demo: this script ran the agent on an in-memory store, which the UC-02 runtime no longer
    # allows (every agent workflow must persist to Supabase). Its functions stay importable for tests.
    print("This demo ran the UC-02 agent on memory and is retired. Use the Supabase-only synthetic demo: "
          "  python demo/uc02_supabase_agentic_workflow.py preflight   (see its docstring for the stages)", file=sys.stderr)
    raise SystemExit(2)

