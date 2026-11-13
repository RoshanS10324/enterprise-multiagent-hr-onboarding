"""Thin PostgREST client for the Supabase project.

WHY PostgREST and not psycopg
-----------------------------
docs/schema/uc01-schema.md section 9 prescribes `psycopg` directly
against the Postgres connection string, because operations #1-#12 each
need one multi-statement transaction. That remains the right target.

It is not reachable today: this deployment has no usable Postgres
connection string (see docs/schema/live-schema-comparison.md). The only
credentials that authenticate against the confirmed project are the
PostgREST URL + service-role key. PostgREST runs one statement per call
and cannot group statements into a transaction.

The consequence is NOT hidden: every adapter built on this client is
single-statement. Multi-row operations are therefore not atomic, and
CaseRepository/AuditLog - the two that most need atomicity - are not
implemented against it at all (see unsupported.py). Nothing here claims
a transactional guarantee it cannot keep.

Secrets: the service-role key is held in a SecretStr and attached to
request headers only. It is never logged, never placed in a URL, and
never included in an exception message or repr.
"""

from __future__ import annotations

import binascii
from typing import Any, Iterable, Mapping, Optional

from urllib.parse import quote

import httpx
from pydantic import SecretStr

# Postgres SQLSTATEs worth distinguishing from a generic failure.
_UNIQUE_VIOLATION = "23505"
_FK_VIOLATION = "23503"
_NOT_NULL_VIOLATION = "23502"
_CHECK_VIOLATION = "23514"


class SupabaseError(RuntimeError):
    """A PostgREST call failed. Carries no credentials."""


class UniqueViolation(SupabaseError):
    """A unique index rejected the write (SQLSTATE 23505)."""


class NotNullViolation(SupabaseError):
    """A NOT NULL column was not supplied (SQLSTATE 23502)."""


class ForeignKeyViolation(SupabaseError):
    """A referenced row does not exist (SQLSTATE 23503)."""


class CheckViolation(SupabaseError):
    """A CHECK constraint rejected the value (SQLSTATE 23514)."""


_SQLSTATE_ERRORS = {
    _UNIQUE_VIOLATION: UniqueViolation,
    _NOT_NULL_VIOLATION: NotNullViolation,
    _FK_VIOLATION: ForeignKeyViolation,
    _CHECK_VIOLATION: CheckViolation,
}


def encode_bytea(raw: bytes) -> str:
    r"""Postgres hex-format bytea literal, e.g. b"\x01" -> "\\x01"."""
    return "\\x" + binascii.hexlify(raw).decode("ascii")


def decode_bytea(value: str | bytes | None) -> Optional[bytes]:
    r"""Parse what PostgREST returns for a bytea column ("\\xdeadbeef")."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    text = value[2:] if value.startswith("\\x") else value
    try:
        return binascii.unhexlify(text)
    except binascii.Error as exc:
        raise SupabaseError("bytea column was not valid hex") from exc


class PostgrestClient:
    """Minimal synchronous PostgREST wrapper.

    Only the verbs the adapters actually need. Deliberately not a general
    query builder: a narrow surface is easier to keep safe.
    """

    def __init__(
        self,
        base_url: str,
        service_key: SecretStr,
        *,
        schema: str = "public",
        timeout: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._schema = schema
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/rest/v1",
            timeout=timeout,
            transport=transport,
            headers={
                "apikey": service_key.get_secret_value(),
                "Authorization": f"Bearer {service_key.get_secret_value()}",
                "Accept-Profile": schema,
                "Content-Profile": schema,
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PostgrestClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        code, message = "", ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                code = str(body.get("code") or "")
                # PostgREST puts the Postgres message in "message"; it can
                # echo the offending VALUE, so it is not propagated verbatim.
                message = str(body.get("details") or body.get("hint") or "")
        except Exception:  # noqa: BLE001 - non-JSON error body
            pass
        exc_type = _SQLSTATE_ERRORS.get(code, SupabaseError)
        raise exc_type(
            f"PostgREST {resp.request.method} {resp.request.url.path} "
            f"failed with HTTP {resp.status_code}"
            + (f" (SQLSTATE {code})" if code else "")
            + (f": {message}" if message and code in _SQLSTATE_ERRORS else "")
        )

    # -- verbs -------------------------------------------------------

    def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Optional[Mapping[str, str]] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"select": columns}
        params.update(filters or {})
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        resp = self._client.get(f"/{table}", params=params)
        self._raise_for_status(resp)
        return list(resp.json())

    def insert(
        self,
        table: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        returning: bool = True,
        on_conflict_ignore: bool = False,
    ) -> list[dict[str, Any]]:
        prefer = ["return=representation" if returning else "return=minimal"]
        if on_conflict_ignore:
            prefer.append("resolution=ignore-duplicates")
        resp = self._client.post(
            f"/{table}",
            json=list(rows),
            headers={"Prefer": ",".join(prefer)},
        )
        self._raise_for_status(resp)
        return list(resp.json()) if returning and resp.content else []

    def upsert(
        self, table: str, rows: Iterable[Mapping[str, Any]], *, on_conflict: str
    ) -> list[dict[str, Any]]:
        resp = self._client.post(
            f"/{table}",
            params={"on_conflict": on_conflict},
            json=list(rows),
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        self._raise_for_status(resp)
        return list(resp.json()) if resp.content else []

    def update(
        self, table: str, values: Mapping[str, Any], *, filters: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        resp = self._client.patch(
            f"/{table}",
            params=dict(filters),
            json=dict(values),
            headers={"Prefer": "return=representation"},
        )
        self._raise_for_status(resp)
        return list(resp.json()) if resp.content else []

    def rpc(self, function: str, args: Mapping[str, Any]) -> Any:
        """Call a Postgres function exposed by PostgREST (POST /rpc/<name>).

        One HTTP call is one transaction, so a function is the only way to
        make a multi-statement operation atomic over PostgREST.
        """
        resp = self._client.post(f"/rpc/{function}", json=dict(args))
        self._raise_for_status(resp)
        return resp.json() if resp.content else None

    def ping(self) -> None:
        """Real round-trip against the project. Raises on any failure.

        Used by the container at startup so a misconfigured deployment
        fails immediately rather than on the first business request.
        """
        resp = self._client.get("/")
        self._raise_for_status(resp)


# ---------------------------------------------------------------- storage


class StorageError(SupabaseError):
    pass


class SupabaseStorage:
    """Minimal Supabase Storage client: object exists / upload / download.

    Never creates or deletes a bucket, never overwrites an object
    (x-upsert: false), never makes anything public.
    """

    def __init__(self, base_url: str, service_key: SecretStr, *, timeout: float = 30.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        key = service_key.get_secret_value()
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/storage/v1", timeout=timeout,
                                  transport=transport, headers={"apikey": key, "Authorization": f"Bearer {key}"})

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _p(path: str) -> str:
        return quote(path, safe="/")

    def exists(self, bucket: str, path: str) -> bool:
        folder, _, name = path.rpartition("/")
        resp = self._http.post(f"/object/list/{quote(bucket)}", json={"prefix": folder, "search": name, "limit": 100})
        if resp.status_code >= 400:
            raise StorageError(f"storage list failed with HTTP {resp.status_code}")
        return any(o.get("name") == name and o.get("id") for o in resp.json())

    def upload(self, bucket: str, path: str, data: bytes, mime_type: str) -> None:
        resp = self._http.post(f"/object/{quote(bucket)}/{self._p(path)}", content=data,
                               headers={"Content-Type": mime_type, "x-upsert": "false"})
        if resp.status_code >= 400:
            raise StorageError(f"storage upload failed with HTTP {resp.status_code}")

    def download(self, bucket: str, path: str) -> bytes:
        resp = self._http.get(f"/object/authenticated/{quote(bucket)}/{self._p(path)}")
        if resp.status_code >= 400:
            raise StorageError(f"storage download failed with HTTP {resp.status_code}")
        return resp.content
