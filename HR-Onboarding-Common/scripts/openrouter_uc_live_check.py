"""MODE 1 -- REAL OPENROUTER: every UC's LLM-dependent path, through the central gateway, live.

NOT part of the test suite: it makes real, billable OpenRouter requests with the key in
HR-Onboarding-Common/.env. Run it yourself:

    .venv/Scripts/python.exe scripts/openrouter_uc_live_check.py [--json report.json]

For each UC it calls that UC's OWN gateway path with a real `LiteLLMGateway` (built by
`hr_onboarding_common.llm.build_llm`, LLM_MODE forced online) and records every gateway event (task,
route, attempt, latency, tokens) through a callback -- the evidence that a request reached OpenRouter.
Prompt, answer and reasoning text are never printed; the key is never printed.

  UC01  GatewayLlm.complete, task discrepancy_summary (UC01 venv)
  UC02  the orchestrator's real plan_tasks adapter -> UC02SubAgent -> uc02.advise (in process)
  UC03  name consistency and reminder draft via its advisor port (integrations/gateway, in process); low-confidence
        extraction sends no document text to a model by design
  UC04  LlmAdvisor over GatewayModelPort: the advisory narrative of a real UC04 evaluation (UC04 venv)
  UC05  answer_policy_question over the policy knowledge base (UC05 venv)

A UC whose path did not produce a "success" event is reported as NOT VERIFIED -- never as passed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

COMMON = Path(__file__).resolve().parents[1]
WORKSPACE = COMMON.parent
UC_DIRS = {"uc01": "UC01_Employee_Record_Creation", "uc03": "UC03_Document_Validation",
           "uc04": "UC04_Equipment_Access_Integration_Ready", "uc05": "UC05_Orientation-Schedule-Agent"}

# Shared by every snippet: a recording gateway built by the platform factory, online.
RECORDER = textwrap.dedent('''
    import json, os, sys
    from dataclasses import asdict
    os.environ["LLM_MODE"] = "online"
    from hr_onboarding_common.config import get_settings, load_environment
    load_environment()
    os.environ["LLM_MODE"] = "online"
    from hr_onboarding_common.llm import build_llm
    EVENTS = []
    def record(event):
        EVENTS.append({k: v for k, v in asdict(event).items() if v is not None})
    GATEWAY = build_llm(get_settings(), callbacks=[record])
    def emit(uc, path, outcome, **extra):
        print("UCRESULT " + json.dumps({"uc": uc, "path": path, "outcome": outcome, "gateway": type(GATEWAY).__name__,
                                        "events": EVENTS, **extra}))
''')

SNIPPETS = {
    "uc01": RECORDER + textwrap.dedent('''
        from sub_agent.llm import load_all_tasks, load_persona
        from sub_agent.llm.base import LlmRequest
        from sub_agent.llm.gateway_client import GatewayLlm
        client = GatewayLlm(GATEWAY, load_all_tasks(), load_persona())
        response = client.complete(LlmRequest(task="discrepancy_summary", inputs={
            "case_id": "SYN-CASE-001", "fields_in_conflict": "joining_date, designation",
            "other_source": "HRIS", "form_version": "1"}))
        emit("uc01", "GatewayLlm.complete(discrepancy_summary)", "ok", text_chars=len(response.text or ""),
             model=response.model if hasattr(response, "model") else None)
    '''),
    "uc04": RECORDER + textwrap.dedent('''
        sys.path.insert(0, "tests")
        from helpers import make
        from integrations.gateway import advisor_for_client
        from sub_agent.agent import Uc04Agent
        result = Uc04Agent().evaluate(make(), actor_id="SYN-USER-100", idempotency_key="live-check")
        advisor = advisor_for_client(GATEWAY, timeout_s=90)
        report = advisor.generate_advisory(result)
        emit("uc04", "LlmAdvisor(GatewayModelPort).generate_advisory", "ok" if report.narrative else "degraded",
             provider=report.provider, narrative_chars=len(report.narrative or ""), advisory_error=report.advisory_error,
             overall_status_from_service=result.overall_status)
    '''),
    "uc05": RECORDER + textwrap.dedent('''
        from datetime import date
        from context_layer.knowledge.policy_index import PolicyKnowledgeBase
        from hr_onboarding_common.platform.audit import AuditLog
        from sub_agent.services.policy_qa import answer_policy_question
        kb = PolicyKnowledgeBase()
        clients = sorted({getattr(d, "client_id", None) for d in getattr(kb, "documents", [])} - {None}) or ["CLIENT-ALPHA"]
        answer = answer_policy_question(GATEWAY, kb, AuditLog(), question="How many days of annual leave do new employees get?",
                                        client_id=clients[0], as_of=date(2026, 11, 4))
        emit("uc05", "answer_policy_question", "ok" if answer.answer else f"no_answer:{answer.no_answer_reason}",
             citations=len(answer.citations), client_id=clients[0])
    '''),
}


def run_snippet(uc: str) -> dict:
    root = WORKSPACE / UC_DIRS[uc]
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        return {"uc": uc, "outcome": "NOT RUN", "error": f"no virtualenv at {python.parent.parent}"}
    env = {**os.environ, "HR_ONBOARDING_ENV_FILE": str(COMMON / ".env"), "PYTHONPATH": os.pathsep.join(
        str(p) for p in (root / "src", root) if p.exists())}
    started = time.perf_counter()
    proc = subprocess.run([str(python), "-c", SNIPPETS[uc]], cwd=root, env=env, capture_output=True, text=True,
                          timeout=600)
    for line in proc.stdout.splitlines():
        if line.startswith("UCRESULT "):
            return {**json.loads(line[9:]), "wall_s": round(time.perf_counter() - started, 1)}
    tail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1][:300]
    return {"uc": uc, "outcome": "ERROR", "error": tail}


def run_in_process() -> list[dict]:
    """UC02 and UC03 run inside the orchestrator process, as they do in production."""
    from dataclasses import asdict

    os.environ["LLM_MODE"] = "online"
    from hr_onboarding_common.config import get_settings, load_environment

    load_environment()
    os.environ["LLM_MODE"] = "online"
    from hr_onboarding_common.llm import build_llm

    results = []

    # --- UC02: the orchestrator's real plan_tasks adapter, with the gateway as the orchestrator's LLM.
    events: list[dict] = []
    gateway = build_llm(get_settings(), callbacks=[lambda e: events.append(
        {k: v for k, v in asdict(e).items() if v is not None})])
    from types import SimpleNamespace

    from hr_onboarding_common.orchestration.adapters.uc02_tasks import call_uc02_plan_tasks
    from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state

    sys.path.insert(0, str(COMMON / "tests" / "orchestration"))

    class Deps:
        policy = SimpleNamespace(settings={"task_planning_trigger_roles": ["hr_admin"]})
        uc03_repo_root = WORKSPACE / UC_DIRS["uc03"]
        uc03_document_store_path = None
        _uc02_sub_agent = None
        _uc03_graph = _uc03_case_store = _uc03_audit_store = _uc03_checklist_service = None
        _uc03_collection_service = None
        ctx = SimpleNamespace(llm=gateway)

    actor = {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": "CLIENT-ALPHA"}
    state = new_state(case_id="SYN-LIVE-UC02", client_id="CLIENT-ALPHA", correlation_id="live-check", actor=actor,
                      intake_trigger={"offer_accepted": True}, uc02_role_context={
                          "role_id": "backend_engineer", "department": "Engineering", "manager_id": "MGR-1",
                          "location": "Dubai", "project": "apollo", "offer_accepted_at": "2026-10-20T10:00:00Z",
                          "form_approved_at": "2026-10-21T10:00:00Z", "approved_by_role": "hr_ops"})
    state["employee_id"], state["joining_date"] = "EMP-SYN-001", "2026-11-16"
    state["stage_statuses"][Stage.INTAKE.value] = StageStatus.COMPLETE.value
    result = call_uc02_plan_tasks(state, Deps())
    plan = result.get("task_plan") or {}
    results.append({"uc": "uc02", "path": "orchestrator plan_tasks -> UC02SubAgent(gateway) -> uc02.advise",
                    "outcome": result["stage_statuses"][Stage.PLAN_TASKS.value], "gateway": type(gateway).__name__,
                    "advice_source": plan.get("advice_source"), "advice_degraded": plan.get("advice_degraded"),
                    "tasks": len(plan.get("tasks") or []), "events": events})

    # --- UC03: its model use goes through its advisor port (integrations/gateway/GatewayAdvisor over the shared
    # client). Name check and reminder draft are advisory; low-confidence extraction sends no document text to any
    # model by design, so the extraction below exercises UC03's safe low-confidence path, not a model call.
    import asyncio

    uc03 = WORKSPACE / UC_DIRS["uc03"]
    sys.path.insert(0, str(uc03))
    from integrations.gateway import GatewayAdvisor
    from src.sub_agent.comms import draft_communication
    from src.sub_agent.extraction import LLMDocumentExtractor
    from src.sub_agent.name_agent import check_name_consistency_agent

    events3: list[dict] = []
    advisor = GatewayAdvisor(build_llm(get_settings(), callbacks=[lambda e: events3.append(
        {k: v for k, v in asdict(e).items() if v is not None})]))

    async def uc03_calls():
        name = await check_name_consistency_agent("Yogesh Velayutham", "Yogesh Velayutham Subramaniam", 0.95,
                                                  advisor=advisor)
        extractor = LLMDocumentExtractor()

        async def text(_ref):
            return ("REPUBLIC OF EXAMPLE PASSPORT\nSurname: SAMPLE\nGiven names: ALEX\nPassport No: X1234567\n"
                    "Date of expiry: 12 MAR 2031\nNationality: EXAMPLE")

        extractor._read_text_snippet = text
        extracted = await extractor.extract("synthetic://passport", "passport")
        draft = await draft_communication("SEND_REMINDER", "Alex Sample", "Passport", ["Passport", "Payslips"],
                                          advisor=advisor)
        return name, extracted, draft

    name, extracted, draft = asyncio.run(uc03_calls())
    results.append({"uc": "uc03", "path": "name_agent + comms via advisor port (extraction: no model by design)",
                    "outcome": "ok",
                    "gateway": "LiteLLMGateway", "name_used_llm": name.used_llm, "name_consistent": name.is_consistent,
                    "extraction_confidence": extracted.confidence, "extracted_field_names": sorted(extracted.extracted_fields),
                    "draft_chars": len(draft), "events": events3})
    return results


def summarize(result: dict) -> dict:
    successes = [e for e in result.get("events", []) if e.get("kind") == "success"]
    return {
        "uc": result["uc"], "verified_real_call": bool(successes), "outcome": result.get("outcome"),
        "successful_calls": len(successes), "tasks": sorted({e["task"] for e in successes}),
        "routes": sorted({e["model"] for e in successes}),
        "latency_ms": [e.get("latency_ms") for e in successes],
        "tokens_in_out_reasoning": [(e.get("prompt_tokens"), e.get("completion_tokens"), e.get("reasoning_tokens"))
                                    for e in successes],
        "non_success_events": [(e["kind"], e.get("task"), e.get("error_type")) for e in result.get("events", [])
                               if e.get("kind") != "success"],
        "error": result.get("error"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, help="also write the full report here")
    args = parser.parse_args()
    results = run_in_process() + [run_snippet(uc) for uc in ("uc01", "uc04", "uc05")]
    results.sort(key=lambda r: r["uc"])
    rows = [summarize(r) for r in results]
    for row, full in zip(rows, results):
        print(f"[{row['uc']}] real OpenRouter call verified: {row['verified_real_call']}  outcome={row['outcome']}")
        print(f"    tasks={row['tasks']} routes={row['routes']} latency_ms={row['latency_ms']}")
        print(f"    tokens(in,out,reasoning)={row['tokens_in_out_reasoning']}")
        extra = {k: v for k, v in full.items() if k not in ("events", "uc", "outcome", "path", "gateway")}
        print(f"    path={full.get('path')}  details={extra}")
        if row["non_success_events"]:
            print(f"    non-success events: {row['non_success_events']}")
    if args.json:
        args.json.write_text(json.dumps({"summary": rows, "results": results}, indent=1, default=str), encoding="utf-8")
    return 0 if all(r["verified_real_call"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
