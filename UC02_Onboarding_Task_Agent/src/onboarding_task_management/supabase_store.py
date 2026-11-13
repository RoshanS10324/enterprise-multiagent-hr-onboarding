"""``TableClient`` over the live shared Supabase tables, by explicit mapping (no schema change).

``OnboardingStore`` keeps speaking UC-02's own table and column names; this adapter translates each
row to and from the live table, so business logic is untouched.

Ownership rules the mapping enforces
------------------------------------
* ``onboarding_cases`` belongs to UC-01 (it requires candidate name and email). UC-02 never inserts
  or updates it. A UC-02 onboarding is an *existing* case: ``onboarding_id`` IS the case uuid, and a
  save for a case that does not exist for that client is refused (:class:`CaseNotFoundError`); a case
  that exists under another client is an isolation breach (``DataIsolationError``). (The orchestrator
  additionally checks the case's ``employee_no`` against the workflow's employee before it calls UC-02.)
* UC-02's own case state (plan and final HR status, plan history, idempotency ledger, revision, config
  version, correlation id and any value UC-02 changed such as joining date) has no live column. It is
  kept as an append-only ``UC02_CASE_STATE`` snapshot event in ``audit_events`` (``after_state`` jsonb);
  the latest snapshot overlays the case row on read. "Latest" is decided by ``snapshot_seq``, a counter
  this adapter increments per case, never by a timestamp (two snapshots written within the clock's
  resolution used to tie, and a restart then resumed from the OLDER one). Snapshots written before the
  counter existed count as 0 and are ordered by ``updated_at`` / ``occurred_at`` as before.
* Task fields with no live column are kept in ``onboarding_tasks.status_summary['uc02']``; nothing is
  dropped.
* Rows written by other workflows are never read back as UC-02 data (approvals are filtered by
  ``workflow_module``; audit by ``reference``).

Tenant isolation (the service-role key bypasses Row Level Security, so these filters ARE the isolation)
---------------------------------------------------------------------------------------------------
Every read and write of planning data carries ``client_id`` next to ``case_id`` / ``task_id``. The tenant
of a case is the ``client_id`` of its ``onboarding_cases`` row; a task's is that of its own row. A
dependency edge between tasks of two clients, or of two cases, is refused. Nothing here trusts a
``case_id`` or ``task_id`` alone.

Plan revisions (``plan_revisions``)
-----------------------------------
In the shared table ``revision`` is the PLAN VERSION the orchestrator and the Control Center show
(``onboarding_plan.plan_version``): revision 1 is the initial plan, every replan adds the next one, and
earlier rows are never changed. Each row is projected from the record's append-only ``plan_events``
(``proposed`` / ``replanned`` / ``replanned_documents`` / ``revised``) the first time a save carries that
version, insert-if-missing by ``(case_id, revision)`` -- a replayed save adds nothing. The approved-change
path also writes its drivers and task ids through ``save_plan_revision``; that row is the same version's
row (UC-02's own task-replan counter is kept in ``change_summary.task_revision``). The in-memory and
SQLite stores keep their original local numbering; this mapping applies to the shared database only.

Write order (restart safety)
----------------------------
``OnboardingStore`` saves a record as one ``transaction()``: case state, tasks, dependencies. PostgREST
has no multi-request transaction, so this client makes the case-state snapshot the COMMIT MARKER: it is
validated first (the case must exist for the tenant) but written LAST, after every task and dependency
row, together with any new plan-revision rows. A failure part-way leaves the previous snapshot in force
and the next save rewrites the task rows wholesale; a restart never sees a new plan version (or a
replayed-feedback ledger entry) whose tasks were not stored.

Not verified against the live database: CHECK constraints, foreign keys and RLS are not visible through
PostgREST. See ``docs/UC02_SUPABASE_DATABASE_MAPPING.md``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from onboarding_task_management.audit import SHARED_DB_FORBIDDEN_DETAIL_KEYS
from onboarding_task_management.errors import DataIsolationError
from onboarding_task_management.persistence import (
    FORBIDDEN_DETAIL_KEYS,
    TABLE_AGENT_RUN,
    TABLE_APPROVAL_REQUEST,
    TABLE_AUDIT_EVENT,
    TABLE_CLIENT_CONFIG_REF,
    TABLE_ONBOARDING,
    TABLE_PLAN_REVISION,
    TABLE_TASK,
    TABLE_TASK_DEPENDENCY,
    PersistenceError,
)
from onboarding_task_management.supabase_rest import SupabaseRestClient

WORKFLOW_MODULE = "uc02_onboarding_task_management"
STATE_ACTION = "UC02_CASE_STATE"
REF_PREFIX = "uc02:"
SNAPSHOT_SEQ = "snapshot_seq"
_NS = uuid.UUID("5a1f6c0e-3b7d-4c52-9d0e-0c02a11d0c02")

_CASE_READ = ("case_id,client_id,employee_no,joining_date,location_name,project_name,role_id,grade,"
              "department,manager_id,nationality_group,onboarding_attributes,is_deleted")

_TASK_DIRECT = ("task_id", "template_key", "client_id", "employee_id", "category", "title", "title_ar",
                "status", "due_date", "started_at", "completed_at", "cancelled_at", "notes",
                "evidence_required", "evidence_ref", "evidence_recorded_at", "approval_role",
                "requires_attributes", "priority", "planned_start_date", "plan_order", "sensitive_to",
                "assigned_by", "assigned_at", "external_outcome", "external_source",
                "external_reference", "external_blocking_reason", "external_updated_at")
_TASK_RENAMED = {"onboarding_id": "case_id", "owner": "owner_role",
                 "assignee_id": "assigned_actor_id", "assignee_team": "assigned_team"}
_TASK_EXTRAS_KEY = "uc02"

#: Plan events that create a plan version, and what triggered each (``plan_revisions.change_summary``).
VERSION_EVENTS = {
    "proposed": "initial_planning",
    "replanned": "approved_change",
    "replanned_documents": "document_validation_feedback",
    "revised": "hr_plan_revision",
}


def _uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise PersistenceError(f"{what} must be a case uuid in supabase mode", value=str(value)) from None


#: Values ``audit_events.actor_type`` allows (live CHECK constraint, verified 2026-11-02).
ACTOR_TYPES = frozenset({"HUMAN", "AGENT", "SYSTEM"})


def actor_type_for(actor_role: str) -> str:
    """Explicit mapping from a UC-02 actor role to the live ``actor_type``.

    The ``system`` role is a service identity, so ``SYSTEM``; every other role is a person acting
    through UC-02, so ``HUMAN``. ``AGENT`` is never inferred: UC-02 audit events do not say whether
    the agent loop or a person acted, so a deliberate rule must be agreed before using it.
    """
    return "SYSTEM" if actor_role == "system" else "HUMAN"


def _event_uuid(event_id: str) -> str:
    return str(uuid.uuid5(_NS, event_id))


def _snapshot_key(state: dict[str, Any], row: dict[str, Any]) -> tuple[int, str, str]:
    """Order of two snapshots of one case: the explicit counter first, timestamps only for older rows."""
    return int(state.get(SNAPSHOT_SEQ) or 0), str(state.get("updated_at") or ""), str(row.get("occurred_at") or "")


class OwnershipConflictError(PersistenceError):
    """A UC-02 write would touch a row another use case owns. Nothing is written or removed."""

    code = "POLICY_BREACH"


class CaseNotFoundError(PersistenceError):
    """The UC-01 case UC-02 must plan for does not exist (or is deleted) for this client.

    UC-02 never creates a case: the onboarding waits until UC-01 has written its ``onboarding_cases`` row.
    """

    code = "MISSING_DATA"


class SupabaseTableClient:
    """Implements ``persistence.TableClient`` against the live schema.

    Ownership: a task row is UC-02's only if ``status_summary`` carries the ``uc02`` marker UC-02
    writes. Rows without it (UC-01, the orchestrator, other use cases) are never read into a UC-02
    record, never overwritten and never deleted; a clash on ``task_id`` or ``template_key`` stops the
    write with :class:`OwnershipConflictError`.
    """

    def __init__(self, rest: SupabaseRestClient) -> None:
        self.rest = rest
        #: ``case_id -> config_version`` of the plan last saved, so every task row carries the version that generated it.
        self._config_versions: dict[str, str] = {}
        #: ``case_id -> client_id`` as ``onboarding_cases`` holds it (the tenant of every planning row of the case).
        self._case_tenants: dict[str, str] = {}
        #: ``task_id -> (client_id, case_id)`` of rows read or written, so edges and deletes are tenant-checked.
        self._task_owners: dict[str, tuple[str, str]] = {}
        #: ``case_id -> plan_version`` of the record last saved (the shared ``plan_revisions.revision``).
        self._plan_versions: dict[str, int] = {}
        #: ``case_id -> plan versions`` known to have a ``plan_revisions`` row.
        self._revisions_known: dict[str, set[int]] = {}
        #: ``case_id -> the record state`` of the last save in this process (its plan events and ledger).
        self._states: dict[str, dict[str, Any]] = {}
        #: Writes deferred to the end of the outermost ``transaction()`` (the case-state commit marker).
        self._depth = 0
        self._deferred: list[tuple[str, dict[str, Any]]] = []
        #: ``case_id -> state`` of the newest snapshot this client wrote (committed) or holds for commit (pending).
        self._latest: dict[str, dict[str, Any]] = {}
        self._latest_pending: dict[str, dict[str, Any]] = {}
        #: Tells the service to use the UC-01 case uuid as ``onboarding_id``.
        self.uses_case_ids = True

    # -- transaction (commit-marker ordering, see the module docstring) -------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._depth += 1
        try:
            yield
        except BaseException:
            if self._depth == 1:
                # The group failed: its commit marker is never written, and nothing it held counts as stored.
                self._deferred.clear()
                self._latest_pending.clear()
            raise
        finally:
            self._depth -= 1
        if self._depth == 0:
            self._flush()

    def _flush(self) -> None:
        pending, self._deferred = self._deferred, []
        try:
            for kind, payload in pending:
                if kind == "snapshot":
                    self.rest.insert("audit_events", [payload])
                    self._latest[str(payload["case_id"])] = payload["after_state"]
                else:
                    self._project_revisions(payload)
        finally:
            self._latest_pending.clear()

    def _defer(self, kind: str, payload: dict[str, Any]) -> None:
        if self._depth:
            if kind == "snapshot":
                self._latest_pending[str(payload["case_id"])] = payload["after_state"]
            self._deferred.append((kind, payload))
        elif kind == "snapshot":
            self.rest.insert("audit_events", [payload])
            self._latest[str(payload["case_id"])] = payload["after_state"]
        else:
            self._project_revisions(payload)

    def _newest_state(self, case_id: str, client_id: str) -> dict[str, Any] | None:
        """The newest snapshot of the case: the stored one, or the one this client wrote or holds for commit
        (a later save in the same group, or a database read that has not caught up)."""
        stored = self._snapshots({"case_id": case_id, "client_id": client_id}).get(case_id)
        candidates = [s for s in (stored, self._latest.get(case_id), self._latest_pending.get(case_id))
                      if s is not None and s.get("client_id") in (None, client_id)]
        return max(candidates, key=lambda s: int(s.get(SNAPSHOT_SEQ) or 0), default=None)

    # -- tenant resolution ---------------------------------------------------

    def _case_tenant(self, case_id: str) -> str | None:
        """The ``client_id`` of the case's ``onboarding_cases`` row, or None when there is no such case."""
        if case_id not in self._case_tenants:
            rows = self.rest.select("onboarding_cases", {"case_id": case_id}, columns="case_id,client_id")
            if not rows:
                return None
            self._case_tenants[case_id] = rows[0]["client_id"]
        return self._case_tenants[case_id]

    def _require_case_tenant(self, case_id: str, client_id: str) -> None:
        tenant = self._case_tenant(case_id)
        if tenant is None:
            raise CaseNotFoundError("no such onboarding case; UC-01 creates cases, UC-02 never does", case_id=case_id)
        if tenant != client_id:
            raise DataIsolationError("the onboarding case belongs to another client", case_id=case_id,
                                     actor_client_id=client_id)

    def _task_owner(self, task_id: str) -> tuple[str, str] | None:
        """``(client_id, case_id)`` of a stored task row, or None when there is no such task."""
        if task_id not in self._task_owners:
            rows = self.rest.select("onboarding_tasks", {"task_id": task_id}, columns="task_id,client_id,case_id")
            if not rows:
                return None
            self._task_owners[task_id] = (rows[0]["client_id"], str(rows[0]["case_id"]))
        return self._task_owners[task_id]

    # -- TableClient ---------------------------------------------------------

    def upsert(self, table: str, rows: Sequence[dict[str, Any]]) -> int:
        handler = {
            TABLE_ONBOARDING: self._save_onboarding,
            TABLE_TASK: self._save_tasks,
            TABLE_TASK_DEPENDENCY: self._save_dependencies,
            TABLE_PLAN_REVISION: self._save_revisions,
            TABLE_APPROVAL_REQUEST: self._save_approvals,
            TABLE_AUDIT_EVENT: self._save_audit,
            TABLE_AGENT_RUN: self._save_runs,
            TABLE_CLIENT_CONFIG_REF: lambda rows: len(rows),  # config is read, never written, here
        }.get(table)
        if handler is None:
            raise PersistenceError("unknown table", table=table)
        return handler(list(rows))

    def select(self, table: str, filters: dict[str, Any], order_by: str | None = None) -> list[dict[str, Any]]:
        reader = {
            TABLE_ONBOARDING: self._read_onboarding,
            TABLE_TASK: self._read_tasks,
            TABLE_TASK_DEPENDENCY: self._read_dependencies,
            TABLE_PLAN_REVISION: self._read_revisions,
            TABLE_APPROVAL_REQUEST: self._read_approvals,
            TABLE_AUDIT_EVENT: self._read_audit,
            TABLE_AGENT_RUN: self._read_runs,
            TABLE_CLIENT_CONFIG_REF: lambda f: [],
        }.get(table)
        if reader is None:
            raise PersistenceError("unknown table", table=table)
        rows = reader(dict(filters))
        if order_by:
            rows.sort(key=lambda r: (r.get(order_by) is None, r.get(order_by)))
        return rows

    def delete(self, table: str, filters: dict[str, Any]) -> int:
        """Only UC-02's own task and dependency rows may be removed (a re-plan dropping a task), and only in
        the task's own tenant."""
        if table == TABLE_TASK_DEPENDENCY:
            tid = filters["task_id"]
            owner = self._require_owned(tid, "delete dependencies of")
            if owner is None:
                return 0  # no such task: nothing of it can be stored
            return self.rest.delete("task_dependencies", {"task_id": tid, "client_id": owner[0]})
        if table == TABLE_TASK:
            tid = filters["task_id"]
            owner = self._require_owned(tid, "delete")
            if owner is None:
                return 0
            client_id = owner[0]
            # task_dependencies has a foreign key to onboarding_tasks on BOTH columns, so every edge that
            # mentions the task must go before the task itself. An edge from a task UC-02 does not own
            # is not ours to remove: stop instead.
            for edge in self.rest.select("task_dependencies", {"depends_on_task_id": tid}, columns="task_id,client_id"):
                if edge.get("client_id") not in (None, "", client_id):
                    raise OwnershipConflictError("a task of another client depends on this task", task_id=tid)
                self._require_owned(edge["task_id"], f"remove the dependency on {tid} held by")
            self.rest.delete("task_dependencies", {"depends_on_task_id": tid, "client_id": client_id})
            self.rest.delete("task_dependencies", {"task_id": tid, "client_id": client_id})
            removed = self.rest.delete("onboarding_tasks", {"task_id": tid, "client_id": client_id})
            self._task_owners.pop(tid, None)
            return removed
        raise PersistenceError("delete refused for this table", table=table)

    # -- onboarding cases (read-only on the live row) -------------------------

    def _snapshots(self, filters: dict[str, Any]) -> dict[str, dict[str, Any]]:
        query = {k: v for k, v in filters.items() if k in ("case_id", "client_id")}
        if "client_id" not in query and "case_id" in query:
            tenant = self._case_tenant(query["case_id"])
            if tenant is None:
                return {}
            query["client_id"] = tenant
        rows = self.rest.select("audit_events", {**query, "action": STATE_ACTION},
                                order_by="occurred_at.desc")
        latest: dict[str, dict[str, Any]] = {}
        keys: dict[str, tuple[int, str, str]] = {}
        for row in rows:
            state = row.get("after_state") or {}
            case = str(row["case_id"])
            key = _snapshot_key(state, row)
            if case not in latest or key > keys[case]:
                latest[case], keys[case] = state, key
        return latest

    def _save_onboarding(self, rows: list[dict[str, Any]]) -> int:
        for row in rows:
            case_id = _uuid(row["onboarding_id"], "onboarding_id")
            client_id = row["client_id"]
            found = self.rest.select("onboarding_cases", {"case_id": case_id, "client_id": client_id},
                                     columns="case_id,client_id,is_deleted")
            if not found:
                if self._case_tenant(case_id) not in (None, client_id):
                    raise DataIsolationError("the onboarding case belongs to another client", case_id=case_id,
                                             actor_client_id=client_id)
                raise CaseNotFoundError(
                    "no such onboarding case for this client; UC-01 creates cases, UC-02 never does",
                    case_id=case_id,
                )
            if found[0].get("is_deleted"):
                raise CaseNotFoundError("the onboarding case is deleted; UC-02 does not plan for it", case_id=case_id)
            self._case_tenants[case_id] = client_id
            self._config_versions[case_id] = str(row.get("config_version") or "")
            self._plan_versions[case_id] = int(row.get("plan_version") or 1)
            state = {k: v for k, v in row.items() if k != "config_hash"}
            state["config_hash"] = row.get("config_hash")
            state = json.loads(json.dumps(state))
            self._states[case_id] = state
            previous = self._newest_state(case_id, client_id)
            if previous is not None and {k: v for k, v in previous.items() if k != SNAPSHOT_SEQ} == state:
                self._defer("revisions", {"case_id": case_id, "row": state})  # heals a missing revision row
                continue  # unchanged: idempotent, no new event
            state[SNAPSHOT_SEQ] = int((previous or {}).get(SNAPSHOT_SEQ) or 0) + 1
            self._defer("snapshot", {
                "event_id": str(uuid.uuid4()), "case_id": case_id, "client_id": client_id,
                "actor_type": "SYSTEM", "actor_id": "uc02", "actor_role": "system",
                "action": STATE_ACTION, "outcome": "SNAPSHOT", "detail": {},
                "after_state": state, "reference": REF_PREFIX + "state",
                "correlation_id": row.get("correlation_id"),
            })
            self._defer("revisions", {"case_id": case_id, "row": state})
        return len(rows)

    def _read_onboarding(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if "onboarding_id" in filters:
            try:
                query["case_id"] = str(uuid.UUID(str(filters["onboarding_id"])))
            except ValueError:
                return []  # not a case uuid, so not a case
        if "client_id" in filters:
            query["client_id"] = filters["client_id"]
        if "employee_id" in filters:
            query["employee_no"] = filters["employee_id"]
        cases = [c for c in self.rest.select("onboarding_cases", query, columns=_CASE_READ)
                 if not c.get("is_deleted")]  # a soft-deleted case is not an onboarding UC-02 may touch
        out = []
        for case in cases:
            self._case_tenants[str(case["case_id"])] = case["client_id"]
        snaps = self._snapshots(query) if ("client_id" in query or "case_id" in query) else {}
        for case in cases:
            state = snaps.get(str(case["case_id"]))
            if state is not None and state.get("client_id") not in (None, case["client_id"]):
                continue  # a snapshot of another tenant never overlays this case
            if state is None and "onboarding_id" not in filters:
                continue  # a UC-01 case UC-02 has not planned yet
            row = {
                "onboarding_id": str(case["case_id"]), "client_id": case["client_id"],
                "employee_id": case["employee_no"], "joining_date": case["joining_date"],
                "location": case["location_name"], "project": case["project_name"],
                "role_id": case["role_id"], "grade": case["grade"],
                "nationality_group": case["nationality_group"], "department": case["department"],
                "manager_id": case["manager_id"], "attributes": case["onboarding_attributes"],
            }
            row.update(state or {})
            row.pop(SNAPSHOT_SEQ, None)
            out.append(row)
        return out

    # -- tasks ------------------------------------------------------------------

    def _require_owned(self, task_id: str, verb: str) -> tuple[str, str] | None:
        """The task's ``(client_id, case_id)`` when UC-02 owns it; None when there is no such task. A task
        another use case owns (no ``uc02`` marker) stops the operation."""
        found = self.rest.select("onboarding_tasks", {"task_id": task_id},
                                 columns="task_id,client_id,case_id,status_summary")
        if not found:
            return None
        if _TASK_EXTRAS_KEY not in (found[0].get("status_summary") or {}):
            raise OwnershipConflictError(f"refusing to {verb} a task UC-02 does not own", task_id=task_id)
        owner = (found[0]["client_id"], str(found[0]["case_id"]))
        self._task_owners[task_id] = owner
        return owner

    def _save_tasks(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        case_id = _uuid(rows[0]["onboarding_id"], "onboarding_id")
        client_id = rows[0]["client_id"]
        if any(_uuid(r["onboarding_id"], "onboarding_id") != case_id or r["client_id"] != client_id for r in rows):
            raise PersistenceError("one task write must belong to one case and one client", case_id=case_id)
        self._require_case_tenant(case_id, client_id)
        stored = self.rest.select("onboarding_tasks", {"case_id": case_id},
                                  columns="task_id,template_key,client_id,status_summary")
        existing = {r["task_id"]: r.get("status_summary") or {} for r in stored}
        # Ours: the UC-02 marker AND this case's tenant. A row of the same case id under another client is not ours.
        owned = {r["task_id"] for r in stored
                 if _TASK_EXTRAS_KEY in (r.get("status_summary") or {}) and r.get("client_id") == client_id}
        clashes = sorted(
            {f"task_id={r['task_id']}" for r in rows if r["task_id"] in existing and r["task_id"] not in owned}
            | {f"template_key={r['template_key']}" for r in rows for s in stored
               if s["template_key"] == r["template_key"] and s["task_id"] not in owned}
            | {f"template_key={r['template_key']}" for r in rows for s in stored
               if s["template_key"] == r["template_key"] and s["task_id"] != r["task_id"]})
        if clashes:
            raise OwnershipConflictError(
                "UC-02 tasks clash with tasks another use case owns; nothing was written",
                conflicts=", ".join(clashes))
        live_rows = []
        for row in rows:
            live = {c: row[c] for c in _TASK_DIRECT if c in row}
            for ours, theirs in _TASK_RENAMED.items():
                live[theirs] = row.get(ours)
            live["case_id"] = case_id
            known = set(_TASK_DIRECT) | set(_TASK_RENAMED)
            extras = {k: v for k, v in row.items() if k not in known}
            summary = dict(existing.get(row["task_id"], {}))
            summary[_TASK_EXTRAS_KEY] = extras
            live["status_summary"] = summary
            live["assigned_team"] = live["assigned_team"] or ""
            live["config_version"] = self._task_config_version(case_id, client_id)
            live_rows.append(live)
        self.rest.upsert("onboarding_tasks", live_rows, on_conflict="task_id")
        for live in live_rows:
            self._task_owners[live["task_id"]] = (client_id, case_id)
        return len(rows)

    def _task_config_version(self, case_id: str, client_id: str) -> int | None:
        """The numeric configuration version of the case's plan (``onboarding_tasks.config_version`` is an integer column)."""
        version = self._config_versions.get(case_id)
        if version is None:
            snapshot = self._snapshots({"case_id": case_id, "client_id": client_id}).get(case_id) or {}
            version = str(snapshot.get("config_version") or "")
            self._config_versions[case_id] = version
        return int(version) if version.isdigit() else None

    def _read_tasks(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if "onboarding_id" in filters:
            query["case_id"] = _uuid(filters["onboarding_id"], "onboarding_id")
            tenant = filters.get("client_id") or self._case_tenant(query["case_id"])
            if tenant is None:
                return []  # no such case: it has no tasks
            query["client_id"] = tenant
        if "task_id" in filters:
            query["task_id"] = filters["task_id"]
            if "client_id" not in query:
                owner = filters.get("client_id") or (self._task_owner(filters["task_id"]) or (None,))[0]
                if owner is None:
                    return []
                query["client_id"] = owner
        if "client_id" not in query:
            raise PersistenceError("a task read needs a case or a task to scope it to its client")
        out = []
        for live in self.rest.select("onboarding_tasks", query, order_by="plan_order"):
            if _TASK_EXTRAS_KEY not in (live.get("status_summary") or {}):
                continue  # another use case's task: never part of a UC-02 record
            row = {c: live.get(c) for c in _TASK_DIRECT}
            for ours, theirs in _TASK_RENAMED.items():
                row[ours] = live.get(theirs)
            row["onboarding_id"] = str(live["case_id"])
            row["assignee_team"] = row["assignee_team"] or ""
            row.update(((live.get("status_summary") or {}).get(_TASK_EXTRAS_KEY)) or {})
            self._task_owners[row["task_id"]] = (live["client_id"], str(live["case_id"]))
            out.append(row)
        return out

    # -- dependencies -----------------------------------------------------------

    def _save_dependencies(self, rows: list[dict[str, Any]]) -> int:
        live_rows = []
        for row in rows:
            tid, on = row["task_id"], row["depends_on_id"]
            owner, prerequisite = self._task_owner(tid), self._task_owner(on)
            if owner is None:
                raise PersistenceError("dependency for a task that is not stored", task_id=tid)
            if prerequisite is None:
                raise PersistenceError("dependency on a task that is not stored", task_id=tid, depends_on=on)
            if owner[0] != prerequisite[0]:
                raise DataIsolationError("a dependency must not link tasks of two clients", task_id=tid, depends_on=on)
            if owner[1] != prerequisite[1]:
                raise PersistenceError("a dependency must not link tasks of two onboarding cases", task_id=tid,
                                       depends_on=on)
            live_rows.append({"task_id": tid, "depends_on_task_id": on, "client_id": owner[0]})
        self.rest.upsert("task_dependencies", live_rows, on_conflict="task_id,depends_on_task_id")
        return len(rows)

    def _read_dependencies(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        tid = filters["task_id"]
        client_id = filters.get("client_id") or (self._task_owner(tid) or (None,))[0]
        if client_id is None:
            return []
        live = self.rest.select("task_dependencies", {"task_id": tid, "client_id": client_id},
                                order_by="depends_on_task_id")
        return [{"task_id": r["task_id"], "depends_on_id": r["depends_on_task_id"]} for r in live]

    # -- plan revisions -----------------------------------------------------------

    def _save_revisions(self, rows: list[dict[str, Any]]) -> int:
        """The approved-change path's revision row (drivers, task ids, approval). Stored as the CURRENT plan
        version of the case (see the module docstring); UC-02's own counter goes to change_summary."""
        for row in rows:
            case_id = _uuid(row["onboarding_id"], "onboarding_id")
            client_id = row["client_id"]
            self._require_case_tenant(case_id, client_id)
            record = self._states.get(case_id) or self._newest_state(case_id, client_id) or {}
            revision = int(self._plan_versions.get(case_id) or record.get("plan_version") or row["revision"])
            event = self._version_events(record).get(revision)
            base = self._projected_row(case_id, record, revision, event) if event is not None and record else {}
            live = {**base, "case_id": case_id, "revision": revision, "client_id": client_id,
                    "drivers": row["drivers"], "added_task_ids": row["added_task_ids"],
                    "removed_task_ids": row["removed_task_ids"],
                    "rescheduled_task_ids": row["rescheduled_task_ids"],
                    "retained_task_ids": row["retained_task_ids"], "applied_by": row["applied_by"],
                    "approval_request_id": row.get("approval_request_id")}
            existing = self.rest.select("plan_revisions", {"case_id": case_id, "client_id": client_id,
                                                           "revision": revision}, columns="revision_id,change_summary")
            summary = {**(base.get("change_summary") or {}),
                       **((existing[0].get("change_summary") or {}) if existing else {})}
            summary["task_revision"] = row["revision"]
            live["change_summary"] = summary
            if existing:
                self.rest.update("plan_revisions", {"case_id": case_id, "client_id": client_id,
                                                    "revision": revision}, live)
            else:
                self._insert_revision(live)
            self._revisions_known.setdefault(case_id, set()).add(revision)
        return len(rows)

    def _insert_revision(self, live: dict[str, Any]) -> None:
        """Insert-if-missing by (case_id, revision): a concurrent writer that inserted it first wins."""
        try:
            self.rest.insert("plan_revisions", [live])
        except PersistenceError as exc:
            if exc.context.get("status") != 409:
                raise
            if not self.rest.select("plan_revisions", {"case_id": live["case_id"], "client_id": live["client_id"],
                                                       "revision": live["revision"]}, columns="revision_id"):
                raise

    @staticmethod
    def _version_events(row: dict[str, Any]) -> dict[int, dict[str, Any]]:
        """The first version-creating plan event of each plan version of a record."""
        firsts: dict[int, dict[str, Any]] = {}
        for event in row.get("plan_events") or []:
            version = int(event.get("version") or 0)
            if event.get("action") in VERSION_EVENTS and version >= 1 and version not in firsts:
                firsts[version] = event
        return firsts

    @staticmethod
    def _projected_row(case_id: str, row: dict[str, Any], version: int, event: dict[str, Any]) -> dict[str, Any]:
        """The ``plan_revisions`` row of one plan version: ids, codes, counts and dates only."""
        trigger = VERSION_EVENTS[event["action"]]
        documents: dict[str, Any] = {}
        if event["action"] == "replanned_documents" and event.get("request_id"):
            try:
                documents = json.loads((row.get("flow_ledger") or {}).get(f"docfb:{event['request_id']}") or "{}")
            except ValueError:
                documents = {}
        summary: dict[str, Any] = {
            "plan_version": version, "previous_revision": version - 1 if version > 1 else None,
            "trigger_source": trigger, "action": event["action"],
            "config_version": row.get("config_version") or None,
            "data_origin": row.get("data_origin") or None,
        }
        if event["action"] == "replanned_documents":
            summary.update({"feedback_id": event.get("request_id"),
                            "added_documents": documents.get("added_documents", []),
                            "removed_documents": documents.get("removed_documents", []),
                            "changed_fields": ["documents", "tasks"]})
        elif event["action"] == "replanned":
            summary.update({"request_id": event.get("request_id"),
                            "changed_fields": [d for d in str(event.get("reason") or "").split(",") if d]})
        elif event["action"] == "revised":
            summary.update({"changed_fields": ["plan"]})
        if version == int(row.get("plan_version") or 0):
            summary["task_revision"] = row.get("revision")
        return {
            "case_id": case_id, "revision": version, "client_id": row["client_id"], "drivers": [],
            "added_task_ids": documents.get("added_task_ids", []),
            "removed_task_ids": documents.get("removed_task_ids", []),
            "rescheduled_task_ids": documents.get("rescheduled_task_ids", []),
            "retained_task_ids": [], "applied_by": event.get("by") or "uc02",
            "applied_at": event.get("at"), "approval_request_id": None,
            "reason": (str(event.get("reason") or "") or trigger)[:500],
            "correlation_id": row.get("correlation_id"), "change_summary": summary,
        }

    def _project_revisions(self, payload: dict[str, Any]) -> None:
        """One ``plan_revisions`` row per plan version, from the record's append-only ``plan_events``."""
        case_id, row = payload["case_id"], payload["row"]
        firsts = self._version_events(row)
        known = self._revisions_known.setdefault(case_id, set())
        if not firsts or set(firsts) <= known:
            return
        known |= {int(r["revision"]) for r in self.rest.select(
            "plan_revisions", {"case_id": case_id, "client_id": row["client_id"]}, columns="revision")}
        for version in sorted(set(firsts) - known):
            self._insert_revision(self._projected_row(case_id, row, version, firsts[version]))
            known.add(version)

    def _read_revisions(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        live = self.rest.select("plan_revisions", {"case_id": _uuid(filters["onboarding_id"], "onboarding_id"),
                                                   "client_id": filters["client_id"]}, order_by="revision")
        keep = ("revision", "client_id", "drivers", "added_task_ids", "removed_task_ids",
                "rescheduled_task_ids", "retained_task_ids", "approval_request_id", "applied_by")
        return [{"onboarding_id": str(r["case_id"]), **{k: r.get(k) for k in keep}} for r in live]

    # -- approvals ------------------------------------------------------------------

    def _save_approvals(self, rows: list[dict[str, Any]]) -> int:
        live_rows = []
        for row in rows:
            case_id = _uuid(row["onboarding_id"], "onboarding_id")
            self._require_case_tenant(case_id, row["client_id"])
            live_rows.append({
                "request_id": row["request_id"], "case_id": case_id, "client_id": row["client_id"],
                "gate": row["gate"], "approver_role": row["approver_role"],
                "requested_by": row["requested_by"], "workflow_module": WORKFLOW_MODULE,
                "subject_ref": case_id, "payload": row["payload"],
                "outcome": str(row["outcome"]).upper(), "decided_by": row.get("decided_by"),
                "decided_at": row.get("decided_at"), "decision_notes": row.get("reason") or None,
                "created_at": row["created_at"],
            })
        self.rest.upsert("approval_requests", live_rows, on_conflict="request_id")
        return len(rows)

    def _read_approvals(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        live = self.rest.select("approval_requests", {"client_id": filters["client_id"],
                                                      "workflow_module": WORKFLOW_MODULE})
        return [{
            "request_id": r["request_id"], "onboarding_id": str(r["case_id"]),
            "client_id": r["client_id"], "gate": r["gate"], "requested_by": r["requested_by"],
            "approver_role": r["approver_role"], "payload": r.get("payload") or {},
            "created_at": r["created_at"], "outcome": str(r["outcome"]).lower(),
            "decided_by": r.get("decided_by"), "decided_at": r.get("decided_at"),
            "reason": r.get("decision_notes") or "",
        } for r in live]

    # -- audit (append-only) ---------------------------------------------------------

    def _save_audit(self, rows: list[dict[str, Any]]) -> int:
        live_rows = []
        for row in rows:
            leaked = (FORBIDDEN_DETAIL_KEYS | SHARED_DB_FORBIDDEN_DETAIL_KEYS) & set(row.get("detail") or {})
            if leaked:
                raise PersistenceError("audit detail must not contain personal data keys",
                                       keys=",".join(sorted(leaked)))
            onboarding = row.get("onboarding_id")
            case_id = _uuid(onboarding, "onboarding_id") if onboarding else None
            if case_id is not None:
                self._require_case_tenant(case_id, row["client_id"])
            extras = {k: row[k] for k in ("approver_id", "previous_assignee_id", "current_assignee_id")
                      if row.get(k) is not None}
            live_rows.append({
                "event_id": _event_uuid(row["event_id"]),
                "case_id": case_id,
                "client_id": row["client_id"],
                "actor_type": actor_type_for(row["actor_role"]),
                "actor_id": row["actor_id"], "actor_role": row["actor_role"],
                "action": row["action"], "outcome": row["outcome"], "detail": row["detail"],
                "before_state": {"status": row["before_status"]} if row.get("before_status") is not None else None,
                "after_state": ({"status": row.get("after_status"), **extras}
                                if row.get("after_status") is not None or extras else None),
                "correlation_id": row.get("correlation_id"), "reference": REF_PREFIX + row["event_id"],
                "occurred_at": row["at"], "task_id": row.get("task_id"),
            })
        self.rest.insert("audit_events", live_rows)  # a duplicate key is refused: append-only
        return len(rows)

    def _read_audit(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        live = self.rest.select("audit_events", {"client_id": filters["client_id"]},
                                extra=[("reference", f"like.{REF_PREFIX}*")])
        out = []
        for r in live:
            if r["action"] == STATE_ACTION:
                continue
            after = r.get("after_state") or {}
            out.append({
                "event_id": r["reference"][len(REF_PREFIX):], "client_id": r["client_id"],
                "onboarding_id": str(r["case_id"]) if r.get("case_id") else None,
                "actor_id": r["actor_id"], "actor_role": r["actor_role"], "action": r["action"],
                "outcome": r["outcome"], "detail": r.get("detail") or {}, "at": r["occurred_at"],
                "correlation_id": r.get("correlation_id"), "task_id": r.get("task_id"),
                "before_status": (r.get("before_state") or {}).get("status"),
                "after_status": after.get("status"), "approver_id": after.get("approver_id"),
                "previous_assignee_id": after.get("previous_assignee_id"),
                "current_assignee_id": after.get("current_assignee_id"),
            })
        return out

    # -- agent runs --------------------------------------------------------------------

    def _save_runs(self, rows: list[dict[str, Any]]) -> int:
        live_rows = []
        for row in rows:
            onboarding = row.get("onboarding_id")
            case_id = _uuid(onboarding, "onboarding_id") if onboarding else None
            if case_id is not None:
                self._require_case_tenant(case_id, row["client_id"])
            live = {
                "run_id": row["run_id"], "case_id": case_id,
                "client_id": row["client_id"], "agent_id": row["agent_id"],
                "agent_version": row["agent_version"], "actor_id": row["actor_id"],
                "status": row["status"], "advisor_mode": row["advisor"], "model_name": row.get("model"),
                "proposal_count": row["proposal_count"], "action_count": row["action_count"],
                "exception_count": row["exception_count"], "tool_calls": row["tool_calls"],
                "finished_at": row.get("finished_at"),
            }
            for optional in ("started_at", "error_summary"):  # orchestrated stage runs carry these
                if row.get(optional) is not None:
                    live[optional] = row[optional]
            live_rows.append(live)
        self.rest.upsert("agent_runs", live_rows, on_conflict="run_id")
        return len(rows)

    def _read_runs(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        live = self.rest.select("agent_runs", {"client_id": filters["client_id"]})
        return [{
            "run_id": r["run_id"], "agent_id": r["agent_id"], "agent_version": r["agent_version"],
            "client_id": r["client_id"], "onboarding_id": str(r["case_id"]) if r.get("case_id") else None,
            "actor_id": r["actor_id"], "status": r["status"], "advisor": r["advisor_mode"],
            "model": r.get("model_name"), "proposal_count": r["proposal_count"],
            "action_count": r["action_count"], "exception_count": r["exception_count"],
            "tool_calls": r["tool_calls"], "finished_at": r.get("finished_at"),
        } for r in live]


__all__ = ["ACTOR_TYPES", "STATE_ACTION", "VERSION_EVENTS", "WORKFLOW_MODULE", "CaseNotFoundError",
           "OwnershipConflictError", "SupabaseTableClient", "actor_type_for"]
