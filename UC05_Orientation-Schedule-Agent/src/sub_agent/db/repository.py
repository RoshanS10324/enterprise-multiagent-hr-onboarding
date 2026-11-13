"""SupabaseOnboardingRepository: UC05's OnboardingPort over the shared Supabase tables.

The services, tool registry and workflow do not know this class exists: it implements the same
OnboardingPort contract as MockOnboarding and SqliteOnboardingRepository (sub_agent/repository.py),
so every business rule runs unchanged. The full table mapping is in docs/UC05_DATABASE_MAPPING.md.

Tenant isolation
----------------
The service-role key bypasses RLS, so isolation is enforced here, in every query:
  - A repository bound to a tenant (`for_tenant(client_id)`, which OnboardingToolRegistry does for
    its verified actor) adds `client_id=eq.<tenant>` to every read and write of a tenant-scoped table.
  - An unbound repository refuses any tenant-scoped lookup that does not name a client_id
    (TenantScopeRequiredError) -- an employee number is only unique within a client.
  - session_participants has no client_id column: a participant row is only read or written after
    its session has been read WITH the tenant filter.
  - A lookup for another tenant's record returns "not found" (KeyError), never the record.

Ownership
---------
  - onboarding_cases belongs to UC01. UC05 reads it and never inserts or updates it. "Creating" a
    UC05 case links UC05 to the existing case (an audit_events row UC05_CASE_LINKED); a missing case
    is refused (OnboardingCaseNotFoundError). The joining date is always read from the case.
  - Rows UC05 writes are marked as UC05's own (orientation_sessions.session_metadata.owner = 'uc05';
    communications.comm_type 'uc05:<template>:<language>'; audit_events.reference 'uc05:...';
    approval_requests.workflow_module), and only those are read back as UC05 data.

Atomicity without transactions (PostgREST runs one statement per call)
----------------------------------------------------------------------
  - Inserts whose identity is deterministic (stable_uuid) collide on the primary key when retried.
  - Read-modify-write updates are compare-and-set: the PATCH filters on the value read
    (updated_at / responded_at / status), and an empty result means someone else wrote first; the
    operation re-reads and tries again (bounded), it never overwrites blindly.
  - Buddy capacity: insert-then-verify; an assignment that pushes a buddy over max_active_buddies is
    rolled back and refused. Two racing assignments can both be refused (never both kept); a
    database function would make this a single atomic step (docs: required database-owner actions).

State with no table of its own (checklist tasks, the operation ledger, notification records) is kept
as versioned snapshot rows in audit_events, the pattern UC02 already uses for its case state.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from hr_onboarding_common.contracts import now_utc
from sub_agent.db import mapping as m
from sub_agent.db.postgrest import (PersistenceError, PostgrestClient, UniqueViolation, in_list)
from sub_agent.models import (BuddyCandidate, BuddyCapacityExceededError, CaseOwnershipError, CaseStatus,
                              ClientCalendar, ClientCalendarUnavailableError, ClientConfig, CommunicationDraft,
                              CommunicationStatus, DuplicateOnboardingCaseError, DuplicateRecordError,
                              DuplicateSessionError, DuplicateTaskError, InvalidClientConfigError, Invitation,
                              InvitationStatus, MissingClientConfigError, Notification, OnboardingCaseNotFoundError,
                              OnboardingRecord, OnboardingSession, OnboardingTask, OperationRecord, ParticipantResponse,
                              ResponseStatus, SessionStatus, SessionType, TaskStatus, TenantMismatchError,
                              TenantScopeRequiredError, UnsupportedLanguageError)

_CASE_COLUMNS = ("case_id,client_id,employee_no,candidate_name,candidate_email,joining_date,role_id,department,"
                 "manager_id,status,current_stage,is_deleted,location_name")
_CAS_ATTEMPTS = 5
# session_metadata keys for participants UC05 holds no e-mail address for (session_participants.email is NOT NULL)
PARTICIPANTS_WITHOUT_EMAIL = "participants_without_email"
SESSION_RESPONSES = "responses_without_participant_row"
_CANCELLED_CASE_STATUSES = frozenset({"CANCELLED", "CANCELED"})
_WITHDRAWN_CASE_STATUSES = frozenset({"WITHDRAWN", "OFFER_WITHDRAWN", "OFFER_DECLINED"})


class AmbiguousCaseError(PersistenceError):
    """More than one live onboarding case matches (client_id, employee_no). Never guessed."""

    code = "AMBIGUOUS_CASE"


class ConcurrentUpdateError(PersistenceError):
    """A compare-and-set kept losing to concurrent writers. Nothing was overwritten."""

    code = "CONCURRENT_UPDATE"
    retryable = True


class SessionConflictError(DuplicateSessionError):
    """The employee already has a live session overlapping this time."""


class DuplicateCommunicationError(DuplicateRecordError):
    """A live (not rejected) communication of this template already exists for the case."""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _ts(value: Any) -> datetime:
    text = str(value).replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _date(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _eq(column: str, value: Any) -> tuple[str, str]:
    return column, f"eq.{value}"


_BUDDY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_PLACEHOLDER = re.compile(r"[<>{}\[\]]|\b(placeholder|dummy|test buddy|sample|lorem|tbd|todo|xxx|"
                          r"colleague (employee number|full name)|employee number|full name)\b", re.IGNORECASE)


def is_real_buddy_profile(profile: dict[str, Any]) -> bool:
    """Whether a buddy_profiles row holds real identifying values: an employee id of identifier shape and a name that
    is neither empty nor template text. The same rule the orchestrator's worker applies to UC05's local mode."""
    employee_id = str(profile.get("employee_id") or "")
    name = str(profile.get("full_name") or "").strip()
    return bool(_BUDDY_ID.fullmatch(employee_id) and name and not _PLACEHOLDER.search(name)
                and not _PLACEHOLDER.search(employee_id))


class SupabaseOnboardingRepository:
    def __init__(self, client: PostgrestClient, *, tenant: str | None = None,
                 clock: Callable[[], datetime] = now_utc, _shared: dict | None = None) -> None:
        self.client, self.tenant, self.clock = client, tenant, clock
        # Immutable identities only (case uuid <-> employee number). Never a joining date or a
        # status, which are always read fresh.
        self._shared = _shared if _shared is not None else {"case_ids": {}, "employees": {}}

    def __repr__(self) -> str:
        return f"SupabaseOnboardingRepository(tenant={self.tenant!r})"

    # ================================================================== scoping
    def for_tenant(self, client_id: str) -> "SupabaseOnboardingRepository":
        """A view bound to one verified tenant: every query carries its client_id."""
        if not client_id:
            raise TenantScopeRequiredError("a tenant view needs a verified client_id")
        if self.tenant is not None and self.tenant != client_id:
            raise TenantMismatchError("this repository is already bound to another client")
        return SupabaseOnboardingRepository(self.client, tenant=client_id, clock=self.clock, _shared=self._shared)

    def _scope(self, client_id: str | None, what: str) -> str:
        if self.tenant is not None and client_id is not None and client_id != self.tenant:
            raise TenantMismatchError(f"{what} is not accessible to this client")
        tenant = self.tenant or client_id
        if not tenant:
            raise TenantScopeRequiredError(f"{what} requires a verified client_id (tenant-scoped lookup)")
        return tenant

    def _now(self) -> datetime:
        return self.clock()

    # ================================================================== onboarding cases (UC01's, read-only)
    def _case_rows_for_employee(self, tenant: str, employee_id: str) -> list[dict[str, Any]]:
        return self.client.select("onboarding_cases", columns=_CASE_COLUMNS,
                                  filters=[_eq("client_id", tenant), _eq("employee_no", employee_id)])

    def _case_row(self, tenant: str, employee_id: str) -> dict[str, Any]:
        rows = self._case_rows_for_employee(tenant, employee_id)
        live = [r for r in rows if r.get("is_deleted") is not True]
        if len(live) > 1:
            raise AmbiguousCaseError(f"{len(live)} live onboarding cases match employee {employee_id} in this client")
        row = live[0] if live else (rows[-1] if rows else None)
        if row is None:
            raise OnboardingCaseNotFoundError(f"no onboarding case for employee {employee_id} in this client")
        self._remember(tenant, row)
        return row

    def _remember(self, tenant: str, row: dict[str, Any]) -> None:
        if row.get("employee_no") and row.get("is_deleted") is not True:
            self._shared["case_ids"][(tenant, row["employee_no"])] = row["case_id"]
            self._shared["employees"][(tenant, row["case_id"])] = row["employee_no"]

    def case_id_for(self, employee_id: str, client_id: str | None = None) -> str:
        tenant = self._scope(client_id, f"employee {employee_id}")
        cached = self._shared["case_ids"].get((tenant, employee_id))
        return cached or self._case_row(tenant, employee_id)["case_id"]

    def _employee_for_case(self, tenant: str, case_id: str) -> str:
        cached = self._shared["employees"].get((tenant, case_id))
        if cached:
            return cached
        rows = self.client.select("onboarding_cases", columns=_CASE_COLUMNS,
                                  filters=[_eq("client_id", tenant), _eq("case_id", case_id)])
        if not rows or not rows[0].get("employee_no"):
            raise KeyError(case_id)
        self._remember(tenant, rows[0])
        return rows[0]["employee_no"]

    @staticmethod
    def _case_status(row: dict[str, Any]) -> CaseStatus:
        if row.get("is_deleted") is True:
            return CaseStatus.WITHDRAWN
        status = str(row.get("status") or "").upper()
        if status in _CANCELLED_CASE_STATUSES:
            return CaseStatus.CANCELLED
        if status in _WITHDRAWN_CASE_STATUSES:
            return CaseStatus.WITHDRAWN
        return CaseStatus.ACTIVE

    def _record(self, row: dict[str, Any], buddy_id: str | None) -> OnboardingRecord:
        return OnboardingRecord(
            employee_id=row["employee_no"], full_name=row.get("candidate_name") or "",
            email=row.get("candidate_email") or "", role_title=row.get("role_id") or "",
            department=row.get("department") or "", team=row.get("department") or "",
            team_lead_id=row.get("manager_id") or "", client_id=row["client_id"],
            start_date=_date(row["joining_date"]), case_status=self._case_status(row), buddy_id=buddy_id)

    def get_onboarding_record(self, employee_id: str) -> OnboardingRecord:
        tenant = self._scope(None, f"employee {employee_id}")
        row = self._case_row(tenant, employee_id)
        return self._record(row, self._confirmed_buddies(tenant, [row["case_id"]]).get(row["case_id"]))

    def _linked_case_ids(self, tenant: str) -> list[str]:
        rows = self.client.select("audit_events", columns="case_id",
                                  filters=[_eq("client_id", tenant), _eq("action", m.ACTION_CASE_LINKED)])
        return sorted({r["case_id"] for r in rows if r.get("case_id")})

    def is_case_linked(self, case_id: str, client_id: str | None = None) -> bool:
        tenant = self._scope(client_id, f"case {case_id}")
        return bool(self.client.select("audit_events", columns="event_id", limit=1, filters=[
            _eq("client_id", tenant), _eq("action", m.ACTION_CASE_LINKED), _eq("case_id", case_id)]))

    def list_onboarding_records(self, *, client_id: str | None = None) -> list[OnboardingRecord]:
        """The cases UC05 has taken on (linked) in this tenant -- not every UC01 case."""
        tenant = self._scope(client_id, "listing onboarding cases")
        case_ids = self._linked_case_ids(tenant)
        rows: list[dict[str, Any]] = []
        for i in range(0, len(case_ids), 100):
            rows += self.client.select("onboarding_cases", columns=_CASE_COLUMNS, filters=[
                _eq("client_id", tenant), ("case_id", in_list(case_ids[i:i + 100]))])
        rows = [r for r in rows if r.get("employee_no")]
        for r in rows:
            self._remember(tenant, r)
        buddies = self._confirmed_buddies(tenant, [r["case_id"] for r in rows])
        return sorted((self._record(r, buddies.get(r["case_id"])) for r in rows), key=lambda rec: rec.employee_id)

    def create_onboarding_case(self, record: OnboardingRecord) -> OnboardingRecord:
        """Links UC05 to the EXISTING case (UC05 never creates one). Idempotent through a
        deterministic audit row: a second link raises DuplicateOnboardingCaseError, as the contract
        requires for "one case per employee"."""
        tenant = self._scope(record.client_id, f"employee {record.employee_id}")
        row = self._case_row(tenant, record.employee_id)
        if self._case_status(row) != CaseStatus.ACTIVE and row.get("is_deleted") is True:
            raise OnboardingCaseNotFoundError(f"the onboarding case for {record.employee_id} has been deleted")
        try:
            self.client.insert("audit_events", [self._audit_row(
                tenant, m.ACTION_CASE_LINKED, event_id=m.stable_uuid("case-link", row["case_id"]),
                case_id=row["case_id"], reference=f"{m.REF_PREFIX}case-link:{row['case_id']}",
                detail={"employee_id": record.employee_id, "joining_date": str(row["joining_date"])[:10]},
                outcome="SUCCESS")])
        except UniqueViolation:
            raise DuplicateOnboardingCaseError(f"UC05 already holds the onboarding case for {record.employee_id}") from None
        return self.get_onboarding_record(record.employee_id)

    def update_case_status(self, employee_id: str, status: CaseStatus) -> OnboardingRecord:
        raise CaseOwnershipError("onboarding_cases belongs to UC01; UC05 never changes a case's status")

    # ================================================================== buddies
    def _confirmed_buddies(self, tenant: str, case_ids: list[str]) -> dict[str, str]:
        if not case_ids:
            return {}
        rows = self.client.select("buddy_assignments", columns="case_id,buddy_employee_id,status", filters=[
            _eq("client_id", tenant), ("case_id", in_list(case_ids)), ("status", in_list(m.BUDDY_ACTIVE_STATUSES))])
        return {r["case_id"]: r["buddy_employee_id"] for r in rows}

    def _buddy_load(self, tenant: str, buddy_ids: Iterable[str] | None = None) -> dict[str, int]:
        filters = [_eq("client_id", tenant), ("status", in_list(m.BUDDY_ACTIVE_STATUSES))]
        if buddy_ids is not None:
            filters.append(("buddy_employee_id", in_list(buddy_ids)))
        load: dict[str, int] = {}
        for r in self.client.select("buddy_assignments", columns="buddy_employee_id", filters=filters):
            load[r["buddy_employee_id"]] = load.get(r["buddy_employee_id"], 0) + 1
        return load

    @staticmethod
    def _has_capacity(profile: dict[str, Any], load: int) -> bool:
        cap = profile.get("max_active_buddies")
        return cap is None or load < int(cap)

    def _buddy_profiles(self, tenant: str, employee_id: str | None = None) -> list[dict[str, Any]]:
        """Active profiles of this tenant holding real identifying values. A profile whose employee id or name is
        template text ("<colleague full name>", "placeholder", ...) is never offered or assigned: it is skipped, not
        repaired (HR corrects the record)."""
        filters = [_eq("client_id", tenant), ("is_active", "is.true")]
        if employee_id is not None:
            filters.append(_eq("employee_id", employee_id))
        return [p for p in self.client.select("buddy_profiles", filters=filters, order="employee_id.asc")
                if is_real_buddy_profile(p)]

    def list_buddy_candidates(self, client_id: str) -> list[BuddyCandidate]:
        """Active profiles of this tenant that still have capacity. `team` is the profile's
        department (the same field the new hire's `team` is read from)."""
        tenant = self._scope(client_id, "buddy candidates")
        profiles = self._buddy_profiles(tenant)
        load = self._buddy_load(tenant)
        return [BuddyCandidate(employee_id=p["employee_id"], full_name=p.get("full_name") or "", client_id=tenant,
                               team=p.get("department") or "", active=True)
                for p in profiles if self._has_capacity(p, load.get(p["employee_id"], 0))]

    def _assignment(self, tenant: str, case_id: str) -> dict[str, Any] | None:
        rows = self.client.select("buddy_assignments", filters=[_eq("client_id", tenant), _eq("case_id", case_id)])
        return rows[0] if rows else None

    def get_buddy_assignment(self, employee_id: str) -> dict[str, Any] | None:
        tenant = self._scope(None, f"employee {employee_id}")
        return self._assignment(tenant, self.case_id_for(employee_id, tenant))

    def record_buddy_proposal(self, employee_id: str, buddy_id: str, *, approval_id: str, match_score: float,
                              same_team: bool) -> None:
        """buddy_assignments row in status PROPOSED with the matching criteria the rule used. A
        confirmed assignment is never downgraded."""
        tenant = self._scope(None, f"employee {employee_id}")
        case_id = self.case_id_for(employee_id, tenant)
        values = {"buddy_employee_id": buddy_id, "status": m.BUDDY_PROPOSED, "match_score": round(match_score, 3),
                  "matching_criteria": {"rule": "same_team_first", "same_team": same_team, "approval_id": approval_id},
                  "assigned_at": None, "confirmed_at": None}
        for _ in range(_CAS_ATTEMPTS):
            existing = self._assignment(tenant, case_id)
            if existing is None:
                try:
                    self.client.insert("buddy_assignments", [{
                        "assignment_id": m.stable_uuid("buddy-assignment", case_id), "case_id": case_id,
                        "client_id": tenant, **values}])
                    return
                except UniqueViolation:
                    continue
            if str(existing.get("status") or "").upper() == m.BUDDY_CONFIRMED:
                return
            if self.client.update("buddy_assignments", values, filters=[
                    _eq("assignment_id", existing["assignment_id"]), _eq("client_id", tenant),
                    _eq("status", existing["status"])]):
                return
        raise ConcurrentUpdateError("buddy proposal kept changing concurrently")

    def release_buddy_proposal(self, employee_id: str, buddy_id: str) -> None:
        tenant = self._scope(None, f"employee {employee_id}")
        self.client.update("buddy_assignments", {"status": m.BUDDY_REJECTED}, filters=[
            _eq("client_id", tenant), _eq("case_id", self.case_id_for(employee_id, tenant)),
            _eq("status", m.BUDDY_PROPOSED), _eq("buddy_employee_id", buddy_id)])

    def assign_buddy(self, employee_id: str, buddy_id: str) -> OnboardingRecord:
        """Confirms `buddy_id` for the case (upsert on the unique case_id), then re-counts the buddy's
        active assignments: one over max_active_buddies is rolled back and refused."""
        tenant = self._scope(None, f"employee {employee_id}")
        case_id = self.case_id_for(employee_id, tenant)
        profiles = self._buddy_profiles(tenant, buddy_id)
        if not profiles:
            raise KeyError(f"{buddy_id} is not an active buddy profile in this client")
        now = _iso(self._now())
        for _ in range(_CAS_ATTEMPTS):
            existing = self._assignment(tenant, case_id)
            if (existing and str(existing.get("status") or "").upper() == m.BUDDY_CONFIRMED
                    and existing.get("buddy_employee_id") == buddy_id):
                return self.get_onboarding_record(employee_id)
            same = existing is not None and existing.get("buddy_employee_id") == buddy_id
            values = {"buddy_employee_id": buddy_id, "status": m.BUDDY_CONFIRMED,
                      "assigned_at": (existing or {}).get("assigned_at") if same and (existing or {}).get("assigned_at")
                      else now, "confirmed_at": now}
            if existing is None:
                try:
                    self.client.insert("buddy_assignments", [{
                        "assignment_id": m.stable_uuid("buddy-assignment", case_id), "case_id": case_id,
                        "client_id": tenant, "match_score": None, "matching_criteria": {"rule": "hr_assigned"},
                        **values}])
                except UniqueViolation:
                    continue
            elif not self.client.update("buddy_assignments", values, filters=[
                    _eq("assignment_id", existing["assignment_id"]), _eq("client_id", tenant),
                    _eq("status", existing["status"]), _eq("buddy_employee_id", existing["buddy_employee_id"])]):
                continue
            if not self._has_capacity(profiles[0], self._buddy_load(tenant, [buddy_id]).get(buddy_id, 0) - 1):
                self._roll_back_assignment(tenant, case_id, existing)
                raise BuddyCapacityExceededError(f"{buddy_id} has reached max_active_buddies; nothing was assigned")
            return self.get_onboarding_record(employee_id)
        raise ConcurrentUpdateError("buddy assignment kept changing concurrently")

    def _roll_back_assignment(self, tenant: str, case_id: str, previous: dict[str, Any] | None) -> None:
        filters = [_eq("client_id", tenant), _eq("case_id", case_id)]
        if previous is None:
            self.client.delete("buddy_assignments", filters=filters)
            return
        self.client.update("buddy_assignments", {k: previous.get(k) for k in (
            "buddy_employee_id", "status", "assigned_at", "confirmed_at", "match_score", "matching_criteria")},
            filters=filters)

    # ================================================================== audit-backed state (no table of its own)
    def _audit_row(self, tenant: str, action: str, *, event_id: str, reference: str, detail: dict[str, Any],
                   case_id: str | None = None, after_state: dict[str, Any] | None = None,
                   outcome: str = "RECORDED") -> dict[str, Any]:
        return {"event_id": event_id, "case_id": case_id, "client_id": tenant, "actor_type": "AGENT",
                "actor_id": m.AGENT_ID, "actor_role": "agent", "action": action, "outcome": outcome,
                "detail": m.sanitize(detail), "before_state": None, "after_state": after_state,
                "correlation_id": None, "reference": reference, "occurred_at": _iso(self._now())}

    def _latest_states(self, tenant: str, action: str, *, case_id: str | None = None,
                       entity_id: str | None = None) -> dict[str, tuple[int, dict[str, Any]]]:
        filters = [_eq("client_id", tenant), _eq("action", action)]
        if case_id is not None:
            filters.append(_eq("case_id", case_id))
        if entity_id is not None:
            filters.append(_eq("detail->>entity_id", entity_id))
        latest: dict[str, tuple[int, dict[str, Any]]] = {}
        for r in self.client.select("audit_events", columns="detail,after_state", filters=filters):
            detail = r.get("detail") or {}
            key, version = detail.get("entity_id"), int(detail.get("version") or 0)
            if key is not None and (key not in latest or version > latest[key][0]):
                latest[key] = (version, r.get("after_state") or {})
        return latest

    def _save_state(self, tenant: str, action: str, entity_id: str, body: dict[str, Any], *, version: int,
                    case_id: str | None) -> None:
        """Version `version` of an entity; raises UniqueViolation if that version already exists
        (a concurrent writer) -- the deterministic event_id is the compare-and-set."""
        self.client.insert("audit_events", [self._audit_row(
            tenant, action, event_id=m.stable_uuid(action, tenant, entity_id, version), case_id=case_id,
            reference=f"{m.REF_PREFIX}{action.lower()}:{m.stable_uuid(entity_id)}:{version}",
            detail={"entity_id": entity_id, "version": version}, after_state=body)])

    def _upsert_state(self, tenant: str, action: str, entity_id: str, body: Callable[[dict | None], dict], *,
                      case_id: str | None) -> dict[str, Any]:
        for _ in range(_CAS_ATTEMPTS):
            current = self._latest_states(tenant, action, entity_id=entity_id).get(entity_id)
            new_body = body(current[1] if current else None)
            try:
                self._save_state(tenant, action, entity_id, new_body, version=(current[0] if current else 0) + 1,
                                 case_id=case_id)
                return new_body
            except UniqueViolation:
                continue
        raise ConcurrentUpdateError(f"{action} for one record kept changing concurrently")

    # ---- notifications (append-only)
    def record_notification(self, employee_id: str, recipient_id: str, kind: str, **detail: object) -> Notification:
        tenant = self._scope(None, f"employee {employee_id}")
        case_id = self.case_id_for(employee_id, tenant)
        notification = Notification(employee_id=employee_id, recipient_id=recipient_id, kind=kind,
                                    detail=m.sanitize(dict(detail)))
        self.client.insert("audit_events", [self._audit_row(
            tenant, m.ACTION_NOTIFICATION, event_id=m.stable_uuid("notification", notification.id), case_id=case_id,
            reference=f"{m.REF_PREFIX}notification:{notification.id}", outcome="SUCCESS",
            detail={"kind": kind, "recipient_id": recipient_id},
            after_state=notification.model_dump(mode="json"))])
        return notification

    def list_notifications(self, employee_id: str) -> list[Notification]:
        tenant = self._scope(None, f"employee {employee_id}")
        rows = self.client.select("audit_events", columns="after_state", filters=[
            _eq("client_id", tenant), _eq("case_id", self.case_id_for(employee_id, tenant)),
            _eq("action", m.ACTION_NOTIFICATION)])
        return sorted((Notification.model_validate(r["after_state"]) for r in rows if r.get("after_state")),
                      key=lambda n: n.id)

    # ---- checklist tasks
    def create_task(self, task: OnboardingTask) -> OnboardingTask:
        tenant = self._scope(task.client_id, f"employee {task.employee_id}")
        case_id = self.case_id_for(task.employee_id, tenant)
        if any(t.task_type == task.task_type and t.status != TaskStatus.CANCELLED for t in self.list_tasks(task.employee_id)):
            raise DuplicateTaskError(f"task '{task.task_type}' already exists for {task.employee_id}")
        try:
            self._save_state(tenant, m.ACTION_TASK_STATE, task.id, task.model_dump(mode="json"), version=1,
                             case_id=case_id)
        except UniqueViolation:
            raise DuplicateTaskError(f"task {task.id} already exists") from None
        return task.model_copy(deep=True)

    def get_task(self, task_id: str, *, client_id: str | None = None) -> OnboardingTask:
        tenant = self._scope(client_id, f"task {task_id}")
        found = self._latest_states(tenant, m.ACTION_TASK_STATE, entity_id=task_id).get(task_id)
        if found is None:
            raise KeyError(task_id)
        return OnboardingTask.model_validate(found[1])

    def list_tasks(self, employee_id: str) -> list[OnboardingTask]:
        tenant = self._scope(None, f"employee {employee_id}")
        states = self._latest_states(tenant, m.ACTION_TASK_STATE, case_id=self.case_id_for(employee_id, tenant))
        return sorted((OnboardingTask.model_validate(body) for _, body in states.values()), key=lambda t: t.id)

    def update_task_status(self, task_id: str, status: TaskStatus) -> OnboardingTask:
        tenant = self._scope(None, f"task {task_id}")
        status = TaskStatus(status)
        task = self.get_task(task_id)
        case_id = self.case_id_for(task.employee_id, tenant)

        def change(current: dict | None) -> dict:
            if current is None:
                raise KeyError(task_id)
            t = OnboardingTask.model_validate(current)
            return t.model_copy(update={"status": status, "completed_at": self._now()
                                        if status == TaskStatus.COMPLETED else t.completed_at}).model_dump(mode="json")

        return OnboardingTask.model_validate(self._upsert_state(tenant, m.ACTION_TASK_STATE, task_id, change,
                                                                case_id=case_id))

    # ---- operation ledger (idempotency of calendar bookings and notifications)
    def get_operation(self, key: str) -> OperationRecord | None:
        tenant = self._scope(None, f"operation {key}")
        found = self._latest_states(tenant, m.ACTION_OPERATION_STATE, entity_id=key).get(key)
        return OperationRecord.model_validate(found[1]) if found else None

    def save_operation(self, record: OperationRecord) -> OperationRecord:
        tenant = self._scope(record.client_id, f"operation {record.key}")
        stored = record.model_copy(update={"updated_at": self._now()})
        self._upsert_state(tenant, m.ACTION_OPERATION_STATE, record.key, lambda _cur: stored.model_dump(mode="json"),
                           case_id=None)
        return stored

    def list_operations(self, *, client_id: str | None = None,
                        statuses: tuple[str, ...] = ()) -> list[OperationRecord]:
        tenant = self._scope(client_id, "listing operations")
        records = [OperationRecord.model_validate(body)
                   for _, body in self._latest_states(tenant, m.ACTION_OPERATION_STATE).values()]
        return sorted((r for r in records if not statuses or r.status in statuses), key=lambda r: r.key)

    # ================================================================== orientation sessions
    @staticmethod
    def _session_status(row: dict[str, Any]) -> SessionStatus:
        """The column says SCHEDULED for both a tentative hold and an HR-confirmed session (the live
        vocabulary has no 'confirmed'); UC05's exact status is kept in session_metadata."""
        if str(row.get("status") or "").upper() == "CANCELLED":
            return SessionStatus.CANCELLED
        exact = (row.get("session_metadata") or {}).get("uc05_status")
        return SessionStatus(exact) if exact else SessionStatus.SCHEDULED

    def _session(self, row: dict[str, Any]) -> OnboardingSession:
        meta = row.get("session_metadata") or {}
        return OnboardingSession(
            id=row["session_id"], employee_id=meta["employee_id"], client_id=row["client_id"],
            session_type=SessionType(meta["uc05_session_type"]), scheduled_start=_ts(row["start_utc"]),
            scheduled_end=_ts(row["end_utc"]), status=self._session_status(row),
            attendees=list(meta.get("attendees") or []), external_event_ref=row.get("calendar_event_id"),
            booking_operation_key=meta.get("booking_operation_key"))

    @staticmethod
    def _session_status_values(row: dict[str, Any], status: SessionStatus) -> dict[str, Any]:
        """Column value + UC05's exact status, written together in one PATCH."""
        return {"status": m.to_db(m.SESSION_STATUS, status.value),
                "session_metadata": {**(row.get("session_metadata") or {}), "uc05_status": status.value}}

    @staticmethod
    def _ours(row: dict[str, Any]) -> bool:
        meta = row.get("session_metadata") or {}
        return meta.get("owner") == "uc05" and bool(meta.get("uc05_session_type")) and bool(meta.get("employee_id"))

    def _session_row(self, tenant: str, session_id: str) -> dict[str, Any]:
        if not m.is_uuid(session_id):
            raise KeyError(session_id)
        rows = [r for r in self.client.select("orientation_sessions", filters=[
            _eq("session_id", session_id), _eq("client_id", tenant)]) if self._ours(r)]
        if not rows:
            raise KeyError(session_id)
        return rows[0]

    def _session_rows_for_case(self, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return [r for r in self.client.select("orientation_sessions", filters=[
            _eq("client_id", tenant), _eq("case_id", case_id)], order="created_at.asc") if self._ours(r)]

    def create_session(self, session: OnboardingSession) -> OnboardingSession:
        tenant = self._scope(session.client_id, f"employee {session.employee_id}")
        case = self._case_row(tenant, session.employee_id)
        live = [self._session(r) for r in self._session_rows_for_case(tenant, case["case_id"])]
        live = [s for s in live if s.status != SessionStatus.CANCELLED]
        if any(s.session_type == session.session_type and s.scheduled_start == session.scheduled_start for s in live):
            raise DuplicateSessionError(f"a {session.session_type} session already exists for "
                                        f"{session.employee_id} at {session.scheduled_start}")
        if any(s.scheduled_start < session.scheduled_end and s.scheduled_end > session.scheduled_start for s in live):
            raise SessionConflictError(f"{session.employee_id} already has a session overlapping "
                                       f"{session.scheduled_start}-{session.scheduled_end}")
        session_id = m.stable_uuid("session", tenant, session.id)
        now = _iso(self._now())
        self.client.insert("orientation_sessions", [{
            "session_id": session_id, "case_id": case["case_id"], "client_id": tenant,
            "session_type": m.to_db(m.SESSION_TYPE, session.session_type.value),
            "start_utc": _iso(session.scheduled_start),
            "end_utc": _iso(session.scheduled_end), "meeting_link": None, "room_location": None,
            "status": m.to_db(m.SESSION_STATUS, session.status.value),
            "calendar_event_id": session.external_event_ref or None, "created_at": now, "updated_at": now,
            "session_metadata": {"owner": "uc05", "uc05_session_id": session.id, "employee_id": session.employee_id,
                                 "uc05_session_type": session.session_type.value,
                                 "uc05_status": session.status.value, "attendees": list(session.attendees),
                                 "booking_operation_key": session.booking_operation_key,
                                 "joining_date_basis": str(case["joining_date"])[:10], "invitations": {}}}])
        return session.model_copy(update={"id": session_id}, deep=True)

    def get_session(self, session_id: str, *, client_id: str | None = None) -> OnboardingSession:
        tenant = self._scope(client_id, f"session {session_id}")
        return self._session(self._session_row(tenant, session_id))

    def list_sessions(self, employee_id: str) -> list[OnboardingSession]:
        tenant = self._scope(None, f"employee {employee_id}")
        return [self._session(r) for r in self._session_rows_for_case(tenant, self.case_id_for(employee_id, tenant))]

    def list_sessions_in_range(self, start: datetime, end: datetime, *, client_id: str | None = None,
                               include_cancelled: bool = False) -> list[OnboardingSession]:
        """UC05 sessions of the tenant overlapping [start, end)."""
        tenant = self._scope(client_id, "sessions by date range")
        rows = self.client.select("orientation_sessions", order="start_utc.asc", filters=[
            _eq("client_id", tenant), ("start_utc", f"lt.{_iso(end)}"), ("end_utc", f"gt.{_iso(start)}")])
        sessions = [self._session(r) for r in rows if self._ours(r)]
        return [s for s in sessions if include_cancelled or s.status != SessionStatus.CANCELLED]

    def find_conflicting_sessions(self, employee_id: str, start: datetime, end: datetime) -> list[OnboardingSession]:
        return [s for s in self.list_sessions(employee_id) if s.status != SessionStatus.CANCELLED
                and s.scheduled_start < end and s.scheduled_end > start]

    def stale_sessions(self, employee_id: str) -> list[OnboardingSession]:
        """Live sessions drafted for an earlier joining date than the case now holds (a joining-date
        revision since scheduling): they must be re-validated by HR before onboarding relies on them."""
        tenant = self._scope(None, f"employee {employee_id}")
        case = self._case_row(tenant, employee_id)
        current = str(case["joining_date"])[:10]
        return [self._session(r) for r in self._session_rows_for_case(tenant, case["case_id"])
                if (r.get("session_metadata") or {}).get("joining_date_basis") not in (None, current)
                and self._session_status(r) != SessionStatus.CANCELLED]

    def _patch_session(self, tenant: str, session_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]
                       ) -> dict[str, Any]:
        """Compare-and-set on updated_at: re-read and retry, never overwrite a concurrent change."""
        for _ in range(_CAS_ATTEMPTS):
            row = self._session_row(tenant, session_id)
            values = {**change(row), "updated_at": _iso(self._now())}
            updated = self.client.update("orientation_sessions", values, filters=[
                _eq("session_id", session_id), _eq("client_id", tenant), _eq("updated_at", row["updated_at"])])
            if updated:
                return updated[0]
        raise ConcurrentUpdateError(f"session {session_id} kept changing concurrently")

    def update_session_status(self, session_id: str, status: SessionStatus) -> OnboardingSession:
        tenant = self._scope(None, f"session {session_id}")
        return self._session(self._patch_session(tenant, session_id,
                                                 lambda row: self._session_status_values(row, status)))

    def cancel_session(self, session_id: str) -> OnboardingSession:
        return self.update_session_status(session_id, SessionStatus.CANCELLED)

    def update_session_time(self, session_id: str, start: datetime, end: datetime) -> OnboardingSession:
        """Moves a live session (UTC storage). UC05's own business rule reschedules by cancelling and
        re-booking through a fresh approval; this exists for operators and is conflict-checked."""
        if end <= start:
            raise ValueError("end must be after start")
        tenant = self._scope(None, f"session {session_id}")
        current = self._session(self._session_row(tenant, session_id))
        if current.status == SessionStatus.CANCELLED:
            raise ValueError(f"session {session_id} is cancelled")
        if any(s.id != session_id for s in self.find_conflicting_sessions(current.employee_id, start, end)):
            raise SessionConflictError(f"{current.employee_id} already has a session overlapping {start}-{end}")
        return self._session(self._patch_session(tenant, session_id,
                                                 lambda _row: {"start_utc": _iso(start), "end_utc": _iso(end)}))

    # ---- participants, invitations and RSVPs
    def _participant_role(self, tenant: str, session_row: dict[str, Any], participant_id: str
                          ) -> tuple[str, str | None]:
        employee_id = (session_row.get("session_metadata") or {}).get("employee_id")
        case = self._case_row(tenant, employee_id)
        if participant_id == employee_id:
            return "employee", case.get("candidate_email")
        if participant_id == case.get("manager_id"):
            return "team_lead", None
        if participant_id == self._confirmed_buddies(tenant, [case["case_id"]]).get(case["case_id"]):
            return "buddy", None
        return "participant", None

    def _ensure_participant(self, tenant: str, session_row: dict[str, Any], participant_id: str) -> str:
        pid = m.stable_uuid("participant", session_row["session_id"], participant_id)
        role, email = self._participant_role(tenant, session_row, participant_id)
        if not email:
            # session_participants.email is NOT NULL and UC05 holds no address for this participant (onboarding_cases
            # names the team lead by id only; buddy_profiles holds no e-mail): the participant is recorded on the
            # session instead, with the reason -- never with an invented address.
            self._note_participant_without_email(tenant, session_row, participant_id, role)
            return pid
        self.client.insert("session_participants", [{
            "participant_id": pid, "session_id": session_row["session_id"], "actor_id": participant_id,
            "email": email, "role": m.to_db(m.PARTICIPANT_ROLE, role)}], on_conflict="participant_id", ignore_duplicates=True)
        return pid

    def _note_participant_without_email(self, tenant: str, session_row: dict[str, Any], participant_id: str,
                                        role: str) -> None:
        noted = ((session_row.get("session_metadata") or {}).get(PARTICIPANTS_WITHOUT_EMAIL) or {})
        if participant_id in noted:
            return

        def add(row: dict[str, Any]) -> dict[str, Any]:
            meta = dict(row.get("session_metadata") or {})
            meta[PARTICIPANTS_WITHOUT_EMAIL] = {**(meta.get(PARTICIPANTS_WITHOUT_EMAIL) or {}), participant_id: {
                "role": m.to_db(m.PARTICIPANT_ROLE, role), "reason": "EMAIL_UNKNOWN",
                "detail": "session_participants.email is required; UC05 holds no e-mail address for this participant"}}
            return {"session_metadata": meta}

        self._patch_session(tenant, session_row["session_id"], add)

    @staticmethod
    def _invitation_id(session_id: str, participant_id: str) -> str:
        return f"inv|{session_id}|{participant_id}"

    def record_invitation(self, session_id: str, participant_id: str, status: InvitationStatus) -> Invitation:
        tenant = self._scope(None, f"session {session_id}")
        self._ensure_participant(tenant, self._session_row(tenant, session_id), participant_id)
        invitation = Invitation(id=self._invitation_id(session_id, participant_id), session_id=session_id,
                                participant_id=participant_id, status=status, sent_at=self._now())

        def add(row: dict[str, Any]) -> dict[str, Any]:
            meta = dict(row.get("session_metadata") or {})
            meta["invitations"] = {**(meta.get("invitations") or {}), invitation.id: invitation.model_dump(mode="json")}
            return {"session_metadata": meta}

        self._patch_session(tenant, session_id, add)
        return invitation

    def update_invitation_status(self, invitation_id: str, status: InvitationStatus) -> Invitation:
        try:
            _, session_id, _participant = invitation_id.split("|", 2)
        except ValueError:
            raise KeyError(invitation_id) from None
        tenant = self._scope(None, f"invitation {invitation_id}")
        updated: dict[str, Any] = {}

        def change(row: dict[str, Any]) -> dict[str, Any]:
            meta = dict(row.get("session_metadata") or {})
            invitations = dict(meta.get("invitations") or {})
            if invitation_id not in invitations:
                raise KeyError(invitation_id)
            invitations[invitation_id] = {**invitations[invitation_id], "status": status.value,
                                          "sent_at": _iso(self._now())}
            updated.update(invitations[invitation_id])
            return {"session_metadata": {**meta, "invitations": invitations}}

        self._patch_session(tenant, session_id, change)
        return Invitation.model_validate(updated)

    def list_invitations(self, session_id: str) -> list[Invitation]:
        tenant = self._scope(None, f"session {session_id}")
        meta = self._session_row(tenant, session_id).get("session_metadata") or {}
        return sorted((Invitation.model_validate(v) for v in (meta.get("invitations") or {}).values()),
                      key=lambda i: i.id)

    def record_participant_response(self, session_id: str, participant_id: str, response: ResponseStatus, *,
                                    responded_at: datetime | None = None, proposed_new_time: datetime | None = None,
                                    note: str | None = None) -> ParticipantResponse:
        """Newest-wins, atomically: the write is conditional on the responded_at it read, so an older,
        late-delivered response can never overwrite a newer one, even under concurrency."""
        tenant = self._scope(None, f"session {session_id}")
        session_row = self._session_row(tenant, session_id)  # tenant-checked before any participant access
        when = responded_at or self._now()
        pid = m.stable_uuid("participant", session_id, participant_id)
        if participant_id in ((session_row.get("session_metadata") or {}).get(PARTICIPANTS_WITHOUT_EMAIL) or {}):
            return self._session_response(tenant, session_id, participant_id, pid, response, when, proposed_new_time,
                                          note)
        values = {"rsvp_status": m.to_db(m.RSVP_STATUS, response.value), "responded_at": _iso(when),
                  "proposed_new_time": _iso(proposed_new_time) if proposed_new_time else None,
                  "response_note": (note or None) and note[:500]}
        for _ in range(_CAS_ATTEMPTS):
            rows = self.client.select("session_participants", filters=[_eq("participant_id", pid),
                                                                      _eq("session_id", session_id)])
            if rows and rows[0].get("responded_at") and _ts(rows[0]["responded_at"]) > when:
                return self._response(rows[0], participant_id)
            if not rows:
                role, email = self._participant_role(tenant, session_row, participant_id)
                if not email:  # no address: the reply is kept on the session (see _ensure_participant)
                    self._note_participant_without_email(tenant, session_row, participant_id, role)
                    return self._session_response(tenant, session_id, participant_id, pid, response, when,
                                                  proposed_new_time, note)
                try:
                    self.client.insert("session_participants", [{
                        "participant_id": pid, "session_id": session_id, "actor_id": participant_id, "email": email,
                        "role": m.to_db(m.PARTICIPANT_ROLE, role), **values}])
                    return ParticipantResponse(id=pid, session_id=session_id, participant_id=participant_id,
                                               response=response, responded_at=when)
                except UniqueViolation:
                    continue
            previous = rows[0].get("responded_at")
            guard = ("responded_at", "is.null") if previous is None else _eq("responded_at", previous)
            if self.client.update("session_participants", values, filters=[
                    _eq("participant_id", pid), _eq("session_id", session_id), guard]):
                return ParticipantResponse(id=pid, session_id=session_id, participant_id=participant_id,
                                           response=response, responded_at=when)
        raise ConcurrentUpdateError(f"RSVP for {participant_id} kept changing concurrently")

    def _session_response(self, tenant: str, session_id: str, participant_id: str, pid: str, response: ResponseStatus,
                          when: datetime, proposed_new_time: datetime | None, note: str | None) -> ParticipantResponse:
        """The reply of a participant without a session_participants row, newest-wins on the session's metadata (a
        compare-and-set on the session row, like every other session change)."""
        kept: dict[str, Any] = {}

        def change(row: dict[str, Any]) -> dict[str, Any]:
            meta = dict(row.get("session_metadata") or {})
            responses = dict(meta.get(SESSION_RESPONSES) or {})
            previous = responses.get(participant_id)
            if previous and _ts(previous["responded_at"]) > when:
                kept.update(previous)
                return {}
            entry = {"response": response.value, "responded_at": _iso(when),
                     "proposed_new_time": _iso(proposed_new_time) if proposed_new_time else None,
                     "response_note": (note or None) and note[:500]}
            kept.update(entry)
            responses[participant_id] = entry
            return {"session_metadata": {**meta, SESSION_RESPONSES: responses}}

        self._patch_session(tenant, session_id, change)
        return ParticipantResponse(id=pid, session_id=session_id, participant_id=participant_id,
                                   response=ResponseStatus(kept["response"]), responded_at=_ts(kept["responded_at"]))

    @staticmethod
    def _response(row: dict[str, Any], participant_id: str | None = None) -> ParticipantResponse:
        return ParticipantResponse(id=row["participant_id"], session_id=row["session_id"],
                                   participant_id=participant_id or row["actor_id"],
                                   response=ResponseStatus(m.from_db(m.RSVP_STATUS, row["rsvp_status"])),
                                   responded_at=_ts(row["responded_at"]))

    def list_participant_responses(self, session_id: str) -> list[ParticipantResponse]:
        tenant = self._scope(None, f"session {session_id}")
        self._session_row(tenant, session_id)
        session_row = self._session_row(tenant, session_id)
        rows = self.client.select("session_participants", filters=[_eq("session_id", session_id)])
        replies = [self._response(r) for r in rows if r.get("responded_at") and r.get("rsvp_status")]
        for participant_id, entry in ((session_row.get("session_metadata") or {}).get(SESSION_RESPONSES) or {}).items():
            replies.append(ParticipantResponse(id=m.stable_uuid("participant", session_id, participant_id),
                                               session_id=session_id, participant_id=participant_id,
                                               response=ResponseStatus(entry["response"]),
                                               responded_at=_ts(entry["responded_at"])))
        return sorted(replies, key=lambda r: r.id)

    def list_session_participants(self, session_id: str) -> list[dict[str, Any]]:
        """The session's participants: its session_participants rows, then the participants recorded on the session
        because UC05 holds no e-mail address for them (`email_unknown`, their reply from the session metadata)."""
        tenant = self._scope(None, f"session {session_id}")
        meta = self._session_row(tenant, session_id).get("session_metadata") or {}
        rows = self.client.select("session_participants", filters=[_eq("session_id", session_id)],
                                  order="participant_id.asc")
        replies = meta.get(SESSION_RESPONSES) or {}
        for participant_id, noted in sorted((meta.get(PARTICIPANTS_WITHOUT_EMAIL) or {}).items()):
            reply = replies.get(participant_id) or {}
            rows.append({"participant_id": m.stable_uuid("participant", session_id, participant_id),
                         "session_id": session_id, "actor_id": participant_id, "email": None, "role": noted.get("role"),
                         "rsvp_status": m.to_db(m.RSVP_STATUS, reply["response"]) if reply else "PENDING",
                         "responded_at": reply.get("responded_at"), "email_unknown": True})
        return rows

    # ================================================================== client configuration and calendar
    def _client_row(self, tenant: str) -> dict[str, Any]:
        rows = self.client.select("client_configs", filters=[_eq("client_id", tenant)])
        if not rows:
            raise MissingClientConfigError("no client configuration exists for this client")
        if rows[0].get("is_active") is False:
            raise MissingClientConfigError("this client's configuration is inactive")
        return rows[0]

    def get_client_config(self, client_id: str) -> ClientConfig:
        """The tenant's welcome configuration: client_configs.company_name plus the
        settings_payload['uc05_welcome'] section (languages, branding, templates). Nothing is defaulted:
        no section means no welcome configuration (MissingClientConfigError)."""
        tenant = self._scope(client_id, "client configuration")
        row = self._client_row(tenant)
        section = (row.get("settings_payload") or {}).get("uc05_welcome")
        if not section:
            raise MissingClientConfigError("no welcome configuration (settings_payload.uc05_welcome) is set up for "
                                           "this client")
        try:
            return ClientConfig.model_validate({**section, "client_id": tenant, "name": row.get("company_name") or tenant})
        except ValidationError as exc:
            raise InvalidClientConfigError(f"this client's welcome configuration is invalid "
                                           f"({exc.error_count()} problem(s))") from None

    def get_client_calendar(self, client_id: str, *, on: date | None = None) -> ClientCalendar:
        """Working days, timezone and observed holidays, only while an approved configuration
        version is effective on `on` (default: today, UTC). Never a default calendar."""
        tenant = self._scope(client_id, "client calendar")
        try:
            row = self._client_row(tenant)
        except MissingClientConfigError as exc:
            raise ClientCalendarUnavailableError(str(exc)) from None
        day = on or self._now().date()
        versions = [v for v in self.client.select("client_config_versions", filters=[_eq("client_id", tenant)])
                    if str(v.get("status") or "").lower() in m.EFFECTIVE_VERSION_STATUSES
                    and v.get("effective_from") and _date(v["effective_from"]) <= day
                    and (not v.get("effective_to") or _date(v["effective_to"]) >= day)]
        if not versions:
            raise ClientCalendarUnavailableError(f"no approved configuration version is effective on {day} for "
                                                 f"this client")
        tz = str(row.get("timezone") or "")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ClientCalendarUnavailableError("this client's configured timezone is missing or invalid") from None
        working = sorted({int(d) for d in (row.get("working_days") or [])})
        if not working or any(d < 1 or d > 7 for d in working):
            raise ClientCalendarUnavailableError("this client's working_days are missing or outside 1..7")
        holidays = [_date(h["holiday_date"]) for h in self.client.select("client_holidays", filters=[
            _eq("client_id", tenant)]) if h.get("is_observed") is not False]
        return ClientCalendar(client_id=tenant, timezone=tz, working_days=working, holidays=sorted(set(holidays)),
                              config_version=max(int(v["version"]) for v in versions))

    def list_client_ids(self) -> list[str]:
        """Active tenants (client_configs), for the joining-day scheduler. Not tenant data."""
        return sorted(r["client_id"] for r in self.client.select("client_configs", columns="client_id,is_active")
                      if r.get("is_active") is not False)

    # ================================================================== communications
    # communications holds the message; its comm_type/status vocabularies are coarser than UC05's, so
    # each UC05 draft also has a versioned UC05_COMMUNICATION_STATE record (audit_events) with its
    # template, language and exact status. A row is UC05's only if that record exists.
    def _comm_states(self, tenant: str, *, case_id: str | None = None,
                     comm_id: str | None = None) -> dict[str, dict[str, Any]]:
        return {k: body for k, (_, body) in self._latest_states(
            tenant, m.ACTION_COMMUNICATION_STATE, case_id=case_id, entity_id=comm_id).items()}

    def _draft(self, tenant: str, row: dict[str, Any], state: dict[str, Any]) -> CommunicationDraft:
        return CommunicationDraft(
            id=row["comm_id"], employee_id=state.get("employee_id") or self._employee_for_case(tenant, row["case_id"]),
            client_id=row["client_id"], template_id=state["template_id"], language=state["language"],
            subject=row.get("subject") or "", body=row.get("body_text") or "",
            status=CommunicationStatus(state["status"]),
            created_at=_ts(row["created_at"]) if row.get("created_at") else self._now())

    def _comm(self, tenant: str, draft_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not m.is_uuid(draft_id):
            raise KeyError(draft_id)
        rows = self.client.select("communications", filters=[_eq("comm_id", draft_id), _eq("client_id", tenant)])
        state = self._comm_states(tenant, comm_id=draft_id).get(draft_id)
        if not rows or state is None:
            raise KeyError(draft_id)
        return rows[0], state

    def create_communication_draft(self, draft: CommunicationDraft) -> CommunicationDraft:
        tenant = self._scope(draft.client_id, f"employee {draft.employee_id}")
        if draft.template_id not in m.COMM_TYPE:
            raise ValueError(f"template '{draft.template_id}' has no communications.comm_type mapping")
        config = self.get_client_config(tenant)
        if draft.language not in config.enabled_languages:
            raise UnsupportedLanguageError(f"language '{draft.language}' is not enabled for {draft.client_id} "
                                           f"(enabled: {config.enabled_languages})")
        case = self._case_row(tenant, draft.employee_id)
        live = [d for d in self.list_communication_drafts(draft.employee_id)
                if d.template_id == draft.template_id and d.status != CommunicationStatus.REJECTED]
        if live:
            raise DuplicateCommunicationError(f"a '{draft.template_id}' communication already exists for "
                                              f"{draft.employee_id} ({live[-1].status.value})")
        comm_id = m.stable_uuid("communication", tenant, draft.id)
        state = {"template_id": draft.template_id, "language": draft.language, "status": draft.status.value,
                 "employee_id": draft.employee_id, "delivery": None}
        try:  # the UC05 record first: a row without it is never read back as UC05's
            self._save_state(tenant, m.ACTION_COMMUNICATION_STATE, comm_id, state, version=1, case_id=case["case_id"])
            self.client.insert("communications", [{
                "comm_id": comm_id, "case_id": case["case_id"], "client_id": tenant,
                "comm_type": m.COMM_TYPE[draft.template_id], "recipient_email": case.get("candidate_email"),
                "subject": draft.subject, "body_text": draft.body, "body_html": None,
                "status": m.to_db(m.COMM_STATUS, draft.status.value), "review_gate_id": None, "dispatched_at": None,
                "created_at": _iso(draft.created_at)}])
        except UniqueViolation:
            raise DuplicateCommunicationError(f"communication {draft.id} already exists") from None
        return draft.model_copy(update={"id": comm_id}, deep=True)

    def get_communication_draft(self, draft_id: str, *, client_id: str | None = None) -> CommunicationDraft:
        tenant = self._scope(client_id, f"communication {draft_id}")
        return self._draft(tenant, *self._comm(tenant, draft_id))

    def list_communication_drafts(self, employee_id: str) -> list[CommunicationDraft]:
        tenant = self._scope(None, f"employee {employee_id}")
        case_id = self.case_id_for(employee_id, tenant)
        states = self._comm_states(tenant, case_id=case_id)
        if not states:
            return []
        rows = self.client.select("communications", order="created_at.asc,comm_id.asc", filters=[
            _eq("client_id", tenant), _eq("case_id", case_id), ("comm_id", in_list(states))])
        return [self._draft(tenant, r, states[r["comm_id"]]) for r in rows]

    def update_communication_status(self, draft_id: str, status: CommunicationStatus) -> CommunicationDraft:
        """UC05's exact status goes to its record; the row gets the closest truthful value. SENT becomes
        DISPATCHED + dispatched_at ONLY when the delivery ledger shows a real (non-simulated) provider
        acceptance; a simulated delivery leaves the row APPROVED -- the table never claims an email
        left the system when it did not."""
        tenant = self._scope(None, f"communication {draft_id}")
        row, state = self._comm(tenant, draft_id)
        values: dict[str, Any] = {"status": m.to_db(m.COMM_STATUS, status.value)}
        delivery = state.get("delivery")
        if status == CommunicationStatus.SENT:
            op = self.get_operation(f"welcome:{tenant}:{draft_id}")
            if op is not None and op.status == "succeeded" and op.detail.get("simulated") is False:
                delivery = "delivered"
                values["dispatched_at"] = op.detail.get("accepted_at") or _iso(self._now())
            else:
                delivery = "simulated"
                values["status"] = m.COMM_STATUS["approved"]
        elif status == CommunicationStatus.DRAFT and row.get("review_gate_id"):
            values["status"] = m.COMM_AWAITING_REVIEW
        self._upsert_state(tenant, m.ACTION_COMMUNICATION_STATE, draft_id,
                           lambda cur: {**(cur or state), "status": status.value, "delivery": delivery},
                           case_id=row["case_id"])
        rows = self.client.update("communications", values, filters=[_eq("comm_id", draft_id), _eq("client_id", tenant)])
        if not rows:
            raise KeyError(draft_id)
        return self._draft(tenant, rows[0], {**state, "status": status.value})

    def link_review_gate(self, draft_id: str, approval_id: str, *, client_id: str) -> None:
        """communications.review_gate_id = the approval request gating this draft; a DRAFT row becomes
        AWAITING_REVIEW (HR's review has been requested)."""
        tenant = self._scope(client_id, f"communication {draft_id}")
        if not m.is_uuid(draft_id):
            return
        filters = [_eq("comm_id", draft_id), _eq("client_id", tenant)]
        self.client.update("communications", {"review_gate_id": approval_id}, filters=filters)
        self.client.update("communications", {"status": m.COMM_AWAITING_REVIEW},
                           filters=[*filters, _eq("status", m.COMM_STATUS["draft"])])

    # ================================================================== orchestration state (read-only)
    def get_orchestration_state(self, case_id: str, *, client_id: str | None = None) -> dict[str, Any] | None:
        """The orchestrator's state row for a case, read-only: UC05 never writes it (the orchestrator
        records UC05's outcome from the structured result UC05 returns)."""
        tenant = self._scope(client_id, f"case {case_id}")
        rows = self.client.select("case_orchestration_state", filters=[_eq("case_id", case_id), _eq("client_id", tenant)])
        return rows[0] if rows else None
