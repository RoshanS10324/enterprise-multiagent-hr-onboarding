"""Generic worker runtime for UC01/UC04/UC05 -- the three UC agents that collide on the
top-level module name `sub_agent`.

STDLIB ONLY at module level. This file imports nothing from `hr_onboarding_common` and no
third-party package at import time: it is launched under *that UC's own interpreter* (its own
`.venv`), which generally does not have `hr_onboarding_common` installed (only UC02/UC05 depend on
it -- the UC05 handler below imports it lazily, from UC05's own environment). It is invoked as a
bare script path (`<uc_python> uc_worker_runtime.py --uc uc04 --src-dir <path>`), never as
`python -m hr_onboarding_common...`, so the target interpreter never needs this package on its own
sys.path.

Protocol: one JSON object per line on stdin -> one JSON object per line on stdout. No network
socket, no FastAPI -- fully local stdio pipes. Exactly one such process is ever started per
colliding UC (never a pool): each holds that UC's business state in memory for the lifetime of
the process (unless that UC persists it itself -- UC05 with UC05_WORKER_DATA_DIR), and two
independent processes for the same UC would NOT share that state -- see
orchestration/workers/uc_worker_client.py and docs/ONBOARDING_COMMON_ARCHITECTURE.md.

stdout is reserved for the protocol: `sys.stdout` is pointed at stderr before any UC code is
imported, so a stray print or a logger configured for stdout can never corrupt a response line.

Failures are returned classified (`_classify`): a short `error_code`, the exception type and a
truncated message. Tracebacks go to stderr only (the client keeps it in a log file) -- never into
a response, where they would end up in case state or an API body.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
import traceback
from pathlib import Path

# Exception type names (anywhere in the MRO) that mean "the provider/infrastructure failed and the
# same request may succeed later". Matched by name: this file cannot import the UCs' exception types.
_RETRYABLE_TYPES = frozenset({"CalendarBookingError", "NotificationDeliveryError", "EmailServiceError", "LLMError",
                              "TimeoutError", "ConnectionError", "OperationalError"})
# hr_onboarding_common.llm.redact.SECRET_ENV_VARS (not importable in every UC's interpreter).
_PROVIDER_CREDENTIALS = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "AZURE_API_KEY")


def _llm_status(uc_id: str, handler) -> dict:
    """`__llm_status__`: which LLM this worker runs with. Names, modes and booleans only -- never a
    credential's value."""
    status = {"uc": uc_id, "llm_mode": os.environ.get("LLM_MODE", "offline"),
              "env_file_configured": bool(os.environ.get("HR_ONBOARDING_ENV_FILE")),
              "credentials_present": {name: bool(os.environ.get(name)) for name in _PROVIDER_CREDENTIALS}}
    if hasattr(handler, "llm_status"):
        status.update(handler.llm_status())
    else:
        status.update(implementation=None, detail=f"{uc_id} makes no model calls in this worker")
    return status


class _Refused(Exception):
    """A request the UC refused for a reason the caller must act on, with an explicit code."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _classify(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, _Refused):
        return exc.code, False
    names = {c.__name__ for c in type(exc).__mro__}
    if names & _RETRYABLE_TYPES:
        return "RETRYABLE_PROVIDER_ERROR", True
    if "PermissionError" in names:
        return "POLICY_DENIED", False
    if "KeyError" in names:
        return "NOT_FOUND", False
    if "ValueError" in names:  # includes pydantic.ValidationError
        return "INVALID_INPUT", False
    return "INTERNAL_ERROR", False


def _short(exc: BaseException, limit: int = 300) -> str:
    return f"{type(exc).__name__}: {str(exc)[:limit]}"


def _identity_check(uc_id: str, src_dir: Path) -> dict:
    entry_module, entry_attr = {
        "uc01": ("sub_agent.onboarding_entry", "open_onboarding_case"),
        "uc04": ("sub_agent.agent", "Uc04Agent"),
        "uc05": ("sub_agent.agent", "OnboardingAgent"),
    }[uc_id]
    import importlib

    mod = importlib.import_module(entry_module)
    attr = getattr(mod, entry_attr)
    actual_file = Path(inspect.getfile(attr)).resolve()
    src_dir_resolved = src_dir.resolve()
    under_src_dir = str(actual_file).startswith(str(src_dir_resolved))
    return {
        "module": getattr(attr, "__module__", mod.__name__),
        "qualname": getattr(attr, "__qualname__", entry_attr),
        "file": str(actual_file),
        "under_configured_src_dir": under_src_dir,
    }


class _Uc01Handler:
    """Holds UC01's own in-memory deps/container for the life of this worker process -- a case
    created by one request must be visible to a later request (no pooling; see client)."""

    def __init__(self) -> None:
        from sub_agent.api.container import build_container  # type: ignore
        from sub_agent.api.settings import Settings  # type: ignore

        self._container = build_container(Settings.from_env())

    def dispatch(self, op: str, payload: dict, *, run_id: str, idempotency_key: str, actor: dict) -> dict:
        from sub_agent.onboarding_entry import OpenCaseRequest, open_onboarding_case  # type: ignore

        if op == "extract_onboarding_document":
            return self._extract_onboarding_document(payload)
        if op == "open_onboarding_case":
            form = payload.get("form")
            from sub_agent.models.onboarding_form import OnboardingForm  # type: ignore

            request = OpenCaseRequest(
                request_id=payload["request_id"],
                client_id=payload["client_id"],
                hr_officer_id=payload["hr_officer_id"],
                offer_accepted=payload["offer_accepted"],
                form=OnboardingForm.model_validate(form) if form else None,
                hiring_freeze=payload.get("hiring_freeze", False),
                id_number=payload.get("id_number"),
                **self._placement(payload.get("placement")),
            )
            result = open_onboarding_case(request, self._container.onboarding_deps)
            return result.model_dump(mode="json")
        raise ValueError(f"uc01: unknown op '{op}'")

    def _extract_onboarding_document(self, payload: dict) -> dict:
        """UC01's own onboarding-form extraction and validation (sub_agent.documents), READ-ONLY: read (docx / text
        PDF, with UC01's size, structure and safety limits) -> extract (deterministic, template onboarding-form-2026)
        -> validate. No case is opened, nothing is stored or audited as an intake: the case is created later, from
        the form HR confirms, through the orchestrator's gated path. Returns UC01's safe view only (sensitive values --
        salary, mobile, parents' names -- by status, never by value)."""
        import base64
        from datetime import date

        from sub_agent.documents.docx_reader import DocumentReadError  # type: ignore
        from sub_agent.documents.service import safe_extraction_view  # type: ignore
        from sub_agent.documents.validation import validate_extraction  # type: ignore

        data = base64.b64decode(payload["content_base64"], validate=True)
        filename = str(payload["filename"])
        try:
            extraction = self._container.documents.extract(data, filename)
        except DocumentReadError as exc:
            return {"status": "REJECTED", "code": exc.code, "reason": str(exc).split(": ", 1)[-1][:300]}
        outcome = validate_extraction(extraction, candidate_email=payload.get("candidate_email"),
                                      work_location_type=payload.get("work_location_type"), today=date.today())
        md = extraction.metadata
        return {"status": "VALIDATION_FAILED" if outcome.errors else "NEEDS_REVIEW" if outcome.review_reasons
                else "VALID",
                "errors": list(outcome.errors), "review_reasons": list(outcome.review_reasons),
                "outstanding_documents": list(outcome.outstanding_documents),
                "extraction": safe_extraction_view(extraction),
                "source_document": {"sha256": md.source_sha256, "size_bytes": md.source_size_bytes,
                                    "template_id": md.template_id, "template_recognised": md.template_recognised,
                                    "extracted_at": md.extracted_at.isoformat()}}

    @staticmethod
    def _placement(placement: dict | None) -> dict:
        """UC01's `placement` keyword, only when the caller supplied one and this UC01 version accepts
        it (an older UC01 has no CasePlacement). Absent -> UC01 itself holds the case for HR."""
        if not placement:
            return {}
        try:
            from sub_agent.models.case import CasePlacement  # type: ignore
        except ImportError:
            return {}
        return {"placement": CasePlacement.model_validate(placement)}


def _uc04_state_mode() -> str:
    """UC04_STATE_MODE for this worker: memory (default: UC04's in-memory store and mock parent-case registry) or
    supabase (UC04's own persistence adapters: the parent case read from onboarding_cases, UC04's case state as versioned
    snapshots in audit_events, its audit entries in audit_events, its runs in agent_runs). Anything else stops the
    worker: never a silent fallback."""
    mode = (os.environ.get("UC04_STATE_MODE") or "memory").strip().lower() or "memory"
    if mode not in ("memory", "supabase"):
        raise SystemExit(f"UC04_STATE_MODE={mode!r} is not one of memory, supabase")
    return mode


def _uc04_supabase_settings() -> tuple[str, str, str]:
    """Checked at worker startup (SystemExit before anything is served): the PLATFORM identity profile and the
    worker-scoped Supabase settings. No network here; the handler checks the tables when it is built."""
    from guardrails import identity as uc04_identity  # type: ignore

    if uc04_identity.active().name != "PLATFORM":
        raise SystemExit("UC04_STATE_MODE=supabase needs UC04_IDENTITY_PROFILE=PLATFORM: the shared tables key cases by "
                         "their uuid")
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    missing = [n for n, v in (("UC04_WORKER_ENV__SUPABASE_URL", url), ("UC04_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY", key))
               if not v]
    if missing:
        raise SystemExit(f"UC04_STATE_MODE=supabase but {', '.join(missing)} is not set")
    return url, key, (os.environ.get("SUPABASE_SCHEMA") or "public").strip() or "public"


def _uc04_supabase_agent(uc04_agent_class):
    """A Uc04Agent on UC04's own Supabase adapters (src/sub_agent/persistence) over UC04's own PostgREST client
    (integrations/supabase/postgrest_table_client.py -- the layer UC04's packaging rules reserve for network code).
    The worker-scoped SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (UC04_WORKER_ENV__*) reach this process only."""
    import importlib.util

    url, key, schema = _uc04_supabase_settings()
    root = Path(sys.modules["sub_agent"].__file__).resolve().parents[2]  # UC04's repository root
    path = root / "integrations" / "supabase" / "postgrest_table_client.py"
    spec = importlib.util.spec_from_file_location("uc04_postgrest_table_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.PostgrestTableClient(url, key, schema)
    for table in ("onboarding_cases", "audit_events", "agent_runs"):  # before the first evaluation, never during it
        if not client.table_exists(table):
            raise RuntimeError(f"UC04_STATE_MODE=supabase but the table {table} is not exposed by the Supabase API")
    from sub_agent.persistence import (AuditEventStateBackend, DatabaseAgentRunRecorder,  # type: ignore
                                       DatabaseAuditSink, DatabaseCaseContext, DatabaseCaseRepository)

    print("uc04 worker: Supabase state (onboarding_cases, audit_events snapshots, agent_runs)", file=sys.stderr)
    return uc04_agent_class(cases=DatabaseCaseContext(client), store=DatabaseCaseRepository(AuditEventStateBackend(client)),
                            audit_sink=DatabaseAuditSink(client), run_recorder=DatabaseAgentRunRecorder(client))


class _Uc04Handler:
    def __init__(self) -> None:
        from sub_agent.agent import Uc04Agent  # type: ignore

        self.state_mode = _uc04_state_mode()
        if self.state_mode == "supabase":
            # The parent case is UC01's onboarding_cases row (DatabaseCaseContext, read-only): nothing is registered here.
            self._agent = _uc04_supabase_agent(Uc04Agent)
            self._cases = None
            return
        from sub_agent.adapters.case_context_mock import MockCaseContext  # type: ignore

        # An empty registry: UC04's own sample parent cases (data/parent_cases.json) are not loaded -- the orchestrator
        # registers each case it runs (_ensure_case).
        self._agent = Uc04Agent(cases=MockCaseContext())
        # UC04 never creates a parent case itself (BR-24) -- its mock case registry is a stand-in
        # for an upstream system. The orchestrator IS that upstream system here, so it populates
        # the mock registry directly, exactly as UC04's own test harness (`Env`) does.
        self._cases = self._agent._cases  # noqa: SLF001 -- intentional, see docstring above

    def _ensure_case(self, case_id: str, employee_ref: str, client_id: str) -> None:
        from sub_agent.ports import ParentCase  # type: ignore

        if self._cases is None:  # Supabase mode: UC04 reads the case from onboarding_cases itself
            return
        if self._cases.get_case(case_id) is None:
            self._cases._cases[case_id] = ParentCase(  # noqa: SLF001
                case_id=case_id, employee_ref=employee_ref, client_id=client_id
            )

    def dispatch(self, op: str, payload: dict, *, run_id: str, idempotency_key: str, actor: dict) -> dict:
        from sub_agent.contracts import Uc04Input  # type: ignore
        from sub_agent.orchestrated import Actor, OrchestratorRequest, WorkflowRef  # type: ignore

        if op == "evaluate_orchestrated":
            # UC04's identifier profile is fixed for this process (guardrails/identity.py). A platform case id under
            # LOCAL_SIMULATION is a missing integration decision, reported as such -- not a malformed request.
            import re

            from guardrails import identity as uc04_identity  # type: ignore

            patterns = uc04_identity.active()
            case_id = str((payload.get("input") or {}).get("case_id", ""))
            if patterns.name == "LOCAL_SIMULATION" and not re.match(patterns.case_id, case_id):
                return {"blocked": {
                    "code": "UC04_IDENTITY_PROFILE_NOT_ENABLED", "owner": "ops",
                    "detail": "UC04 runs with its LOCAL_SIMULATION identity profile (synthetic SYN-* ids only); this "
                              "case's platform id needs the PLATFORM profile (UC04_IDENTITY_PROFILE=PLATFORM)",
                    "next_action": "Enable UC04's PLATFORM identity profile after the shared-contract approval"}}
            inp = Uc04Input.model_validate(payload["input"])
            self._ensure_case(inp.case_id, inp.employee_ref, inp.client_id)
            request = OrchestratorRequest(
                run_id=run_id,
                idempotency_key=idempotency_key,
                workflow=WorkflowRef(id="employee-onboarding", instance=inp.case_id, step="equipment_access"),
                actor=Actor(on_behalf_of=actor["on_behalf_of"], roles=actor["roles"]),
                input=inp,
            )
            response = self._agent.evaluate_orchestrated(request)
            out = response.model_dump(mode="json")
            recommendation = self._role_package(inp, out)
            if recommendation is not None:
                out["role_package_recommendation"] = recommendation
            return out
        raise ValueError(f"uc04: unknown op '{op}'")

    @staticmethod
    def _role_package(inp, out: dict) -> dict | None:
        """UC04's advisory skill recommend_role_standard_items, applied when UC04 asks HR what to request
        (NO_ITEMS_REQUESTED): the role's standard package from the case's own configuration. Read-only; HR decides.
        A UC04 without the skill simply gives no recommendation."""
        blockers = ((out.get("result") or {}).get("blockers")) or []
        if not any(b.get("code") == "NO_ITEMS_REQUESTED" for b in blockers):
            return None
        try:
            from sub_agent.role_packages import recommend_standard_items  # type: ignore
        except ImportError:
            return None
        return recommend_standard_items(inp).model_dump(mode="json")


def _uc05_offer_event_source() -> str:
    """UC05_OFFER_EVENT_SOURCE for this worker: "" / mailbox (UC05's own mailbox) or orchestrator_case. Anything else,
    or orchestrator_case together with UC05's Supabase mode (where onboarding_cases is already the event), stops the
    worker: one source of truth, never a silent fallback."""
    source = (os.environ.get("UC05_OFFER_EVENT_SOURCE") or "").strip().lower()
    if source not in ("", "mailbox", "orchestrator_case"):
        raise SystemExit(f"UC05_OFFER_EVENT_SOURCE={source!r} is not a known offer-event source")
    if source == "orchestrator_case" and (os.environ.get("UC05_DATABASE_MODE") or "").strip().lower() == "supabase":
        raise SystemExit("UC05_OFFER_EVENT_SOURCE=orchestrator_case cannot be combined with UC05_DATABASE_MODE="
                         "supabase: there the onboarding case in onboarding_cases is already the event")
    return source


def _persistent_sim_calendar(path: Path, repository):
    """UC05's simulated calendar (connectors.calendar.mock.MockCalendar), saved to `path` after every change, so a
    worker restart keeps the holds UC05's (persisted) session records point at. Without it a restart empties the
    simulated calendar while UC05 still holds the sessions, and confirming one fails with KeyError(<event ref>).

    At start, a SCHEDULED or CONFIRMED session whose simulated hold ("sim-evt...") the calendar does not hold -- live or
    cancelled -- is restored from UC05's own session record (attendees, times, status). That only re-aligns the
    simulated provider with UC05's records: nothing is booked anew, nobody is invited, nothing is approved, and a hold
    the calendar itself cancelled is never revived. Simulated calendar only; a real provider is never touched."""
    from datetime import datetime

    from connectors.calendar.base import BookingStatus, CalendarBooking  # type: ignore
    from connectors.calendar.mock import MockCalendar  # type: ignore
    from sub_agent.models import SessionStatus  # type: ignore

    class PersistentSimCalendar(MockCalendar):
        def __init__(self) -> None:
            self._path = None  # reset() (called by MockCalendar.__init__) saves only once loading is done
            super().__init__()
            if path.exists():
                self._load()
            self._path = path
            self.restored = self._restore_from(repository)
            self._save()

        def _load(self) -> None:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._bookings = {k: CalendarBooking.model_validate(v) for k, v in data.get("bookings", {}).items()}
            self._cancelled = {k: CalendarBooking.model_validate(v) for k, v in data.get("cancelled", {}).items()}
            self._by_key = dict(data.get("by_key", {}))
            self._busy = [(a, datetime.fromisoformat(s), datetime.fromisoformat(e)) for a, s, e in data.get("busy", [])]
            self.invited = [tuple(x) for x in data.get("invited", [])]

        def _save(self) -> None:
            if self._path is None:
                return
            data = {"bookings": {k: b.model_dump(mode="json") for k, b in self._bookings.items()},
                    "cancelled": {k: b.model_dump(mode="json") for k, b in self._cancelled.items()},
                    "by_key": self._by_key, "invited": [list(x) for x in self.invited],
                    "busy": [[a, s.isoformat(), e.isoformat()] for a, s, e in self._busy]}
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._path)

        def _restore_from(self, repo) -> list[str]:
            restored = []
            try:
                records = repo.list_onboarding_records()
            except Exception as exc:  # noqa: BLE001 -- UC05's Supabase repository lists per verified tenant only
                if type(exc).__name__ != "TenantScopeRequiredError":
                    raise
                # In UC05's Supabase mode the sessions are in orientation_sessions and every listing needs a tenant;
                # the simulated calendar's own JSON copy (saved after every change) is what survives a restart.
                print("uc05 worker: simulated calendar not realigned from session records (UC05's Supabase mode "
                      "lists sessions per tenant only)", file=sys.stderr)
                return restored
            for record in records:
                for s in repo.list_sessions(record.employee_id):
                    ref = s.external_event_ref or ""
                    if (not ref.startswith("sim-evt") or ref in self._bookings or ref in self._cancelled
                            or s.status not in (SessionStatus.SCHEDULED, SessionStatus.CONFIRMED)):
                        continue
                    confirmed = s.status == SessionStatus.CONFIRMED
                    self._bookings[ref] = CalendarBooking(
                        event_ref=ref, status=BookingStatus.CONFIRMED if confirmed else BookingStatus.TENTATIVE,
                        attendees=list(s.attendees), start=s.scheduled_start, end=s.scheduled_end,
                        invitations_sent=confirmed)
                    self._busy.extend((a, s.scheduled_start, s.scheduled_end) for a in s.attendees)
                    if s.booking_operation_key:
                        self._by_key[s.booking_operation_key] = ref
                    restored.append(ref)
            return restored

        def reset(self) -> None:
            super().reset()
            self._save()

        def book_event(self, **kwargs):
            try:
                return super().book_event(**kwargs)
            finally:  # also when the simulated provider "loses" the response after creating the event
                self._save()

        def confirm_event(self, event_ref, **kwargs):
            try:
                return super().confirm_event(event_ref, **kwargs)
            finally:
                self._save()

        def cancel_event(self, event_ref) -> None:
            super().cancel_event(event_ref)
            self._save()

    return PersistentSimCalendar()


def _uc05_welcome_source() -> str:
    """UC05_WELCOME_CONFIG_SOURCE for this worker: "" / local (UC05's own repository: in its mock mode only its
    synthetic clients have a welcome template) or supabase (client_configs.settings_payload.uc05_welcome, read-only --
    exactly what UC05's own Supabase mode reads). supabase with UC05_DATABASE_MODE=supabase is refused (redundant)."""
    source = (os.environ.get("UC05_WELCOME_CONFIG_SOURCE") or "").strip().lower()
    if source not in ("", "local", "supabase"):
        raise SystemExit(f"UC05_WELCOME_CONFIG_SOURCE={source!r} is not a known source (local or supabase)")
    if source == "supabase" and (os.environ.get("UC05_DATABASE_MODE") or "").strip().lower() == "supabase":
        raise SystemExit("UC05_WELCOME_CONFIG_SOURCE=supabase is for UC05's local mode; in UC05_DATABASE_MODE=supabase "
                         "UC05 already reads client_configs")
    return source


def _uc05_supabase_client():
    """One read-only PostgREST client for the worker's Supabase-sourced reads (buddies, welcome configuration), from
    the worker's own Supabase settings; pinged so a misconfiguration fails at startup, never at first use."""
    from sub_agent.db.config import load_supabase_settings  # type: ignore
    from sub_agent.db.postgrest import PostgrestClient  # type: ignore

    settings = load_supabase_settings()
    client = PostgrestClient(settings.url, settings.service_role_key, schema=settings.schema,
                             timeout=settings.timeout_s, read_retries=settings.read_retries)
    client.ping()
    return client


def _uc05_buddy_source() -> str:
    """UC05_BUDDY_SOURCE for this worker: "" / local (UC05's own repository: in its mock mode only synthetic test
    buddies) or supabase (the shared buddy_profiles table, read-only). supabase together with UC05's own Supabase mode
    is refused: there UC05 already reads buddy_profiles itself."""
    source = (os.environ.get("UC05_BUDDY_SOURCE") or "").strip().lower()
    if source not in ("", "local", "supabase"):
        raise SystemExit(f"UC05_BUDDY_SOURCE={source!r} is not a known buddy source (local or supabase)")
    if source == "supabase" and (os.environ.get("UC05_DATABASE_MODE") or "").strip().lower() == "supabase":
        raise SystemExit("UC05_BUDDY_SOURCE=supabase is for UC05's local mode; in UC05_DATABASE_MODE=supabase UC05 "
                         "already reads buddy_profiles")
    return source


_BUDDY_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_PLACEHOLDER = __import__("re").compile(r"[<>{}\[\]]|\b(placeholder|dummy|test buddy|sample|lorem|tbd|todo|xxx|"
                                        r"colleague (employee number|full name)|employee number|full name)\b", 2)


def valid_buddy_profile(profile: dict) -> bool:
    """Whether a buddy profile holds real identifying values: an employee id of identifier shape, and a name that is
    not empty and not template text ("<colleague full name>", "placeholder", "dummy", ...). An invalid profile is never
    offered as a buddy: it is not replaced or repaired, only skipped (HR corrects the record)."""
    employee_id = str(profile.get("employee_id") or "")
    name = str(profile.get("full_name") or "").strip()
    return bool(_BUDDY_ID.fullmatch(employee_id) and name and not _PLACEHOLDER.search(name)
                and not _PLACEHOLDER.search(employee_id))


class _SupabaseBuddyPool:
    """UC05_BUDDY_SOURCE=supabase: UC05's buddy candidates for a client, read from the shared Supabase buddy_profiles
    table -- the HR-maintained buddy directory UC05's own Supabase mode reads -- while UC05 otherwise runs locally.

    Read-only: it never writes buddy_profiles or buddy_assignments. The rule is UC05's own
    (SupabaseOnboardingRepository.list_buddy_candidates): this client's active profiles, `team` = the profile's department (matched against the
    joiner's department), and a profile at its max_active_buddies is not offered. Its load counts confirmed
    assignments in buddy_assignments AND the buddies this UC05 assigned locally. An empty result is UC05's
    NoBuddyCandidatesError, never a fallback to synthetic buddies."""

    def __init__(self, local_repository, client=None) -> None:
        if client is None:
            from sub_agent.db.config import load_supabase_settings  # type: ignore
            from sub_agent.db.postgrest import PostgrestClient  # type: ignore

            settings = load_supabase_settings()  # names every missing/invalid variable at once
            client = PostgrestClient(settings.url, settings.service_role_key, schema=settings.schema,
                                     timeout=settings.timeout_s, read_retries=settings.read_retries)
            client.ping()  # fail at startup, never at the first proposal
        client.select("buddy_profiles", columns="buddy_id,client_id,employee_id,full_name,department,"
                                                "max_active_buddies,is_active", limit=0)  # the columns exist
        self._client, self._local = client, local_repository
        self.ignored: list[str] = []  # buddy_ids of profiles skipped by the last listing (invalid values)

    def profile(self, client_id: str, employee_id: str) -> dict | None:
        """One buddy's profile for display (name, department, location), whatever its capacity: the row as stored,
        with `valid` saying whether it holds real identifying values."""
        rows = self._client.select("buddy_profiles", columns="buddy_id,employee_id,full_name,department,location,"
                                   "is_active", filters=[("client_id", f"eq.{client_id}"),
                                                         ("employee_id", f"eq.{employee_id}")], limit=1)
        if not rows:
            return None
        row = rows[0]
        return {"employee_id": row.get("employee_id"), "full_name": row.get("full_name"),
                "department": row.get("department"), "location": row.get("location"),
                "is_active": row.get("is_active"), "valid": valid_buddy_profile(row), "source": "buddy_profiles"}

    def list_buddy_candidates(self, client_id: str):
        from sub_agent.db import mapping as m  # type: ignore
        from sub_agent.db.postgrest import in_list  # type: ignore
        from sub_agent.models import BuddyCandidate  # type: ignore

        if not client_id:
            raise ValueError("buddy candidates are listed per client")
        rows = self._client.select(
            "buddy_profiles", columns="buddy_id,employee_id,full_name,department,max_active_buddies",
            filters=[("client_id", f"eq.{client_id}"), ("is_active", "is.true")], order="employee_id.asc")
        profiles = [p for p in rows if valid_buddy_profile(p)]
        self.ignored = [str(p.get("buddy_id") or "?") for p in rows if not valid_buddy_profile(p)]
        load: dict[str, int] = {}
        for row in self._client.select("buddy_assignments", columns="buddy_employee_id", filters=[
                ("client_id", f"eq.{client_id}"), ("status", in_list(m.BUDDY_ACTIVE_STATUSES))]):
            load[row["buddy_employee_id"]] = load.get(row["buddy_employee_id"], 0) + 1
        for record in self._local.list_onboarding_records(client_id=client_id):
            if record.buddy_id:
                load[record.buddy_id] = load.get(record.buddy_id, 0) + 1
        return [BuddyCandidate(employee_id=p["employee_id"], full_name=p.get("full_name") or "", client_id=client_id,
                               team=p.get("department") or "", active=True)
                for p in profiles
                if p.get("max_active_buddies") is None or load.get(p["employee_id"], 0) < int(p["max_active_buddies"])]


class _CaseOfferSource:
    """UC05's EmailPort + EmployeeDirectoryPort for UC05_OFFER_EVENT_SOURCE=orchestrator_case: the offer-acceptance
    event IS the orchestrator's onboarding case (as in UC05's Supabase mode), and the employee's directory record is
    what the HR-approved onboarding form states, registered by the orchestrator (op register_case_offer_event)
    before any UC05 call for that case. It knows only registered cases: UC05's synthetic mailbox and directory are
    not used, so no synthetic employee can be resolved for a real case. A value the form did not give stays empty,
    and UC05's own intake then refuses the case as incomplete (IncompleteEmployeeDataError) -- nothing is filled in.

    One registered case per (client, employee id): UC05 keeps one onboarding record per employee, so a second case
    under the same id is refused (employee_id_conflict) instead of being merged into the first."""

    FIELDS = ("case_id", "client_id", "employee_id", "full_name", "email", "designation", "department", "team",
              "team_lead_id", "joining_date")

    def __init__(self) -> None:
        self._cases: dict[str, dict] = {}  # event id (= case id) -> registered facts
        self._by_employee: dict[tuple[str, str], str] = {}  # (client, employee id) -> case id
        self._processed: set[str] = set()

    def register(self, facts: dict, *, client_id: str) -> dict:
        from datetime import date, datetime, timezone

        case = {k: str(facts.get(k) or "").strip() for k in self.FIELDS}
        if case["client_id"] != client_id:
            raise PermissionError("a case can be registered only under the caller's own client")
        if not case["case_id"] or not case["employee_id"]:
            raise ValueError("register_case_offer_event needs case_id and employee_id")
        date.fromisoformat(case["joining_date"])  # ValueError: refused, never guessed
        key = (case["client_id"], case["employee_id"])
        owner = self._by_employee.get(key)
        if owner is not None and owner != case["case_id"]:
            return {"status": "employee_id_conflict", "case_id": owner}
        previous = self._cases.get(case["case_id"])
        if previous is not None and (previous["client_id"], previous["employee_id"]) != key:
            return {"status": "case_identity_changed", "case_id": case["case_id"]}
        case["registered_at"] = (previous or {}).get("registered_at") or datetime.now(timezone.utc).isoformat()
        self._cases[case["case_id"]], self._by_employee[key] = case, case["case_id"]
        return {"status": "registered", "case_id": case["case_id"],
                "missing_fields": [k for k in ("email", "department", "team", "team_lead_id") if not case[k]]}

    # EmailPort
    def get_event(self, event_id: str):
        from datetime import datetime

        from connectors.email.base import OfferAcceptanceEvent  # type: ignore

        case = self._cases.get(event_id)
        if case is None:
            raise KeyError(event_id)
        return OfferAcceptanceEvent(event_id=event_id, employee_id=case["employee_id"],
                                    received_at=datetime.fromisoformat(case["registered_at"]),
                                    raw_subject="", raw_body="")

    def is_processed(self, event_id: str) -> bool:
        return event_id in self._processed

    def mark_processed(self, event_id: str) -> None:
        self._processed.add(event_id)

    # EmployeeDirectoryPort
    def get_employee(self, employee_id: str, *, client_id: str | None = None):
        from datetime import date

        from connectors.employee_directory.base import EmployeeDirectoryRecord  # type: ignore

        matches = [c for c in self._cases.values() if c["employee_id"] == employee_id
                   and (client_id is None or c["client_id"] == client_id)]
        if len(matches) != 1:
            raise KeyError(employee_id)  # unknown, or another tenant's
        c = matches[0]
        return EmployeeDirectoryRecord(employee_id=c["employee_id"], full_name=c["full_name"], email=c["email"],
                                       designation=c["designation"], department=c["department"], team=c["team"],
                                       team_lead_id=c["team_lead_id"], client_id=c["client_id"],
                                       joining_date=date.fromisoformat(c["joining_date"]))


class _Uc05Handler:
    """UC05 (`employee-onboarding`) behind its own public entry points -- nothing here contains
    UC05 business rules; every op is a thin call into UC05 code:

      run_event              OnboardingAgent.run_event (offer_accepted, joining_day, draft_welcome,
                             policy_question, participant_responded, ...)
      preflight_offer_event  read-only: UC05's EmailPort + EmployeeDirectoryPort (tenant-filtered)
                             and intake.REQUIRED_DIRECTORY_FIELDS -- who the offer event resolves
                             to, before anything is written, so the orchestrator can cross-check
                             identity and joining date first
      case_snapshot          read-only: OnboardingAgent.case_status + OnboardingToolRegistry reads,
                             projected to ids/statuses (no names, addresses or message bodies)
      decide_approval        OnboardingToolRegistry.validate_decision FIRST (the specific team lead/
                             buddy, applicability, edited payload -- nothing is recorded if it
                             refuses), then HITLInbox.decide with the approver's own identity; UC05's
                             listener (OnboardingAgent.handle_approval_decision, subscribed exactly
                             as UC05's api.on_decision does) applies it and continues UC05's workflow
      retry_apply            HITLInbox.retry_apply, held to UC05's api._authorize_retry_apply rule
      propose_buddy          OnboardingToolRegistry.propose_buddy_assignment (+ notify_approver)
      schedule_session       OnboardingToolRegistry.schedule_session / schedule_buddy_intro
                             (+ notify_approver) -- an HR-chosen start, UC05's HR-console operation
      resolve_operation      OnboardingToolRegistry.resolve_operation (hr_admin, enforced by UC05)
      redeliver_welcome      OnboardingToolRegistry.redeliver_welcome (hr_admin; only a welcome known not
                             to have been delivered)
      __test_hooks__         TEST ONLY, refused unless HR_ORCH_WORKER_TEST_HOOKS=1; touches only
                             UC05's simulated connectors (MockCalendar/MockNotifier/repository fakes)

    Wiring: UC05's own sub_agent.runtime.build_context -- the exact assembly its API and joining-day
    scheduler use: the LLM that LLM_MODE selects (offline: UC05's offline LLM with its welcome-note and
    policy-Q&A handlers; online: hr_onboarding_common's LiteLLMGateway, validated at startup), its
    simulated connectors, and with UC05_WORKER_DATA_DIR set, its SQLite repository + operation ledger,
    SqliteSaver checkpoints and persistent HITL inbox (HR_AGENT_PERSIST=1 in that directory).
    """

    AGENT_ID = "employee-onboarding"

    def __init__(self, agent_home: Path) -> None:
        self._agent_home = agent_home
        self._build()

    def _build(self) -> None:
        # Configuration for UC05's runtime, scoped to this process. Worker-specific variable names,
        # so the orchestrator's own HR_AGENT_DATA_DIR (its own hitl.db/audit) is never shared with UC05.
        data_dir = os.environ.get("UC05_WORKER_DATA_DIR")
        os.environ["HR_AGENT_ENV"] = os.environ.get("UC05_WORKER_HR_AGENT_ENV", "development")
        # The orchestrator's LLM_MODE (inherited), unless UC05_WORKER_LLM_MODE overrides it for this worker.
        os.environ["LLM_MODE"] = os.environ.get("UC05_WORKER_LLM_MODE") or os.environ.get("LLM_MODE", "offline")
        os.environ["CONNECTOR_MODE"] = "offline"  # the only mode UC05 implements
        os.environ["HR_AGENT_PERSIST"] = "1" if data_dir else "0"
        if data_dir:
            os.environ["HR_AGENT_DATA_DIR"] = data_dir
        # The orchestrator registers its own joiners with UC05: UC05's development seed loads only its client
        # configurations here, never its sample employees and buddies (UC05_WORKER_ENV__UC05_SEED_SAMPLE_PEOPLE=1 restores them).
        os.environ.setdefault("UC05_SEED_SAMPLE_PEOPLE", "0")

        from sub_agent.agent import OnboardingAgent  # type: ignore
        from sub_agent.runtime import build_context  # type: ignore

        source = _uc05_offer_event_source()
        self._buddy_source = _uc05_buddy_source() or "local"
        ctx, _jobs = build_context()
        if data_dir and type(ctx.connector.calendar).__name__ == "MockCalendar":
            # The simulated calendar persists with UC05's records (same data directory), restoring missing holds.
            calendar = _persistent_sim_calendar(Path(data_dir) / "simulated_calendar.json", ctx.connector.repository)
            ctx.connector.calendar = calendar
            if calendar.restored:
                print(f"uc05 worker: restored {len(calendar.restored)} simulated calendar hold(s) from UC05's session "
                      f"records: {', '.join(calendar.restored)}", file=sys.stderr)
        self._welcome_source = _uc05_welcome_source() or "local"
        self._buddy_pool = None
        shared = (_uc05_supabase_client() if "supabase" in (self._buddy_source, self._welcome_source) else None)
        if self._buddy_source == "supabase":
            # Buddies from the shared buddy_profiles table instead of UC05's local (synthetic) pool: only this one
            # read of the repository is replaced, on this instance; everything else stays UC05's own.
            pool = _SupabaseBuddyPool(ctx.connector.repository, client=shared)
            ctx.connector.repository.list_buddy_candidates = pool.list_buddy_candidates
            self._buddy_pool = pool
        if self._welcome_source == "supabase":
            # The client's welcome configuration (languages, branding, templates) as UC05's own Supabase mode reads it:
            # client_configs.settings_payload.uc05_welcome, read-only. Nothing is defaulted (MissingClientConfigError).
            from sub_agent.db.repository import SupabaseOnboardingRepository  # type: ignore

            ctx.connector.repository.get_client_config = SupabaseOnboardingRepository(shared).get_client_config
        self._case_source = None
        if source == "orchestrator_case":
            # The case-backed source replaces UC05's (synthetic) mailbox and directory BEFORE the agent is built.
            self._case_source = _CaseOfferSource()
            ctx.connector.email = ctx.connector.directory = self._case_source
        agent = OnboardingAgent(ctx)
        ctx.hitl.subscribe(agent.handle_approval_decision)  # exactly what UC05's api.on_decision does
        self._ctx, self._agent = ctx, agent

    def llm_status(self) -> dict:
        """Which LLM UC05 was built with (the gateway or the offline LLM) and its tier models; for the
        gateway also the provider, LiteLLM routes and reasoning settings (llm_summary: no credential)."""
        from hr_onboarding_common.llm import LiteLLMGateway, ModelTier, llm_summary  # type: ignore

        llm = getattr(self._ctx.llm, "inner", self._ctx.llm)  # UC05 wraps it in SanitizedLLM
        status = {"implementation": type(llm).__name__,
                  "models": {tier.value: llm.model_for(tier) for tier in ModelTier}}
        if isinstance(llm, LiteLLMGateway):
            status.update({k: v for k, v in llm_summary(llm.settings).items() if k != "models"})
        return status

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _actor(actor: dict):
        from hr_onboarding_common.contracts import Actor as CommonActor  # type: ignore

        return CommonActor(on_behalf_of=actor["on_behalf_of"], roles=actor.get("roles", []),
                           client_id=actor.get("client_id"))

    def _repo_for(self, client_id: str | None):
        """UC05's repository for one verified tenant: its Supabase repository answers only through a tenant-bound
        view (for_tenant); the local SQLite repository filters per call and is returned as it is."""
        repo = self._agent.deps.repository
        for_tenant = getattr(repo, "for_tenant", None)
        if callable(for_tenant):
            if not client_id:
                raise PermissionError("actor has no verified client_id")
            return for_tenant(client_id)
        return repo

    def _registry(self, actor):
        from sub_agent.tool_registry import OnboardingToolRegistry  # type: ignore

        return OnboardingToolRegistry.from_dependencies(self._agent.deps, llm=self._ctx.llm, policy=self._agent.policy,
                                                        hitl=self._ctx.hitl, audit=self._ctx.audit, actor=actor)

    def _item_view(self, item) -> dict:
        """An approval as ids/statuses. The payload's own id (session/draft) and, for a buddy
        proposal, the proposed buddy's employee id -- never a draft's subject/body."""
        payload = item.payload or {}
        view = {"approval_id": item.id, "gate": item.gate, "proposal_type": item.proposal_type,
                "status": str(item.status), "approver_role": item.approver_role, "subject_id": item.subject_id,
                "created_at": item.created_at.isoformat(),
                "decided_at": item.decided_at.isoformat() if item.decided_at else None,
                "apply_error": item.apply_error.split(":", 1)[0] if item.apply_error else None,
                "apply_error_detail": item.apply_error[:300] if item.apply_error else None}
        if item.proposal_type in ("session_booking", "welcome_message"):
            view["record_id"] = payload.get("id")
            view["session_type"] = payload.get("session_type")
        elif item.proposal_type == "buddy_assignment":
            final = item.final_payload or {}
            view["buddy_id"] = final.get("buddy_id", payload.get("buddy_id"))
        elif item.proposal_type == "attendee_declined":
            view["record_id"] = payload.get("session_id")
            view["session_type"] = payload.get("session_type")
        return view

    def _decision_run(self, item) -> dict:
        """The checkpointed outcome of the UC05 workflow run that applied this decision."""
        from sub_agent.agent import WorkflowEvent, workflow_id_for  # type: ignore

        state = self._agent.workflow_state(workflow_id_for(WorkflowEvent.APPROVAL_DECIDED, item.client_id, item.id))
        keep = ("stage", "meeting_session_id", "meeting_type", "meeting_status", "buddy_id", "buddy_status",
                "team_intro_status", "welcome_draft_id", "welcome_status", "conflict_decision", "pending_nodes")
        run = {k: state.get(k) for k in keep if state.get(k) not in (None, "")}
        run["errors"] = [{"stage": e.get("stage"), "error": e.get("error"), "detail": (e.get("detail") or "")[:300]}
                         for e in state.get("errors") or []]
        return run

    def _owned_item(self, actor, approval_id: str, expected_subject_id: str):
        item = self._ctx.hitl.get(approval_id, client_id=actor.client_id)  # KeyError: missing OR another tenant
        if item.agent != self.AGENT_ID or item.subject_id != expected_subject_id:
            raise KeyError(approval_id)  # not this case's approval: reported exactly like "not found"
        return item

    def _notify(self, reg, item) -> bool | None:
        """UC05's request_hr_approval step for an item created outside its graph: tell the approver
        over the configured channel. A failed notice never undoes the item (it stays decidable)."""
        if item is None or str(item.status) != "pending":
            return None
        try:
            reg.notify_approver(item)
            return True
        except Exception as exc:  # noqa: BLE001 -- NotificationRejectedError/NotificationDeliveryError
            print(f"uc05 worker: approver notice for {item.id} not delivered: {_short(exc)}", file=sys.stderr)
            return False

    # ------------------------------------------------------------------ ops
    def dispatch(self, op: str, payload: dict, *, run_id: str, idempotency_key: str, actor: dict) -> dict:
        handler = {
            "run_event": self._run_event, "preflight_offer_event": self._preflight_offer_event,
            "register_case_offer_event": self._register_case_offer_event,
            "welcome_message": self._welcome_message,
            "case_snapshot": self._case_snapshot, "decide_approval": self._decide_approval,
            "retry_apply": self._retry_apply, "propose_buddy": self._propose_buddy,
            "schedule_session": self._schedule_session, "resolve_operation": self._resolve_operation,
            "redeliver_welcome": self._redeliver_welcome,
            "__test_hooks__": self._test_hooks,
        }.get(op)
        if handler is None:
            raise ValueError(f"uc05: unknown op '{op}'")
        return handler(payload, self._actor(actor))

    def _run_event(self, payload: dict, actor) -> dict:
        from sub_agent.agent import OnboardingWorkflowInput  # type: ignore

        event_input = OnboardingWorkflowInput.model_validate(payload["event"])
        response, workflow_summary = self._agent.run_event(event_input, actor=actor, instance=payload["instance"])
        return {"response": response.model_dump(mode="json"), "workflow": workflow_summary}

    def _welcome_message(self, payload: dict, actor) -> dict:
        """Read-only: the employee's welcome message. The newest welcome draft UC05 wrote (on the joining day), else a
        PREVIEW rendered the way UC05's draft_welcome_communication composes it -- the client's approved "welcome"
        template with the facts filled in by code, the confirmed session, the buddy -- without the short personal note
        UC05's model adds only when it drafts. Nothing is drafted, submitted for approval or sent."""
        from sub_agent.models import (InvalidClientConfigError, MissingClientConfigError,  # type: ignore
                                      UnsupportedLanguageError)
        from sub_agent.services import welcome  # type: ignore

        if actor.client_id is None:
            raise PermissionError("actor has no verified client_id")
        repo = self._repo_for(actor.client_id)
        try:
            record = repo.get_onboarding_record(payload["employee_id"])
        except KeyError:
            return {"status": "no_uc05_case"}
        if record.client_id != actor.client_id:
            return {"status": "no_uc05_case"}  # another tenant's employee is reported exactly like an unknown one
        drafts = [d for d in self._registry(actor).list_communication_drafts(record.employee_id)
                  if d.template_id == "welcome"]
        if drafts:
            draft = max(drafts, key=lambda d: d.created_at)
            return {"status": "draft", "draft_id": draft.id, "draft_status": str(draft.status),
                    "language": draft.language, "subject": draft.subject, "body": draft.body}
        try:
            client = welcome.client_config_for(repo, record.client_id)
            template = welcome.select_template(client, template_id="welcome", language=client.default_language)
        except (MissingClientConfigError, InvalidClientConfigError, UnsupportedLanguageError) as exc:
            return {"status": "no_welcome_configuration", "detail": str(exc)[:300], "source": self._welcome_source}
        facts = {"full_name": record.full_name, "role_title": record.role_title,
                 "start_date": record.start_date.isoformat(), "sender_name": client.branding.sender_name}
        body = template.body.format(**facts)
        session_line = welcome._confirmed_session_line(repo, record.employee_id)  # noqa: SLF001 -- UC05's own wording
        if session_line:
            body += "\n\n" + session_line
        if record.buddy_id:
            body += f"\n\nYour onboarding buddy is {record.buddy_id}."
        return {"status": "preview", "language": template.language, "subject": template.subject.format(**facts),
                "body": body, "personal_note": "added by UC05 when it drafts the welcome on the joining day"}

    def _register_case_offer_event(self, payload: dict, actor) -> dict:
        """UC05_OFFER_EVENT_SOURCE=orchestrator_case only: the case's directory facts from the HR-approved form."""
        if self._case_source is None:
            raise _Refused("CASE_SOURCE_NOT_ENABLED", "this UC05 worker resolves offer events from its mailbox; "
                                                      "UC05_OFFER_EVENT_SOURCE=orchestrator_case is not set")
        if actor.client_id is None:
            raise PermissionError("actor has no verified client_id")
        return self._case_source.register(payload["case"], client_id=actor.client_id)

    def _preflight_offer_event(self, payload: dict, actor) -> dict:
        from sub_agent.services.intake import REQUIRED_DIRECTORY_FIELDS  # type: ignore

        if actor.client_id is None:
            raise PermissionError("actor has no verified client_id")
        deps = self._agent.deps
        if deps.email is None or deps.directory is None:
            raise _Refused("CONNECTOR_NOT_CONFIGURED", "UC05 has no email or employee-directory connector configured")
        event_id = payload["event_id"]
        try:
            event = deps.email.get_event(event_id)  # EmailServiceError propagates (retryable)
        except KeyError:
            return {"status": "event_not_found", "event_id": event_id}
        if event.employee_id is None:
            return {"status": "malformed_event", "event_id": event_id}
        try:
            # tenant-filtered: another client's employee is reported exactly like an unknown one
            record = deps.directory.get_employee(event.employee_id, client_id=actor.client_id)
        except KeyError:
            return {"status": "employee_not_in_directory", "event_id": event_id}
        missing = [f for f in REQUIRED_DIRECTORY_FIELDS if not getattr(record, f)]
        return {"status": "incomplete_employee_data" if missing else "ok", "event_id": event_id,
                "employee_id": record.employee_id, "joining_date": record.joining_date.isoformat(),
                "missing_fields": missing, "already_processed": deps.email.is_processed(event_id)}

    def _session_view(self, s, client_id: str | None = None) -> dict:
        """A session as UC05 holds it: times, attendees (ids), calendar reference, each attendee's invitation status
        and newest reply (RSVP), the meeting link and room UC05 holds (None when it holds none -- never invented), and
        in UC05's Supabase mode the stored session_participants rows (participant id, role, RSVP). Ids and statuses
        only -- no names, no e-mail addresses, no message content."""
        repo = self._repo_for(client_id) if client_id else self._agent.deps.repository
        invitations = {i.participant_id: str(i.status) for i in repo.list_invitations(s.id)}
        replies: dict[str, dict] = {}
        for r in sorted(repo.list_participant_responses(s.id), key=lambda r: r.responded_at):
            replies[r.participant_id] = {"response": str(r.response), "responded_at": r.responded_at.isoformat()}
        return {"session_id": s.id, "session_type": str(s.session_type), "status": str(s.status),
                "scheduled_start": s.scheduled_start.isoformat(), "scheduled_end": s.scheduled_end.isoformat(),
                "attendees": list(s.attendees), "calendar_event_id": s.external_event_ref,
                "booking_operation_key": s.booking_operation_key,
                "invitations": [{"participant_id": p, "status": st} for p, st in invitations.items()],
                "responses": [{"participant_id": p, **v} for p, v in replies.items()],
                "meeting_link": getattr(s, "meeting_link", None), "room_location": getattr(s, "room_location", None),
                "stored_participants": self._stored_participants(repo, s.id)}

    @staticmethod
    def _stored_participants(repo, session_id: str) -> list[dict] | None:
        """UC05's Supabase session_participants rows for the session (None where the repository keeps none)."""
        lister = getattr(repo, "list_session_participants", None)
        if not callable(lister):
            return None
        return [{"participant_id": r.get("actor_id"), "role": r.get("role"), "rsvp_status": r.get("rsvp_status"),
                 "responded_at": r.get("responded_at")} for r in lister(session_id)]

    def _buddy_profiles(self, record, items) -> dict:
        """The profile of the assigned buddy and of any proposed one, from the buddy source in use (Supabase
        buddy_profiles, or UC05's own candidates): name, department, location and whether the values are valid."""
        ids = {record.buddy_id, *((i.payload or {}).get("buddy_id") for i in items
                                  if i.proposal_type == "buddy_assignment")} - {None}
        out = {}
        for employee_id in ids:
            if self._buddy_pool is not None:
                profile = self._buddy_pool.profile(record.client_id, employee_id)
            else:
                match = next((c for c in self._agent.deps.repository.list_buddy_candidates(record.client_id)
                              if c.employee_id == employee_id), None)
                profile = None if match is None else {
                    "employee_id": match.employee_id, "full_name": match.full_name, "department": match.team,
                    "location": None, "is_active": match.active, "source": "uc05_candidates",
                    "valid": valid_buddy_profile({"employee_id": match.employee_id, "full_name": match.full_name})}
            out[employee_id] = profile or {"employee_id": employee_id, "valid": valid_buddy_profile(
                {"employee_id": employee_id, "full_name": "unknown"}), "source": None}
        return out

    def _case_snapshot(self, payload: dict, actor) -> dict:
        employee_id = payload["employee_id"]
        reg = self._registry(actor)
        try:
            record = reg.get_record(employee_id)  # TenantAuthorizationError (POLICY_DENIED) for another tenant
        except KeyError:
            return {"found": False, "employee_id": employee_id}
        status = self._agent.case_status(actor, employee_id)
        sessions = reg.list_sessions(employee_id)
        items = [i for i in self._ctx.hitl.list(status=None, client_id=actor.client_id)
                 if i.agent == self.AGENT_ID and i.subject_id == employee_id]
        drafts = [d for d in reg.list_communication_drafts(employee_id) if d.template_id == "welcome"]
        # The operation ledger, tenant-scoped, narrowed to this employee's keys: calendar bookings
        # name the employee, messages name the draft or approval they deliver.
        own_ids = {employee_id, *(d.id for d in drafts), *(i.id for i in items)}
        operations = [op for op in self._agent.deps.repository.list_operations(client_id=actor.client_id)
                      if any(f":{x}:" in f"{op.key}:" for x in own_ids)]
        return {
            "found": True, "employee_id": employee_id, "client_id": record.client_id,
            "case_status": str(record.case_status), "start_date": record.start_date.isoformat(),
            "buddy_id": record.buddy_id, "team_lead_id": record.team_lead_id, "stage": str(status["stage"]),
            "sessions": [self._session_view(s, actor.client_id) for s in sessions],
            "buddy_profiles": self._buddy_profiles(record, items),
            "approvals": [self._item_view(i) for i in items],
            "superseded_approvals": status.get("superseded_approvals", []),
            "welcome_drafts": [{"draft_id": d.id, "status": str(d.status), "language": d.language,
                                "created_at": d.created_at.isoformat()} for d in drafts],
            "operations": [{"key": op.key, "kind": op.kind, "status": str(op.status), "last_error": op.last_error,
                            "attempts": op.attempts, "resolved_by": op.detail.get("resolved_by"),
                            "resolution_reason": op.detail.get("resolution_reason"),
                            "simulated": op.detail.get("simulated")} for op in operations],
        }

    def _decide_approval(self, payload: dict, actor) -> dict:
        from hr_onboarding_common.platform.hitl import Decision, HITLApplyError, HITLError, ReasonCode  # type: ignore
        from sub_agent.services.decisions import DecisionNotApplicableError  # type: ignore
        from sub_agent.tool_registry import TenantAuthorizationError  # type: ignore

        item = self._owned_item(actor, payload["approval_id"], payload["expected_subject_id"])
        expected_type, expected_record = payload.get("expected_proposal_type"), payload.get("expected_record_id")
        if (expected_type and item.proposal_type != expected_type) or (
                expected_record and self._item_view(item).get("record_id") != expected_record):
            # the case knows this approval as a different activity: decide nothing
            raise _Refused("CONFLICT", f"approval {item.id} is not the {expected_type or 'expected'} approval this "
                                       f"case is waiting for")
        decision = Decision(payload["decision"])  # ValueError (INVALID_INPUT) before anything is recorded
        reason = payload.get("reason_code")
        try:
            # UC05's own rules, checked BEFORE the decision is recorded: a refusal leaves it pending
            self._registry(actor).validate_decision(item, decision=decision, approver=actor.on_behalf_of,
                                                    edited_payload=payload.get("edited_payload"))
        except DecisionNotApplicableError as e:
            raise _Refused("CONFLICT", str(e)) from None
        except TenantAuthorizationError:
            raise KeyError(item.id) from None
        applied, apply_error = True, None
        try:
            item = self._ctx.hitl.decide(item.id, decision, approver=actor.on_behalf_of,
                                         approver_roles=list(actor.roles), client_id=actor.client_id,
                                         reason_code=ReasonCode(reason) if reason else None, note=payload.get("note"),
                                         edited_payload=payload.get("edited_payload"))
        except HITLApplyError as e:  # the human decision is recorded; applying it failed (UC05 finding S5)
            item, applied, apply_error = e.item, False, type(e.cause).__name__
        except HITLError as e:
            message = str(e)
            if "already decided" in message:
                raise _Refused("CONFLICT", message) from None
            if "lacks role" in message:
                raise
            raise _Refused("INVALID_INPUT", message) from None
        return {"approval": self._item_view(item), "applied": applied, "apply_error_type": apply_error,
                "run": self._decision_run(item)}

    def _retry_apply(self, payload: dict, actor) -> dict:
        from hr_onboarding_common.platform.hitl import HITLApplyError, HITLError  # type: ignore

        item = self._owned_item(actor, payload["approval_id"], payload["expected_subject_id"])
        # Parity with UC05's api._authorize_retry_apply: a retry re-runs the decision's side effects
        # under the original approver's authority, so it needs that role or hr_admin.
        if item.approver_role not in actor.roles and "hr_admin" not in actor.roles:
            raise PermissionError(f"retrying approval {item.id} requires role '{item.approver_role}' or 'hr_admin'")
        applied, apply_error = True, None
        try:
            item = self._ctx.hitl.retry_apply(item.id, client_id=actor.client_id)
        except HITLApplyError as e:
            item, applied, apply_error = e.item, False, type(e.cause).__name__
        except HITLError as e:
            raise _Refused("CONFLICT", str(e)) from None
        return {"approval": self._item_view(item), "applied": applied, "apply_error_type": apply_error,
                "run": self._decision_run(item)}

    def _propose_buddy(self, payload: dict, actor) -> dict:
        from sub_agent.services.scheduling import NoBuddyCandidatesError  # type: ignore

        employee_id = payload["employee_id"]
        reg = self._registry(actor)
        pending_before = {i.id for i in reg.pending_approvals(employee_id, proposal_type="buddy_assignment")}
        try:
            buddy_id, item = reg.propose_buddy_assignment(employee_id, workflow_instance=employee_id)
        except NoBuddyCandidatesError as e:
            ignored = list(getattr(self._buddy_pool, "ignored", []) or [])
            detail = str(e)[:300] + (f" ({len(ignored)} profile(s) ignored: placeholder or invalid values)"
                                     if ignored else "")
            return {"outcome": "no_candidates", "detail": detail, "buddy_source": self._buddy_source,
                    "ignored_profiles": len(ignored)}
        if item is None:
            return {"outcome": "already_assigned", "buddy_id": buddy_id}
        outcome = "already_pending" if item.id in pending_before else "proposed"
        notified = self._notify(reg, item) if outcome == "proposed" else None
        return {"outcome": outcome, "buddy_id": buddy_id, "approval_id": item.id, "approver_notified": notified}

    def _schedule_session(self, payload: dict, actor) -> dict:
        from datetime import datetime

        from sub_agent.models import SessionType  # type: ignore

        employee_id, session_type = payload["employee_id"], SessionType(payload["session_type"])
        start = datetime.fromisoformat(payload["start"])
        reg = self._registry(actor)
        business = ("CalendarConflictError", "InvalidMeetingTimeError", "DuplicateSessionError", "BuddyNotAssignedError")
        try:
            if session_type == SessionType.BUDDY_INTRO:
                session, item = reg.schedule_buddy_intro(employee_id, start=start, workflow_instance=employee_id)
            else:
                attendees = reg.participants_for(employee_id, session_type)  # UC05's configured participants
                session, item = reg.schedule_session(employee_id, session_type=session_type, start=start,
                                                     attendees=attendees, workflow_instance=employee_id)
        except Exception as e:  # noqa: BLE001 -- business outcomes are results; anything else is classified
            name = type(e).__name__
            if name in business:
                return {"outcome": "rejected", "error_type": name, "detail": str(e)[:300]}
            if name == "BookingOutcomeUncertainError":
                return {"outcome": "uncertain", "error_type": name, "detail": str(e)[:300]}
            raise
        return {"outcome": "scheduled", "session_id": session.id, "session_type": str(session.session_type),
                "status": str(session.status), "approval_id": item.id, "approver_role": item.approver_role,
                "approver_notified": self._notify(reg, item)}

    def _resolve_operation(self, payload: dict, actor) -> dict:
        from sub_agent.models import OperationStatus  # type: ignore

        try:
            record = self._registry(actor).resolve_operation(payload["key"], outcome=OperationStatus(payload["outcome"]),
                                                             provider_ref=payload.get("provider_ref"),
                                                             reason=payload.get("reason"))
        except ValueError as e:
            raise _Refused("CONFLICT", str(e)) from None
        return {"key": record.key, "status": str(record.status)}

    def _redeliver_welcome(self, payload: dict, actor) -> dict:
        from connectors.notifications.base import NotificationRejectedError  # type: ignore
        from sub_agent.services.delivery import DeliveryOutcomeUncertainError  # type: ignore
        from sub_agent.tool_registry import DuplicateSendError  # type: ignore

        try:
            result = self._registry(actor).redeliver_welcome(payload["draft_id"], reason=payload.get("reason") or "")
        except NotificationRejectedError as e:  # refused permanently again: nothing delivered
            return {"outcome": "rejected", "error_type": type(e).__name__, "detail": str(e)[:300]}
        except (DuplicateSendError, DeliveryOutcomeUncertainError) as e:
            raise _Refused("CONFLICT", str(e)) from None
        return {"outcome": "sent", "simulated": result.get("simulated")}

    # ------------------------------------------------------------------ test hooks (simulated connectors only)
    def _test_hooks(self, payload: dict, actor) -> dict:
        if os.environ.get("HR_ORCH_WORKER_TEST_HOOKS") != "1":
            raise PermissionError("worker test hooks are disabled (HR_ORCH_WORKER_TEST_HOOKS is not set)")
        deps = self._agent.deps
        calendar, notifier, repo = deps.calendar, deps.notifier, deps.repository
        simulated_calendar = "MockCalendar" in {c.__name__ for c in type(calendar).__mro__}  # incl. the persistent one
        if not simulated_calendar or type(notifier).__name__ != "MockNotifier":
            raise PermissionError("test hooks only operate on UC05's simulated connectors")
        action = payload["action"]
        if action == "reset":
            self._build()
        elif action == "add_buddy_candidate":
            from sub_agent.models import BuddyCandidate  # type: ignore

            candidate = BuddyCandidate(**payload["candidate"])
            if hasattr(repo, "upsert_buddy_candidate"):
                repo.upsert_buddy_candidate(candidate)
            else:
                repo.buddy_candidates.append(candidate)
        elif action == "add_client_config":  # a tenant's welcome templates/branding (UC05's seed has none for TEST-A)
            from sub_agent.models import ClientConfig  # type: ignore

            config = ClientConfig(**payload["config"])
            if hasattr(repo, "upsert_client_config"):
                repo.upsert_client_config(config)
            else:
                repo.client_configs[config.client_id] = config
        elif action == "remove_client_config":  # a tenant with no welcome configuration
            client_id = payload["client_id"]
            if hasattr(repo, "client_configs"):
                repo.client_configs.pop(client_id, None)
            else:
                with repo.db.transaction() as c:
                    c.execute("DELETE FROM client_configs WHERE client_id = ?", (client_id,))
        elif action == "calendar_busy":
            from datetime import datetime

            calendar._busy.append((payload["attendee"], datetime.fromisoformat(payload["start"]),  # noqa: SLF001
                                   datetime.fromisoformat(payload["end"])))
        elif action == "calendar_clear_busy":
            calendar._busy = [b for b in calendar._busy if b[0] != payload["attendee"]]  # noqa: SLF001
        elif action == "set":
            allowed = {"calendar": {"fail_next_bookings", "lose_next_booking_responses", "fail_next_confirms",
                                    "idempotent_booking"},
                       "notifier": {"fail_next", "lose_next_responses", "idempotent_delivery"}}
            target = {"calendar": calendar, "notifier": notifier}[payload["target"]]
            for name, value in payload["attrs"].items():
                if name not in allowed[payload["target"]]:
                    raise ValueError(f"test hook may not set {payload['target']}.{name}")
                setattr(target, name, value)
        elif action == "reject_recipient":
            notifier.reject_recipients.add(payload["recipient_id"])
        elif action == "clear_rejections":
            notifier.reject_recipients.clear()
        elif action == "sweep_timeouts":  # UC05's own timeout sweep: releases holds whose approval expired
            from datetime import datetime

            from sub_agent.services.scheduling import check_approval_timeouts  # type: ignore

            check_approval_timeouts(repo, calendar, self._ctx.hitl, self._ctx.audit,
                                    as_of=datetime.fromisoformat(payload["as_of"]), client_id=actor.client_id)
        elif action == "sleep":
            time.sleep(float(payload["seconds"]))
        elif action != "counts":
            raise ValueError(f"unknown test hook action '{action}'")
        messages: dict[str, int] = {}
        for message in notifier.sent:
            messages[message.kind] = messages.get(message.kind, 0) + 1
        return {"calendar_live": len(calendar._bookings), "calendar_cancelled": len(calendar._cancelled),  # noqa: SLF001
                "calendar_invited": len(calendar.invited), "messages_by_kind": messages}


def _build_handler(uc_id: str, src_dir: Path):
    if uc_id == "uc01":
        return _Uc01Handler()
    if uc_id == "uc04":
        return _Uc04Handler()
    if uc_id == "uc05":
        return _Uc05Handler(agent_home=src_dir.parent)
    raise ValueError(f"unknown uc_id: {uc_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uc", required=True, choices=["uc01", "uc04", "uc05"])
    parser.add_argument("--src-dir", required=True)
    args = parser.parse_args()

    protocol = sys.stdout
    sys.stdout = sys.stderr  # stdout belongs to the protocol; anything else printed goes to stderr

    def emit(message: dict) -> None:
        protocol.write(json.dumps(message, default=str) + "\n")
        protocol.flush()

    uc_id = args.uc
    src_dir = Path(args.src_dir)
    # Some of these UC agents' own code imports a sibling top-level package that lives next to
    # `src/`, not inside it (e.g. UC04's `sub_agent.guardrails` does `from guardrails.guardrails
    # import *`, where `guardrails/` is a repo-root sibling of `src/`). Their own test suites
    # paper over this via `pythonpath=["."]` at the repo root -- this replicates that, without
    # touching any UC source. UC05 needs it too: its top-level `connectors`/`context_layer`
    # packages live at the repo root.
    sys.path.insert(0, str(src_dir.parent))
    sys.path.insert(0, str(src_dir))

    if uc_id == "uc04":
        # UC04's identifier profile (its guardrails/identity.py), chosen by the embedding layer before anything
        # from `sub_agent` is imported -- the contracts fix it on import. Opt-in: unset keeps LOCAL_SIMULATION
        # (synthetic SYN-* identifiers only, GR-13). PLATFORM accepts the shared platform's identifiers (uuid case
        # ids, EMP-nnnnnn employee numbers); enabling it is a shared-contract decision (UC04
        # docs/common_patches/README.md). An unknown name is refused by UC04, never widened.
        profile = os.environ.get("UC04_IDENTITY_PROFILE", "").strip()
        if profile:
            from guardrails import identity as uc04_identity  # type: ignore

            uc04_identity.configure(profile)
            print(f"uc04 worker: identifier profile {uc04_identity.resolve(profile).name}", file=sys.stderr)
        if _uc04_state_mode() == "supabase":  # SystemExit before serving anything when misconfigured
            _uc04_supabase_settings()

    if uc_id == "uc05":
        source = _uc05_offer_event_source()  # SystemExit before serving anything when misconfigured
        buddies = _uc05_buddy_source()
        welcome_source = _uc05_welcome_source()
        if "supabase" in (buddies, welcome_source):
            from sub_agent.db.config import SupabaseConfigurationError, load_supabase_settings  # type: ignore

            try:  # the settings only (no network here); the handler pings Supabase when it is built
                load_supabase_settings()
            except SupabaseConfigurationError as exc:
                raise SystemExit(f"UC05's Supabase-sourced reads (buddies / welcome configuration) need the worker's "
                                 f"Supabase settings "
                                 f"(UC05_WORKER_ENV__SUPABASE_URL / __SUPABASE_SERVICE_ROLE_KEY): {exc}") from None
        print(f"uc05 worker: offer-event source {source or 'mailbox'}, buddy source {buddies or 'local'}, "
              f"welcome configuration {welcome_source or 'local'}", file=sys.stderr)

    handler = None  # built lazily so __identity_check__ works even if the real handler can't

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            emit({"contract_version": "1.0", "run_id": "unknown", "ok": False, "error_code": "INVALID_INPUT",
                  "error_type": "JSONDecodeError", "error_detail": f"bad request json: {exc}", "retryable": False})
            continue

        run_id = request.get("run_id", "unknown")
        idempotency_key = request.get("idempotency_key", "unknown")
        actor = request.get("actor", {})
        op = request.get("op")
        # The orchestrator's stage execution ids (optional): echoed so a response and this worker's log
        # lines can be joined to the execution that caused them.
        trace = {k: request[k] for k in ("trace_id", "execution_id") if request.get(k)}
        try:
            if op == "__identity_check__":
                result = _identity_check(uc_id, src_dir)
            elif op == "__llm_status__":
                if handler is None:
                    handler = _build_handler(uc_id, src_dir)
                result = _llm_status(uc_id, handler)
            else:
                if handler is None:
                    handler = _build_handler(uc_id, src_dir)
                result = handler.dispatch(op, request.get("payload", {}), run_id=run_id,
                                          idempotency_key=idempotency_key, actor=actor)
            emit({"contract_version": "1.0", "run_id": run_id, "ok": True, "result": result, **trace})
        except Exception as exc:  # noqa: BLE001 -- must never crash the loop; surface as a response
            code, retryable = _classify(exc)
            ids = " ".join(f"{k}={v}" for k, v in trace.items())
            print(f"{uc_id} worker: op '{op}' (run {run_id}{' ' + ids if ids else ''}) failed\n"
                  f"{traceback.format_exc(limit=8)}", file=sys.stderr)
            emit({"contract_version": "1.0", "run_id": run_id, "ok": False, "error_code": code,
                  "error_type": type(exc).__name__, "error_detail": _short(exc), "retryable": retryable, **trace})


if __name__ == "__main__":
    main()
