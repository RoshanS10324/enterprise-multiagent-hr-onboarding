"""Supabase (PostgREST) access for UC-02 over HTTPS, using only the standard library.

What this is
------------
* :class:`SupabaseRestClient` - ``select`` / ``upsert`` / ``delete`` on the shared dev
  database. It implements the same three-method shape as ``persistence.TableClient``,
  but against the *live* table and column names, which differ from UC-02's local
  tables (``case_id`` uuid, ``owner_role``, ``assigned_actor_id`` ...).
* :class:`SupabaseGateway` - the reads UC-02 legitimately needs: the onboarding case
  that UC-01 / the orchestrator created, and a client's configuration metadata.

What this is not
----------------
* Not a full runtime store. UC-02 keeps plan/final-review state that has no column in
  the live ``onboarding_cases`` table, so ``UC02_STORE_MODE`` still accepts only
  ``memory`` and ``sqlite``; nothing here is reachable from ``build_service``.
* Not a case creator. ``onboarding_cases`` requires candidate name/email and other
  UC-01 data that UC-02 does not own and must not invent, so the gateway only reads it,
  and only the non-personal columns.

Credentials come from the environment (``NEXT_PUBLIC_SUPABASE_URL`` and
``SUPABASE_SERVICE_ROLE_KEY``) and are never logged, put in an exception message or
returned. The service-role key bypasses row level security, so every call here carries
an explicit ``client_id`` filter.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from onboarding_task_management.persistence import PersistenceError

ENV_URL = "SUPABASE_URL"
ENV_URL_FALLBACK = "NEXT_PUBLIC_SUPABASE_URL"  # accepted when SUPABASE_URL is unset
ENV_KEY = "SUPABASE_SERVICE_ROLE_KEY"

#: Non-personal columns of ``onboarding_cases``. ``candidate_name`` and
#: ``candidate_email`` are deliberately absent: UC-02 never needs them.
CASE_COLUMNS = (
    "case_id,client_id,employee_no,hr_officer_id,joining_date,work_location_type,"
    "location_name,project_name,role_id,grade,department,manager_id,nationality_group,"
    "status,current_stage,onboarding_attributes,is_deleted"
)

#: ``(method, url, headers, body)`` -> ``(status, parsed json or None)``. Injectable so
#: tests never touch the network.
Transport = Callable[[str, str, Mapping[str, str], bytes | None], "tuple[int, Any]"]


class SupabaseUnavailableError(PersistenceError):
    """The database could not be reached, or answered with a transient server error (HTTP 429 / 5xx).

    A technical failure a retry can fix -- never a business refusal and never "no data". A subclass of
    ``PersistenceError``, so every existing ``except PersistenceError`` still catches it; callers that must tell
    a retryable outage from a refusal (the orchestrator's UC-02 adapter) catch this class first.
    """


def _urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https URL from env
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SupabaseUnavailableError(
            "supabase is unreachable", reason=type(exc).__name__
        ) from None


class SupabaseRestClient:
    """Equality-filter row access to PostgREST. Fails closed; never swallows an error."""

    def __init__(
        self,
        url: str,
        service_role_key: str,
        *,
        transport: Transport | None = None,
    ) -> None:
        if not url.startswith("https://"):
            raise PersistenceError("the supabase url must be https")
        if not service_role_key:
            raise PersistenceError("no supabase service-role key configured")
        self._base = url.rstrip("/") + "/rest/v1/"
        self._key = service_role_key  # kept private; never echoed
        self._transport = transport or _urllib_transport

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str], *, transport: Transport | None = None
    ) -> SupabaseRestClient:
        url = environ.get(ENV_URL, "") or environ.get(ENV_URL_FALLBACK, "")
        key = environ.get(ENV_KEY, "")
        if not url or not key:
            raise PersistenceError(
                "supabase is not configured", missing=",".join(
                    n for n, v in ((f"{ENV_URL} (or {ENV_URL_FALLBACK})", url), (ENV_KEY, key)) if not v
                )
            )
        return cls(url, key, transport=transport)

    # -- plumbing ------------------------------------------------------------

    def _call(
        self, method: str, path: str, *, query: Sequence[tuple[str, str]] = (),
        body: Any = None, prefer: str | None = None,
    ) -> Any:
        url = self._base + path + ("?" + urllib.parse.urlencode(list(query)) if query else "")
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if prefer:
            headers["Prefer"] = prefer
        status, payload = self._transport(method, url, headers, data)
        if status >= 400:
            message = payload.get("message") if isinstance(payload, dict) else None
            error = SupabaseUnavailableError if status == 429 or status >= 500 else PersistenceError
            raise error(
                "supabase is unavailable" if error is SupabaseUnavailableError else "supabase rejected the request",
                table=path, status=status, detail=str(message or "")[:200],
            )
        return payload

    @staticmethod
    def _eq(filters: Mapping[str, Any]) -> list[tuple[str, str]]:
        out = []
        for column, value in filters.items():
            if value is None:
                raise PersistenceError("a filter value must not be None", column=column)
            out.append((column, f"eq.{value}"))
        return out

    def schema(self) -> dict[str, set[str]]:
        """``{table: {column, ...}}`` as PostgREST currently exposes it."""
        spec = self._call("GET", "")
        definitions = spec.get("definitions") if isinstance(spec, dict) else None
        if not isinstance(definitions, dict):
            raise PersistenceError("supabase did not return its schema")
        return {t: set(d.get("properties", {})) for t, d in definitions.items()}

    # -- the TableClient-shaped surface -------------------------------------

    def select(
        self, table: str, filters: Mapping[str, Any], order_by: str | None = None,
        *, columns: str = "*", limit: int | None = None,
        extra: Sequence[tuple[str, str]] = (),
    ) -> list[dict[str, Any]]:
        query = [("select", columns), *self._eq(filters), *extra]
        if order_by:
            query.append(("order", order_by))
        if limit is not None:
            query.append(("limit", str(limit)))
        rows = self._call("GET", table, query=query)
        if not isinstance(rows, list):
            raise PersistenceError("unexpected supabase response", table=table)
        return rows

    def upsert(
        self, table: str, rows: Sequence[dict[str, Any]], *, on_conflict: str | None = None
    ) -> int:
        if not rows:
            return 0
        query = [("on_conflict", on_conflict)] if on_conflict else []
        written = self._call(
            "POST", table, query=query, body=list(rows),
            prefer="resolution=merge-duplicates,return=representation",
        )
        if not isinstance(written, list) or len(written) != len(rows):
            raise PersistenceError(
                "supabase did not confirm every row", table=table,
                sent=len(rows), confirmed=len(written) if isinstance(written, list) else 0,
            )
        return len(written)

    def insert(self, table: str, rows: Sequence[dict[str, Any]]) -> int:
        """Plain insert: a duplicate key is an error, never an overwrite."""
        if not rows:
            return 0
        written = self._call("POST", table, body=list(rows), prefer="return=representation")
        if not isinstance(written, list) or len(written) != len(rows):
            raise PersistenceError("supabase did not confirm every row", table=table)
        return len(written)

    def update(self, table: str, filters: Mapping[str, Any], values: Mapping[str, Any]) -> int:
        if not filters:
            raise PersistenceError("refusing an unfiltered update", table=table)
        changed = self._call(
            "PATCH", table, query=self._eq(filters), body=dict(values),
            prefer="return=representation",
        )
        return len(changed) if isinstance(changed, list) else 0

    def delete(self, table: str, filters: Mapping[str, Any]) -> int:
        if not filters:
            raise PersistenceError("refusing an unfiltered delete", table=table)
        removed = self._call(
            "DELETE", table, query=self._eq(filters), prefer="return=representation"
        )
        return len(removed) if isinstance(removed, list) else 0


class SupabaseGateway:
    """The reads UC-02 needs from the shared tables, scoped to one client."""

    def __init__(self, client: SupabaseRestClient) -> None:
        self.client = client

    def get_case(self, client_id: str, case_id: str) -> dict[str, Any] | None:
        """The UC-01 case, non-personal columns only; ``None`` if absent for this client."""
        rows = self.client.select(
            "onboarding_cases", {"client_id": client_id, "case_id": case_id},
            columns=CASE_COLUMNS,
        )
        return rows[0] if rows and not rows[0].get("is_deleted") else None  # a soft-deleted case does not exist

    def list_cases(self, client_id: str) -> list[dict[str, Any]]:
        rows = self.client.select(
            "onboarding_cases", {"client_id": client_id}, order_by="opened_at",
            columns=CASE_COLUMNS,
        )
        return [r for r in rows if not r.get("is_deleted")]

    def client_config_meta(self, client_id: str) -> dict[str, Any] | None:
        """Calendar and version facts the database holds for a client.

        The task and milestone *definitions* are read from the ``client_*`` tables, and
        are empty today, so this never fabricates a checklist: an absent or non-approved
        version is reported as such and the caller must refuse to plan from it.
        """
        rows = self.client.select("client_configs", {"client_id": client_id})
        if not rows:
            return None
        head = rows[0]
        versions = self.client.select(
            "client_config_versions", {"client_id": client_id}, order_by="version.desc"
        )
        latest = versions[0] if versions else None
        return {
            "client_id": client_id,
            "working_days": head["working_days"],
            "timezone": head["timezone"],
            "is_active": head["is_active"],
            "template_count": head["template_count"],
            "milestone_count": head["milestone_count"],
            "version": latest["version"] if latest else None,
            "version_status": latest["status"] if latest else "unspecified",
            "approved": bool(latest and latest["status"] == "approved" and latest["approved_by"]),
        }

    def task_rows(self, client_id: str, case_id: str) -> list[dict[str, Any]]:
        return self.client.select(
            "onboarding_tasks", {"client_id": client_id, "case_id": case_id},
            order_by="plan_order",
        )


__all__ = ["CASE_COLUMNS", "SupabaseGateway", "SupabaseRestClient", "SupabaseUnavailableError"]
