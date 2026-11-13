"""Dependency bundle the orchestrator agent hands to every adapter node."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.orchestration.case_store import InMemoryCaseStateStore
from hr_onboarding_common.orchestration.notifications import MockNotificationOutbox
from hr_onboarding_common.orchestration.tenant_config import TenantConfig
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerPool


@dataclass
class OrchestratorDeps:
    ctx: AgentContext  # the orchestrator's OWN policy/audit/hitl -- its own gates only
    worker_pool: WorkerPool  # exactly one worker each for uc01/uc04/uc05
    uc03_repo_root: Path  # UC03's repo root, inserted onto sys.path by the UC03 adapter
    # In-process UC03 validation of all of a case's documents is cancelled after this many seconds
    # (adapters/uc03_documents.py). The worker UCs have their own timeouts (workers/config.py).
    uc03_timeout_s: float = 60.0
    checkpointer: Any = None  # LangGraph MemorySaver (default) or SqliteSaver (persist_dir given)
    policy: Any = None  # set by OnboardingOrchestratorAgent.__init__ (self.policy is a property)
    # Authoritative last-known-state handoff for cross-call resumption (see
    # OnboardingOrchestratorAgent.advance_case and orchestration/case_store.py): written through
    # after every stage node. In memory by default; SqliteCaseStateStore survives a restart.
    # LangGraph's own checkpoint `get_state` after a thread reaches END was observed to return
    # inconsistent values across invocation paths in this langgraph version, so routine resume
    # goes through this explicit handoff; the checkpointer still records every step.
    case_states: Any = field(default_factory=InMemoryCaseStateStore)
    # Stage -> adapter override, keyed by Stage value. Empty in production. Tests use it to put a
    # contract-compatible double in front of a UC that cannot run (UC02's import defect) or is not
    # under test; the double returns the same OnboardingCaseState shape the real adapter would.
    stage_adapters: dict[str, Callable[..., Any]] = field(default_factory=dict)
    # Tenant settings: timezone/run time for the joining-day scheduler, employee identity scheme
    # (orchestration/tenant_config.py; build_deps loads config/tenants.yaml). Code defaults otherwise.
    tenants: TenantConfig = field(default_factory=TenantConfig)
    # Intake-only inputs (id number, email, the onboarding form), keyed by the case's original id.
    # Deliberately process memory only: they are never part of graph state, so no checkpoint and
    # no case store holds them. Dropped once intake completes; a restart before then means the
    # start request is resubmitted (INTAKE_INPUT_UNAVAILABLE).
    intake_inputs: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    # Onboarding-form extractions awaiting a Start New Onboarding submit, keyed by extraction id
    # (orchestration/onboarding_documents.py): the tenant, statuses and field names of UC01's extraction, and the
    # prefilled values only to tell which ones HR changed. Process memory only, like intake_inputs; the case records
    # a summary without values. A restart means extracting the document again.
    document_extractions: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    # The document-collection request's transport. MOCK only (orchestration/notifications.py): no
    # real-time messaging integration exists in this project.
    notifier: Any = field(default_factory=MockNotificationOutbox)
    # UC03's document registry (collection per plan version, stored revisions): a SQLite file when set
    # (survives a restart; bootstrap.build_deps puts it under persist_dir), in memory otherwise.
    uc03_document_store_path: Path | None = None
    # UC03's stores (orchestration/uc03_supabase.py): "memory" or "supabase"; None = UC03_STORE_MODE from the
    # environment (default memory). In "supabase" UC03's case state, audit, results AND its document registry are in
    # Supabase, on a table client built from the orchestrator's scoped settings -- or from this factory (tests).
    uc03_store_mode: str | None = None
    uc03_tables_factory: Callable[[], Any] | None = field(default=None, repr=False)
    # UC04's configuration source (uc04_config.py): "file" or "supabase"; None = UC04_CONFIG_SOURCE (default file).
    # The reader returns the tenant's uc04_equipment_access section metadata from Supabase (tests inject one).
    uc04_config_source: str | None = None
    uc04_config_reader: Callable[[str], Any] | None = field(default=None, repr=False)
    # How UC04 reads the case's UC-02 onboarding plan (adapters/uc04_uc02_plan.py): None = UC-02's own service (the
    # production path). The synthetic demo sets an explicit, labelled stand-in reader; tests inject one.
    uc02_plan_reader: Callable[..., Any] | None = field(default=None, repr=False)
    # Where UC05 resolves the offer acceptance from: "mailbox" (its mock/real mailbox; the case carries the event id),
    # "case" (UC05's Supabase mode: the onboarding case itself is the event, so its case id is the reference), or
    # "orchestrator_case" (UC05 reads the joiner from this orchestrator's case -- the HR-approved form -- registered
    # with the UC05 worker; the case id is the event).
    uc05_offer_event_source: str = "mailbox"
    # Writes the orchestrator's own approval requests through to Supabase `approval_requests`
    # (approval_requests.py); None: the HITL inbox only.
    approval_mirror: Any = None
    # How often one invocation may run the UC03 -> UC02 replanning loop before the case parks for a
    # person (REPLAN_LIMIT_REACHED). Bounds the second cycle in the graph.
    max_replan_rounds: int = 3
    # Set by OnboardingOrchestratorAgent: writes a state through to the case store before a side
    # effect whose outcome could be lost (write-ahead), so a restart sees "sending", not nothing.
    persist_progress: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False)
    # UC-02 in Supabase mode (UC02_STORE_MODE=supabase, adapters/uc02_runtime.py): a factory for UC-02's Supabase
    # REST client. None in production (built from the environment); tests inject an in-process PostgREST here.
    uc02_rest_factory: Callable[[], Any] | None = field(default=None, repr=False)
    # cache slots for in-process UC02/UC03 state built once and reused across calls within
    # this orchestrator instance's lifetime (mirrors the single-worker-per-UC state-continuity
    # rule for the two UCs that don't need a separate process).
    _uc02_sub_agent: Any = field(default=None, repr=False)
    _uc03_graph: Any = field(default=None, repr=False)
    _uc03_case_store: Any = field(default=None, repr=False)
    _uc03_audit_store: Any = field(default=None, repr=False)
    _uc03_checklist_service: Any = field(default=None, repr=False)
    _uc03_collection_service: Any = field(default=None, repr=False)
    _uc03_tables: Any = field(default=None, repr=False)
    _uc03_resolved_mode: str | None = field(default=None, repr=False)
