"""MODE 1 -- REAL OPENROUTER, REAL AGENTS: one orchestrated case through the real UC01-UC05 agents online.

NOT part of the test suite (real, billable OpenRouter requests). Run it yourself:

    .venv/Scripts/python.exe scripts/run_online_orchestration_check.py

The orchestrator is built by `build_deps` with LLM_MODE=online: its LLM is the central LiteLLMGateway,
which UC02 (in process) and UC03 (in process) use; the UC05 worker builds its own gateway from the same
configuration. A recording callback on the orchestrator's gateway shows which in-process UC actually
called the model. One submitted document carries a name variant, so UC03's name check needs the model.

What it cannot show, stated rather than hidden: UC04's contract accepts synthetic ids only
(`SYN-CASE-...`) while UC01 issues uuid case ids, so the real UC04 rejects the case and the workflow
cannot reach readiness with real agents (see PRODUCTION_READINESS_GAP_REPORT.md). The case store here is
in memory: the Supabase-persisted run is tests/orchestration/test_live_supabase_orchestration.py.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path

COMMON = Path(__file__).resolve().parents[1]
WORKSPACE = COMMON.parent


def main() -> int:
    os.environ["LLM_MODE"] = "online"
    for uc_id, folder in (("uc01", "UC01_Employee_Record_Creation"), ("uc04", "UC04_Equipment_Access_Integration_Ready"),
                          ("uc05", "UC05_Orientation-Schedule-Agent")):
        # Each colliding UC runs in its own virtualenv (orchestration/workers); an explicit setting wins.
        os.environ.setdefault(f"{uc_id.upper()}_WORKER_SRC_DIR", str(WORKSPACE / folder / "src"))
    from hr_onboarding_common.orchestration.bootstrap import build_deps
    from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput
    from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent

    ctx, deps = build_deps(agent_home=COMMON, uc03_repo_root=WORKSPACE / "UC03_Document_Validation",
                           state_store="memory")
    events: list[dict] = []
    ctx.llm.callbacks.append(lambda e: events.append({k: v for k, v in asdict(e).items() if v is not None}))
    agent = OnboardingOrchestratorAgent(ctx)
    actor = {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": "CLIENT-ALPHA"}
    try:
        print(f"orchestrator LLM: {type(ctx.llm).__name__}")
        state = agent.advance_case(OnboardingCaseInput(
            client_id="CLIENT-ALPHA", hr_officer_id=actor["on_behalf_of"], offer_accepted=True,
            offer_reference="OFR-ONLINE-CHECK", offer_acceptance_event_id="EVT-ONLINE-CHECK",
            candidate_name="Arun Kumar", candidate_email="arun.kumar@example.invalid", designation="Backend Engineer",
            joining_date="2026-12-01", work_location_type="OFFICE", id_number="SYN-ID-ONLINE",
            role_id="backend_engineer", department="Technology", manager_id="MGR-001", location="Dubai",
            project="apollo", offer_accepted_at="2026-10-20T10:00:00Z", form_approved_at="2026-10-21T10:00:00Z",
            form_approved_by_role="hr_ops",
            uc04_client_config_ref=str(WORKSPACE / "UC04_Equipment_Access_Integration_Ready" / "data" /
                                       "client_alpha_config.json")), case_id=None, actor=actor)
        case_id = state["case_id"]
        print(f"after intake/plan: case={case_id} plan_tasks={state['stage_statuses']['plan_tasks']} "
              f"advice_source={(state.get('task_plan') or {}).get('advice_source')}")
        docs = []
        for i, o in enumerate(state["document_collection"]["outstanding"]):
            name = "Arun Kumar Rajendran" if i == 0 else "Arun Kumar"  # a father-name variant on the first document
            docs.append({"document_type": o["document_type"], "document_ref": f"SYN-DOC-{o['document_type']}",
                         "metadata": {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01",
                                      "evidence_stored": True, "extracted_name": name, "attestation_confirmed": True}})
        state = agent.submit_documents(case_id, documents=docs, actor=actor)
        print(f"after submission: validate_documents={state['stage_statuses']['validate_documents']}")
        state = agent.review_documents(case_id, reviews=[{"document_type": d["document_type"], "decision": "approved"}
                                                         for d in docs], actor=actor)
        statuses = state["stage_statuses"]
        print("after HR review: " + ", ".join(f"{s}={statuses[s]}" for s in (
            "validate_documents", "equipment_access", "orientation_scheduling", "day_one_readiness")))
        print(f"workflow_status={state['workflow_status']} errors="
              f"{ {s: e.get('code') for s, e in state['stage_errors'].items()} }")
    finally:
        deps.worker_pool.stop_all()
    print("\norchestrator-gateway events (in-process UCs):")
    for e in events:
        print(f"  {e['kind']:8} task={e['task']:28} route={e['model']} latency_ms={e.get('latency_ms')} "
              f"tokens={e.get('prompt_tokens')}/{e.get('completion_tokens')}/{e.get('reasoning_tokens')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
