"""Where the orchestrator keeps each case's last-known `OnboardingCaseState` between calls.

Resume (`advance_case(None, case_id=...)`) starts from this store, not from LangGraph's
`get_state` (see docs/ONBOARDING_COMMON_ARCHITECTURE.md, Durability). The orchestrator writes the
state through after every stage node, so an orchestrator crash mid-run loses at most the stage that
was running -- whose UC side effect, if any, the next run re-reads from that UC before acting.

  InMemoryCaseStateStore  the default (development, tests): a dict; lost when the process exits.
  SqliteCaseStateStore    optional (`build_deps(persist_dir=...)`): one SQLite file, single host.
  SupabaseCaseStateStore  the shared `case_orchestration_state` table (supabase_state_store.py;
                          `build_deps(state_store="supabase")` or ORCHESTRATOR_STATE_STORE=supabase):
                          authoritative and shared by every orchestrator process, with optimistic
                          concurrency enforced by the database.
                          Same limits as every other SQLite store in this project -- not for several
                          hosts, replicas or network file systems; a deployment with more than one
                          orchestrator process needs a server database behind this same interface.

What this store is not: it holds the orchestrator's projection only. It does not persist any UC's
business data (each UC owns that) and it does not make any external side effect exactly-once.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.orchestration.state import OnboardingCaseState


class StatePersistenceError(RuntimeError):
    """The case state could not be read or written. Carries no credential and no row values."""

    code = "STATE_PERSISTENCE_ERROR"
    retryable = False


class StatePersistenceUnavailable(StatePersistenceError):
    """The state database could not be reached (network, timeout, 5xx). Nothing was written elsewhere
    instead; the caller may retry, and a resume starts from the last committed state."""

    code = "STATE_PERSISTENCE_UNAVAILABLE"
    retryable = True


class StateVersionConflict(StatePersistenceError):
    """Optimistic concurrency: the stored state changed since this process read it (another process
    wrote the case). The write did not happen; reload and re-apply."""

    code = "STATE_VERSION_CONFLICT"
    retryable = True


class CaseOwnershipError(StatePersistenceError):
    """The case does not exist in the state's tenant (unknown case, or another tenant's case)."""

    code = "CASE_OWNERSHIP_ERROR"


class InMemoryCaseStateStore(dict):
    """A plain dict keyed by case_id (kept a dict so existing callers -- and tests that inspect
    `deps.case_states[case_id]` -- keep working unchanged)."""

    def save(self, state: OnboardingCaseState) -> None:
        self[state["case_id"]] = copy.deepcopy(state)


class SqliteCaseStateStore(MutableMapping):
    """Case states as JSON in SQLite, with the tenant as its own indexed column. Reads return a
    copy; changing one does not change the store until it is saved."""

    def __init__(self, path: Path | str):
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS case_states (case_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, "
                           "updated_at TEXT NOT NULL, body TEXT NOT NULL)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_case_states_client ON case_states(client_id)")
        self._conn.commit()

    def save(self, state: OnboardingCaseState) -> None:
        self[state["case_id"]] = state

    def __setitem__(self, case_id: str, state: OnboardingCaseState) -> None:
        body = json.dumps(state, default=str)  # dates are already ISO strings; enums are str
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO case_states VALUES (?,?,?,?)",
                               (case_id, state.get("client_id", ""), now_utc().isoformat(), body))
            self._conn.commit()

    def __getitem__(self, case_id: str) -> OnboardingCaseState:
        with self._lock:
            row = self._conn.execute("SELECT body FROM case_states WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise KeyError(case_id)
        return json.loads(row[0])

    def __delitem__(self, case_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM case_states WHERE case_id=?", (case_id,))
            self._conn.commit()

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            rows = self._conn.execute("SELECT case_id FROM case_states ORDER BY case_id").fetchall()
        return iter([r[0] for r in rows])

    def __len__(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM case_states").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
