"""A `TableClient` for UC-04 over the Supabase PostgREST API. Server-side only; the service-role key never leaves this process and is never
printed or put in an error. Every read, update and delete-free write is filtered or keyed; there is no unfiltered access and no delete.

This file is the "approved external layer" the UC-04 core refuses to contain: UC-04's packaging tests forbid HTTP libraries inside its own
folder, so the connection lives here, next to it, and is injected into UC-04's repositories."""

import json
import urllib.error
import urllib.parse
import urllib.request


class DatabaseError(RuntimeError):
    """A failed database call. Carries the HTTP status and PostgREST's short error code only; never the URL, key or row content."""

    def __init__(self, status, code=None):
        super().__init__(f"database request failed (status {status}{', code ' + str(code) if code else ''})")
        self.status, self.code = status, code


class PostgrestTableClient:
    def __init__(self, base_url: str, service_key: str, schema: str = "public", timeout: float = 20.0):
        if not base_url or not service_key:
            raise ValueError("database URL and service key are required")
        self._base, self._key, self._schema, self._timeout = base_url.rstrip("/"), service_key, schema, timeout

    def _call(self, method: str, path: str, body=None, prefer: str | None = None):
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}", "Accept": "application/json", "Content-Type": "application/json",
                   "Accept-Profile": self._schema, "Content-Profile": self._schema}
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body, default=str).encode() if body is not None else None
        request = urllib.request.Request(self._base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            code = None
            try:
                code = json.loads(exc.read()).get("code")
            except Exception:  # noqa: BLE001 - the body is only mined for its short code
                pass
            raise DatabaseError(exc.code, code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DatabaseError(type(exc).__name__) from None

    @staticmethod
    def _eq(filters) -> str:
        return "".join(f"&{column}=eq.{urllib.parse.quote(str(value), safe='')}" for column, value in filters.items())

    def select(self, table, *, filters, columns=None, limit=None):
        if not filters:
            raise ValueError("an unfiltered read is not allowed")
        cols = ",".join(columns) if columns else "*"
        return self._call("GET", f"/rest/v1/{table}?select={cols}{self._eq(filters)}" + (f"&limit={limit}" if limit else "")) or []

    def insert(self, table, row):
        self._call("POST", f"/rest/v1/{table}", dict(row), "return=minimal")  # a duplicate primary key raises DatabaseError(409)

    def upsert(self, table, row, *, on_conflict):
        self._call("POST", f"/rest/v1/{table}?on_conflict={','.join(on_conflict)}", dict(row), "resolution=merge-duplicates,return=minimal")

    def update(self, table, *, filters, values):
        """Rows changed. The caller uses it as a compare-and-swap: filter on the version read; zero means someone else wrote first."""
        if not filters:
            raise ValueError("an unfiltered update is not allowed")
        changed = self._call("PATCH", f"/rest/v1/{table}?select=*{self._eq(filters)}", dict(values), "return=representation")
        return len(changed or [])

    def table_exists(self, table: str) -> bool:
        """Read-only: does the API expose this table? (a zero-row read; no data is returned)"""
        try:
            self._call("GET", f"/rest/v1/{table}?select=*&limit=0")
            return True
        except DatabaseError as exc:
            if exc.status == 404:
                return False
            raise
