"""Document upload for UC03: the file goes to Supabase Storage, its metadata to `candidate_documents`,
and UC03 validates what it reads back from Storage.

    upload (bytes)  ->  checks (role, tenant, plan requirement, type, signature, size)
                    ->  Supabase Storage  <bucket>/<client_id>/<case_id>/<document_id>.<ext>
                    ->  candidate_documents row (only when the case has an onboarding_cases row)
                    ->  download the stored object again, verify its checksum
                    ->  UC03's own extractor (src/sub_agent/extraction.py) -> DocumentMetadata
                    ->  OnboardingOrchestratorAgent.submit_documents  (UC03 stores + validates; the
                        orchestrator routes: correction, HR review, UC02 replan, or the UC04 || UC05 gate)

What this module does NOT do
----------------------------
- Decide anything about a document. UC03's validation rules and an authorized HR reviewer decide;
  extraction only reports what it read. Nothing extracted can approve a document.
- Store document bytes in PostgreSQL. The bytes live in Storage; the table holds the reference.
- Fall back silently. Without a configured bucket the upload route answers 503.
- Invent `onboarding_cases` rows. `candidate_documents.case_id` is a foreign key to it; while UC01's
  own Supabase writes are paused (UC01 docs/supabase-integration-status.md, decisions B1-B9) a case
  opened by UC01 has no such row, and the metadata row is skipped with an explicit reason. The file
  is still stored and validated.

Object paths follow UC01's design (UC01 adapters/supabase/storage_paths.py): built only from
server-controlled ids, never from the uploaded file name. The document id is derived from the tenant,
case, document type and content hash, so uploading the same file twice is idempotent end to end
(same object, same document_ref, and UC03's collection recognises the identical submission).

Configuration: DOCUMENT_STORAGE_BUCKET, and DOCUMENT_STORAGE_SUPABASE_URL / DOCUMENT_STORAGE_SERVICE_ROLE_KEY
(falling back to SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY). The dedicated names exist because the UC worker
processes inherit the orchestrator's environment: a bare SUPABASE_URL switches UC01 into its Supabase mode,
which is blocked on UC01's open decisions (UC01_FIELD_ENCRYPTION_KEY, B1). Workers never receive
DOCUMENT_STORAGE_SERVICE_ROLE_KEY (workers/config.py).

Secrets: the service-role key only ever goes into request headers of this backend process. Storage
error bodies are not echoed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import re
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.observability.logging import get_logger

log = get_logger("orchestration.documents")

# The file types UC03's own upload endpoint accepts (UC03 src/api/uc03.py), with their file signatures:
# a declared type is only trusted when the bytes start with that type's signature.
ALLOWED_TYPES: dict[str, tuple[str, tuple[bytes, ...]]] = {
    "application/pdf": ("pdf", (b"%PDF-",)),
    "image/jpeg": ("jpg", (b"\xff\xd8\xff",)),
    "image/png": ("png", (b"\x89PNG\r\n\x1a\n",)),
    "image/tiff": ("tif", (b"II*\x00", b"MM\x00*")),
}
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # no limit exists in UC03 or on the bucket: configurable (DOCUMENT_MAX_BYTES)
SIGNED_URL_TTL_S = 60
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
REF_PREFIX = "storage:"


class DocumentUploadError(Exception):
    """A refused upload. `status` is the HTTP status the API answers with; the message carries ids
    and reasons only, never file contents or extracted values."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


class UnsafeObjectPath(ValueError):
    pass


def object_path(client_id: str, case_id: str, document_id: str, extension: str) -> str:
    """`<client_id>/<case_id>/<document_id>.<ext>`: server-controlled segments only."""
    for value, what in ((client_id, "client_id"), (case_id, "case_id")):
        if not isinstance(value, str) or not _SAFE_SEGMENT.match(value):
            raise UnsafeObjectPath(f"{what} contains characters not allowed in an object path")
    try:
        document_id = str(uuid.UUID(document_id))
    except (ValueError, AttributeError, TypeError):
        raise UnsafeObjectPath("document_id must be a UUID") from None
    if extension not in {ext for ext, _ in ALLOWED_TYPES.values()}:
        raise UnsafeObjectPath("extension not allowed")
    return f"{client_id}/{case_id}/{document_id}.{extension}"


def document_ref(bucket: str, path: str) -> str:
    return f"{REF_PREFIX}{bucket}/{path}"


def parse_document_ref(ref: str) -> tuple[str, str] | None:
    """(bucket, path) of a reference this module created, else None (e.g. an `on_file` reference)."""
    if not isinstance(ref, str) or not ref.startswith(REF_PREFIX):
        return None
    bucket, _, path = ref[len(REF_PREFIX):].partition("/")
    if not _BUCKET.match(bucket) or not path or ".." in path or path.startswith("/"):
        return None
    return bucket, path


STORAGE_URL_VARS = ("DOCUMENT_STORAGE_SUPABASE_URL", "SUPABASE_URL")
STORAGE_KEY_VARS = ("DOCUMENT_STORAGE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_ROLE_KEY")


def _first_env(environ: Mapping[str, str], names: tuple[str, ...]) -> str:
    return next((environ[n].strip() for n in names if (environ.get(n) or "").strip()), "")


def safe_file_name(name: str | None) -> str:
    """The client's file name for the metadata row only (never a path): base name, printable, short."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return (base or "document")[:200]


# ---------------------------------------------------------------------------------------- storage


class ObjectStorage(Protocol):
    bucket: str

    def upload(self, path: str, data: bytes, content_type: str) -> str: ...  # "created" | "exists"
    def download(self, path: str) -> bytes: ...
    def signed_url(self, path: str, expires_in: int) -> str: ...


class SupabaseObjectStorage:
    """Supabase Storage over its REST API, with the backend's service-role key. Private bucket only:
    objects are read back by this backend, or through a short-lived signed URL it issues."""

    def __init__(self, *, url: str, service_role_key: SecretStr | str, bucket: str, timeout_s: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        if not url.startswith("https://") and transport is None:
            raise ValueError("SUPABASE_URL must be an https:// URL")
        if not _BUCKET.match(bucket or ""):
            raise ValueError("DOCUMENT_STORAGE_BUCKET is not a valid bucket name")
        key = service_role_key if isinstance(service_role_key, SecretStr) else SecretStr(service_role_key)
        if not key.get_secret_value().strip():
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY is not set")
        self.bucket = bucket
        self._base = url.rstrip("/")
        self._client = httpx.Client(base_url=self._base + "/storage/v1", timeout=timeout_s, transport=transport,
                                    headers={"apikey": key.get_secret_value(),
                                             "Authorization": f"Bearer {key.get_secret_value()}"})

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "SupabaseObjectStorage | None":
        bucket = (environ.get("DOCUMENT_STORAGE_BUCKET") or "").strip()
        if not bucket:
            return None
        return cls(url=_first_env(environ, STORAGE_URL_VARS), service_role_key=_first_env(environ, STORAGE_KEY_VARS),
                   bucket=bucket)

    def _unavailable(self, what: str, response: httpx.Response | None = None) -> DocumentUploadError:
        status = f"HTTP {response.status_code}" if response is not None else "unreachable"
        return DocumentUploadError(503, "DOCUMENT_STORAGE_UNAVAILABLE", f"document storage {what} failed ({status})")

    def upload(self, path: str, data: bytes, content_type: str) -> str:
        try:
            response = self._client.post(f"/object/{self.bucket}/{path}", content=data,
                                         headers={"Content-Type": content_type, "x-upsert": "false"})
        except httpx.HTTPError:
            raise self._unavailable("upload") from None
        if response.status_code in (200, 201):
            return "created"
        if response.status_code in (400, 409):
            # Supabase answers an existing object with 409 (or 400 carrying statusCode 409/"Duplicate").
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code == 409 or str(body.get("statusCode")) == "409" or body.get("error") == "Duplicate":
                return "exists"
        if response.status_code in (401, 403):
            raise DocumentUploadError(503, "DOCUMENT_STORAGE_REFUSED", "document storage refused the backend's key")
        raise self._unavailable("upload", response)

    def download(self, path: str) -> bytes:
        try:
            response = self._client.get(f"/object/authenticated/{self.bucket}/{path}")
        except httpx.HTTPError:
            raise self._unavailable("download") from None
        if response.status_code == 200:
            return response.content
        if response.status_code in (400, 404):
            raise DocumentUploadError(404, "DOCUMENT_NOT_FOUND", "the stored document could not be found")
        raise self._unavailable("download", response)

    def signed_url(self, path: str, expires_in: int) -> str:
        try:
            response = self._client.post(f"/object/sign/{self.bucket}/{path}", json={"expiresIn": int(expires_in)})
        except httpx.HTTPError:
            raise self._unavailable("signing") from None
        if response.status_code != 200:
            if response.status_code in (400, 404):
                raise DocumentUploadError(404, "DOCUMENT_NOT_FOUND", "the stored document could not be found")
            raise self._unavailable("signing", response)
        signed = response.json().get("signedURL") or response.json().get("signedUrl")
        if not signed:
            raise self._unavailable("signing", response)
        return f"{self._base}/storage/v1{signed}" if signed.startswith("/") else signed


class InMemoryObjectStorage:
    """Tests only: the same contract as SupabaseObjectStorage, in a dict."""

    def __init__(self, bucket: str = "test-bucket"):
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.uploads = 0

    def upload(self, path: str, data: bytes, content_type: str) -> str:
        self.uploads += 1
        if path in self.objects:
            return "exists"
        self.objects[path] = (bytes(data), content_type)
        return "created"

    def download(self, path: str) -> bytes:
        if path not in self.objects:
            raise DocumentUploadError(404, "DOCUMENT_NOT_FOUND", "the stored document could not be found")
        return self.objects[path][0]

    def signed_url(self, path: str, expires_in: int) -> str:
        if path not in self.objects:
            raise DocumentUploadError(404, "DOCUMENT_NOT_FOUND", "the stored document could not be found")
        return f"memory://{self.bucket}/{path}?expires_in={expires_in}"


class LocalDiskObjectStorage:
    """Development only, selected explicitly with DOCUMENT_STORAGE_BACKEND=local: the same contract as
    SupabaseObjectStorage, on a local folder (DOCUMENT_STORAGE_LOCAL_DIR). Nothing leaves this machine. Viewing works
    like Supabase's signed URLs: `signed_url` returns a short-lived link to this backend (DOCUMENT_STORAGE_PUBLIC_BASE_URL,
    default http://127.0.0.1:8000), signed with a per-process secret, which `open_signed` verifies before serving."""

    bucket = "local-dev"
    ROUTE = "/v1/orchestration/local-documents/"
    MEDIA = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".tif": "image/tiff", ".tiff": "image/tiff"}

    def __init__(self, root: Path, *, base_url: str = "http://127.0.0.1:8000"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self._secret = os.urandom(32)  # links die with the process

    def _file(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if self.root not in target.parents:
            raise UnsafeObjectPath(f"refusing a path outside the storage folder: {path!r}")
        return target

    def upload(self, path: str, data: bytes, content_type: str) -> str:
        target = self._file(path)
        if target.exists():
            return "exists"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(bytes(data))
        tmp.replace(target)
        return "created"

    def download(self, path: str) -> bytes:
        target = self._file(path)
        if not target.is_file():
            raise DocumentUploadError(404, "DOCUMENT_NOT_FOUND", "the stored document could not be found")
        return target.read_bytes()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def signed_url(self, path: str, expires_in: int) -> str:
        if not self._file(path).is_file():
            raise DocumentUploadError(404, "DOCUMENT_NOT_FOUND", "the stored document could not be found")
        payload = f"{int(time.time()) + int(expires_in)}|{path}"
        token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{self.base_url}{self.ROUTE}{token}.{self._sign(payload)}"

    def open_signed(self, token: str) -> tuple[bytes, str]:
        """The file a signed link names, with its media type. Raises DocumentUploadError(403/404) otherwise."""
        try:
            encoded, signature = token.rsplit(".", 1)
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
            expiry, path = payload.split("|", 1)
        except (ValueError, UnicodeDecodeError):
            raise DocumentUploadError(403, "DOCUMENT_LINK_INVALID", "the document link is not valid") from None
        if not hmac.compare_digest(signature, self._sign(payload)):
            raise DocumentUploadError(403, "DOCUMENT_LINK_INVALID", "the document link is not valid")
        if int(expiry) < time.time():
            raise DocumentUploadError(403, "DOCUMENT_LINK_EXPIRED", "the document link has expired; click View again")
        target = self._file(path)
        return self.download(path), self.MEDIA.get(target.suffix.lower(), "application/octet-stream")


# ------------------------------------------------------------------------------- metadata table


class CandidateDocumentRepository(Protocol):
    def record(self, row: dict[str, Any]) -> dict[str, Any]: ...  # {"persisted": bool, "reason": str | None}


# The table's own vocabulary (CHECK constraints, verified with zero-write probes on 2026-11-06): a stored file starts
# as UPLOADED; UC03's result store moves it to VALIDATED / REJECTED. UC03's lower-case document types are mapped to
# the table's types with UC03's own mapping (UC03 integrations/supabase/mapping.py, DOCUMENT_TYPES).
UPLOADED = "UPLOADED"


def _schema_type(document_types: Mapping[str, str] | None, document_type: str) -> str | None:
    """The table's document type for UC03's `document_type`; None when the table has no value for it.
    Without a mapping (tests that do not exercise the vocabulary) the type passes through unchanged."""
    if document_types is None:
        return document_type
    return document_types.get(document_type)


class SupabaseCandidateDocuments:
    """Writes `candidate_documents` (columns verified against the live schema: document_id, case_id,
    client_id, document_type, document_ref, file_name, file_size_bytes, mime_type, checksum_sha256,
    status, uploaded_at). Only for a case whose `onboarding_cases` row exists in the same tenant.
    `document_type` is written in the table's vocabulary (`document_types`, UC03's mapping)."""

    TABLE, CASES = "candidate_documents", "onboarding_cases"

    def __init__(self, *, url: str, service_role_key: SecretStr | str, schema: str = "public",
                 transport: httpx.BaseTransport | None = None, timeout_s: float = 15.0,
                 document_types: Mapping[str, str] | None = None):
        key = service_role_key if isinstance(service_role_key, SecretStr) else SecretStr(service_role_key)
        self.document_types = dict(document_types) if document_types is not None else None
        self._client = httpx.Client(base_url=url.rstrip("/") + "/rest/v1", timeout=timeout_s, transport=transport,
                                    headers={"apikey": key.get_secret_value(),
                                             "Authorization": f"Bearer {key.get_secret_value()}",
                                             "Accept-Profile": schema, "Content-Profile": schema})

    @classmethod
    def from_env(cls, environ: Mapping[str, str], *,
                 document_types: Mapping[str, str] | None = None) -> "SupabaseCandidateDocuments":
        return cls(url=_first_env(environ, STORAGE_URL_VARS), service_role_key=_first_env(environ, STORAGE_KEY_VARS),
                   schema=(environ.get("SUPABASE_SCHEMA") or "public").strip() or "public",
                   document_types=document_types)

    def schema_document_type(self, document_type: str) -> str | None:
        return _schema_type(self.document_types, document_type)

    def record(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            uuid.UUID(str(row["case_id"]))
        except ValueError:
            return {"persisted": False, "reason": "CASE_HAS_NO_ONBOARDING_CASES_ROW"}
        schema_type = self.schema_document_type(row["document_type"])
        if schema_type is None:
            return {"persisted": False, "reason": "DOCUMENT_TYPE_NOT_IN_SCHEMA"}
        row = {**row, "document_type": schema_type}
        try:
            owner = self._client.get(f"/{self.CASES}", params={"select": "case_id", "case_id": f"eq.{row['case_id']}",
                                                               "client_id": f"eq.{row['client_id']}"})
            if owner.status_code != 200:
                return {"persisted": False, "reason": f"ONBOARDING_CASES_READ_FAILED_HTTP_{owner.status_code}"}
            if not owner.json():
                return {"persisted": False, "reason": "CASE_HAS_NO_ONBOARDING_CASES_ROW"}
            response = self._client.post(f"/{self.TABLE}", json=row,
                                         headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
        except httpx.HTTPError:
            return {"persisted": False, "reason": "SUPABASE_UNREACHABLE"}
        if response.status_code in (200, 201, 204):
            return {"persisted": True, "reason": None}
        try:
            sqlstate = (response.json() or {}).get("code")
        except ValueError:
            sqlstate = None
        return {"persisted": False, "reason": f"INSERT_FAILED_HTTP_{response.status_code}"
                                              + (f"_SQLSTATE_{sqlstate}" if sqlstate else "")}


class InMemoryCandidateDocuments:
    """Tests only."""

    def __init__(self, *, known_cases: set[tuple[str, str]] | None = None,
                 document_types: Mapping[str, str] | None = None):
        self.known_cases = known_cases or set()  # (client_id, case_id) with an onboarding_cases row
        self.document_types = dict(document_types) if document_types is not None else None
        self.rows: dict[str, dict[str, Any]] = {}

    def schema_document_type(self, document_type: str) -> str | None:
        return _schema_type(self.document_types, document_type)

    def record(self, row: dict[str, Any]) -> dict[str, Any]:
        if (row["client_id"], row["case_id"]) not in self.known_cases:
            return {"persisted": False, "reason": "CASE_HAS_NO_ONBOARDING_CASES_ROW"}
        schema_type = self.schema_document_type(row["document_type"])
        if schema_type is None:
            return {"persisted": False, "reason": "DOCUMENT_TYPE_NOT_IN_SCHEMA"}
        self.rows.setdefault(row["document_id"], {**row, "document_type": schema_type})
        return {"persisted": True, "reason": None}


class LocalCandidateDocuments:
    """Development only (DOCUMENT_STORAGE_BACKEND=local): the candidate_documents row is appended to a local JSON-lines
    file. Reported as not persisted to the database, which is the truth."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
        return {"persisted": False, "reason": "LOCAL_DEVELOPMENT_STORAGE"}


# ----------------------------------------------------------------------------------- extraction


Extractor =Callable[[str, str], Any]  # (file path, document_type) -> awaitable UC03 ExtractionResult


def extraction_engine_status() -> dict[str, Any]:
    """Whether UC03's text engine (PyMuPDF) loads in this process. UC03's extractor treats a missing
    engine as an unreadable document (empty text); this makes that condition visible instead."""
    try:
        try:
            import fitz  # noqa: F401
        except ImportError:
            import pymupdf  # type: ignore # noqa: F401
    except ImportError as exc:
        reason = ("PyMuPDF native library failed to load (on Windows: install the Microsoft Visual C++ "
                  "Redistributable x64)") if "DLL" in str(exc) else "PyMuPDF is not installed (pip install pymupdf)"
        return {"available": False, "engine": "pymupdf", "reason": reason}
    return {"available": True, "engine": "pymupdf", "reason": None}


def uc03_extractor(uc03_repo_root: Path, *, online: bool | None = None) -> Extractor:
    """UC03's own extractors, composed like UC03's HybridDocumentExtractor (local first; UC03's
    LLMDocumentExtractor through the central LLM gateway below UC03's confidence threshold, only when
    LLM_MODE=online) with one difference: when local extraction read NO text at all (no text engine,
    an image-only file without OCR, an unopenable file) the LLM is not consulted. It would be sent empty
    text, and UC03's parser then defaults the confidence to 0.5 -- an unreadable file reported readable.
    Imported the way the UC03 adapter imports UC03 (its repo root on sys.path)."""
    from hr_onboarding_common.orchestration.adapters.uc03_documents import _ensure_uc03_importable

    _ensure_uc03_importable(uc03_repo_root)
    from src.sub_agent.extraction import LOW_CONFIDENCE, LLMDocumentExtractor, LocalDocumentExtractor  # type: ignore

    if online is None:
        online = (os.environ.get("LLM_MODE") or "").strip().lower() == "online"
    local = LocalDocumentExtractor()
    llm = LLMDocumentExtractor() if online else None

    async def extract(path: str, document_type: str):
        result = await local.extract(path, document_type)
        no_text = result.confidence == 0.0 and result.language is None
        if llm is None or result.confidence >= LOW_CONFIDENCE or no_text:
            return result, "uc03_local" + (" (no text read)" if no_text else "")
        return await llm.extract(path, document_type), "uc03_llm_gateway"

    return extract


_DATE = re.compile(r"(\d{1,2})[\/\-\. ](\d{1,2})[\/\-\. ](\d{2,4})")


def _parse_expiry(raw: str | None) -> str | None:
    """UC03's own expiry parsing (src/api/uc03.py): day-first numeric dates."""
    match = _DATE.match(raw or "")
    if not match:
        return None
    d, mo, y = (int(g) for g in match.groups())
    y = 2000 + y if y < 100 else y
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def metadata_from_extraction(result: Any) -> dict[str, Any]:
    """UC03's DocumentMetadata from UC03's ExtractionResult. Conservative where UC03's own upload
    endpoint is permissive:
      readable                 confidence > 0 (UC03's rule)
      required_fields_present  True only when every field UC03 looks for in this type was found; None
                               when UC03 has no field patterns for the type (UC03 then decides)
      extraction_confidence    0.0 when UC03's scanner suspects prompt injection, so the document can
                               never pass on extraction alone
      evidence_stored          True: the object is in Storage and was read back
      attestation_confirmed / visa_required / sponsorship_applicable: never inferred here."""
    fields: dict[str, Any] = dict(getattr(result, "extracted_fields", {}) or {})
    confidence = float(getattr(result, "confidence", 0.0) or 0.0)
    injection = bool(getattr(result, "injection_suspected", False))
    return {
        "readable": confidence > 0.0,
        "required_fields_present": (all(v not in (None, "") for v in fields.values()) if fields else None),
        "expiry_date": _parse_expiry(fields.get("expiry_date")),
        "evidence_stored": True,
        "extracted_name": (getattr(result, "extracted_name", None) or None),
        "extraction_confidence": 0.0 if injection else max(0.0, min(confidence, 1.0)),
    }


# -------------------------------------------------------------------------------------- service


@dataclass
class UploadReceipt:
    document_id: str
    document_type: str
    document_ref: str
    bucket: str
    storage_path: str
    storage_status: str  # created | exists
    file_name: str
    mime_type: str
    size_bytes: int
    checksum_sha256: str
    metadata_persisted: bool
    metadata_reason: str | None
    extraction: dict[str, Any]
    uploaded_at: str

    def view(self) -> dict[str, Any]:
        return dict(self.__dict__)


_METADATA_REFUSAL = {
    # reason -> (HTTP status, code): a strict (Supabase-authoritative) upload whose metadata row was not stored
    "CASE_HAS_NO_ONBOARDING_CASES_ROW": (409, "CASE_NOT_IN_DATABASE"),
    "DOCUMENT_TYPE_NOT_IN_SCHEMA": (422, "DOCUMENT_TYPE_NOT_IN_SCHEMA"),
    "SUPABASE_UNREACHABLE": (503, "DOCUMENT_METADATA_UNAVAILABLE"),
}


class DocumentIntakeService:
    """`strict` (Supabase is the authoritative store: UC03_STORE_MODE=supabase or ORCHESTRATOR_STATE_STORE=supabase):
    a document whose `candidate_documents` row cannot be written is refused -- its type before anything is stored,
    any other cause after the (idempotent) Storage upload -- and is never handed to UC03. Not strict: the row is
    best effort and the receipt says why it was skipped."""

    def __init__(self, *, storage: ObjectStorage, repository: CandidateDocumentRepository, extractor: Extractor,
                 max_bytes: int = DEFAULT_MAX_BYTES, strict: bool = False):
        self.storage, self.repository, self.extractor = storage, repository, extractor
        self.max_bytes = int(max_bytes)
        self.strict = bool(strict)
        self._locks = [threading.Lock() for _ in range(32)]
        self.engine = extraction_engine_status()

    @property
    def limits(self) -> dict[str, Any]:
        return {"max_bytes": self.max_bytes, "allowed_types": sorted(ALLOWED_TYPES), "extraction": self.engine}

    @staticmethod
    def check_file(data: bytes, content_type: str, max_bytes: int) -> str:
        """The file extension for an acceptable file, else DocumentUploadError."""
        mime = (content_type or "").split(";")[0].strip().lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in ALLOWED_TYPES:
            raise DocumentUploadError(415, "UNSUPPORTED_FILE_TYPE",
                                      f"file type '{mime or 'unknown'}' is not accepted; allowed: PDF, JPEG, PNG, TIFF")
        if not data:
            raise DocumentUploadError(400, "EMPTY_FILE", "the uploaded file is empty")
        if len(data) > max_bytes:
            raise DocumentUploadError(413, "FILE_TOO_LARGE", f"the file exceeds {max_bytes} bytes")
        extension, signatures = ALLOWED_TYPES[mime]
        if not any(data.startswith(sig) for sig in signatures):
            raise DocumentUploadError(415, "FILE_SIGNATURE_MISMATCH",
                                      f"the file content is not a valid {extension.upper()} file")
        return extension

    def upload(self, agent: Any, *, case_id: str, document_type: str, data: bytes, content_type: str,
               file_name: str | None, actor: dict[str, Any]) -> tuple[dict[str, Any], UploadReceipt]:
        # 1. Authorization and tenant ownership before anything is read or stored.
        agent.require_role(actor, "document_submitter_roles")
        state = agent.get_case(case_id, client_id=actor["client_id"])  # KeyError: unknown / other tenant
        case_id = state["case_id"]
        collection = state.get("document_collection") or {}
        required = {d.get("document_type") for d in collection.get("required_documents") or []}
        if not collection:
            raise DocumentUploadError(409, "COLLECTION_NOT_OPEN", "document collection has not been opened for this case")
        if document_type not in required:
            raise DocumentUploadError(422, "DOCUMENT_NOT_REQUIRED",
                                      f"'{document_type}' is not required by plan version {collection.get('plan_version')}")
        # 2. The file itself -- and, when Supabase is authoritative, whether its type can be recorded at all.
        extension = self.check_file(data, content_type, self.max_bytes)
        schema_type = getattr(self.repository, "schema_document_type", None)
        if self.strict and schema_type is not None and schema_type(document_type) is None:
            raise DocumentUploadError(422, "DOCUMENT_TYPE_NOT_IN_SCHEMA",
                                      f"'{document_type}' has no value in the candidate_documents.document_type CHECK "
                                      f"constraint, so the file cannot be recorded; nothing was stored")
        mime = (content_type or "").split(";")[0].strip().lower().replace("image/jpg", "image/jpeg")
        checksum = hashlib.sha256(data).hexdigest()
        document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{state['client_id']}:{case_id}:{document_type}:{checksum}"))
        try:
            path = object_path(state["client_id"], case_id, document_id, extension)
        except UnsafeObjectPath as exc:
            raise DocumentUploadError(422, "UNSAFE_OBJECT_PATH", str(exc)) from None
        ref = document_ref(self.storage.bucket, path)

        with self._locks[hash((case_id, document_type)) % len(self._locks)]:
            # 3. Storage, then the metadata row.
            storage_status = self.storage.upload(path, data, mime)
            uploaded_at = now_utc().isoformat()
            name = safe_file_name(file_name)
            meta = self.repository.record({
                "document_id": document_id, "case_id": case_id, "client_id": state["client_id"],
                "document_type": document_type, "document_ref": ref, "file_name": name,
                "file_size_bytes": len(data), "mime_type": mime, "checksum_sha256": checksum,
                "status": UPLOADED, "uploaded_at": uploaded_at})
            if self.strict and not meta["persisted"]:
                # The object is in Storage under a content-derived id, so a retry reuses it (idempotent); the
                # document is not submitted to UC03 without its authoritative row.
                status, code = _METADATA_REFUSAL.get(meta["reason"], (502, "DOCUMENT_METADATA_NOT_STORED"))
                raise DocumentUploadError(status, code, f"the document's candidate_documents row was not stored "
                                                        f"({meta['reason']}); the document was not submitted")
            # 4. UC03 validates what is in Storage, not what the browser sent.
            stored = self.storage.download(path)
            if hashlib.sha256(stored).hexdigest() != checksum:
                raise DocumentUploadError(502, "STORED_DOCUMENT_MISMATCH",
                                          "the stored document does not match the uploaded file")
            extraction_result, method = self._extract(stored, extension, document_type)
            metadata = metadata_from_extraction(extraction_result)
            # 5. The orchestrator's existing UC03 path: store the revision, validate, route.
            new_state = agent.submit_documents(case_id, documents=[{
                "document_type": document_type, "document_ref": ref, "source": "uploaded", "metadata": metadata}],
                actor=actor)

        log.info("orchestration.document_uploaded", extra={
            "case_id": case_id, "client_id": state["client_id"], "document_type": document_type,
            "document_id": document_id, "storage_status": storage_status, "metadata_persisted": meta["persisted"]})
        receipt = UploadReceipt(
            document_id=document_id, document_type=document_type, document_ref=ref, bucket=self.storage.bucket,
            storage_path=path, storage_status=storage_status, file_name=name, mime_type=mime, size_bytes=len(data),
            checksum_sha256=checksum, metadata_persisted=meta["persisted"], metadata_reason=meta["reason"],
            extraction={"method": method, "engine_available": self.engine["available"],
                        "engine_issue": self.engine["reason"], "readable": metadata["readable"],
                        "required_fields_present": metadata["required_fields_present"],
                        "expiry_date": metadata["expiry_date"], "name_found": metadata["extracted_name"] is not None,
                        "confidence": metadata["extraction_confidence"],
                        "injection_suspected": bool(getattr(extraction_result, "injection_suspected", False))},
            uploaded_at=uploaded_at)
        return new_state, receipt

    def _extract(self, data: bytes, extension: str, document_type: str):
        fd, tmp = tempfile.mkstemp(suffix=f".{extension}")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            return asyncio.run(self.extractor(tmp, document_type))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def signed_url(self, agent: Any, *, case_id: str, document_type: str, actor: dict[str, Any]) -> dict[str, Any]:
        agent.require_role(actor, "document_viewer_roles")
        state = agent.get_case(case_id, client_id=actor["client_id"])
        document = (state.get("submitted_documents") or {}).get(document_type)
        if not document:
            raise DocumentUploadError(404, "DOCUMENT_NOT_FOUND", f"no '{document_type}' has been submitted")
        parsed = parse_document_ref(document.get("document_ref", ""))
        if parsed is None:
            raise DocumentUploadError(409, "DOCUMENT_NOT_IN_STORAGE",
                                      f"'{document_type}' was not uploaded to document storage (reference only)")
        bucket, path = parsed
        # Defense in depth: the object must be this bucket's, under this tenant's and this case's prefix.
        if bucket != self.storage.bucket or not path.startswith(f"{state['client_id']}/{state['case_id']}/"):
            raise DocumentUploadError(403, "DOCUMENT_OUT_OF_SCOPE", "the document is not stored under this case")
        return {"document_type": document_type, "revision": document.get("revision"),
                "url": self.storage.signed_url(path, SIGNED_URL_TTL_S), "expires_in": SIGNED_URL_TTL_S}


def build_document_service(environ: Mapping[str, str], *, uc03_repo_root: Path) -> DocumentIntakeService | None:
    """From DOCUMENT_STORAGE_BUCKET / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / DOCUMENT_MAX_BYTES.
    None when no bucket is configured (the upload route then answers 503 -- never a silent fallback).
    DOCUMENT_STORAGE_BACKEND=local (development only, explicit) stores files under DOCUMENT_STORAGE_LOCAL_DIR instead."""
    strict = ((environ.get("UC03_STORE_MODE") or "").strip().lower() == "supabase"
              or (environ.get("ORCHESTRATOR_STATE_STORE") or "").strip().lower() == "supabase")
    backend = (environ.get("DOCUMENT_STORAGE_BACKEND") or "supabase").strip().lower()
    if backend == "local":
        if strict:
            # Supabase is the authoritative store here, and a local file can never be recorded there.
            raise ValueError("DOCUMENT_STORAGE_BACKEND=local cannot be combined with UC03_STORE_MODE=supabase or "
                             "ORCHESTRATOR_STATE_STORE=supabase")
        root = (environ.get("DOCUMENT_STORAGE_LOCAL_DIR") or "").strip()
        if not root:
            raise ValueError("DOCUMENT_STORAGE_BACKEND=local needs DOCUMENT_STORAGE_LOCAL_DIR")
        storage: ObjectStorage = LocalDiskObjectStorage(
            Path(root), base_url=(environ.get("DOCUMENT_STORAGE_PUBLIC_BASE_URL") or "http://127.0.0.1:8000").strip())
        repository: CandidateDocumentRepository = LocalCandidateDocuments(Path(root) / "candidate_documents.jsonl")
    elif backend == "supabase":
        supabase = SupabaseObjectStorage.from_env(environ)
        if supabase is None:
            return None
        storage, repository = supabase, SupabaseCandidateDocuments.from_env(
            environ, document_types=uc03_document_types(uc03_repo_root))
    else:
        raise ValueError("DOCUMENT_STORAGE_BACKEND must be 'supabase' or 'local'")
    return DocumentIntakeService(storage=storage, repository=repository,
                                 extractor=uc03_extractor(uc03_repo_root),
                                 max_bytes=int(environ.get("DOCUMENT_MAX_BYTES") or DEFAULT_MAX_BYTES), strict=strict)


def uc03_document_types(uc03_repo_root: Path) -> dict[str, str]:
    """UC03's own mapping of its document types to `candidate_documents.document_type` (UC03 integrations/supabase/
    mapping.py), loaded from UC03's repo root and checked to come from there (`integrations` is a generic name)."""
    from hr_onboarding_common.orchestration.adapters.uc03_documents import _ensure_uc03_importable

    _ensure_uc03_importable(uc03_repo_root)
    import importlib

    module = importlib.import_module("integrations.supabase.mapping")
    if Path(uc03_repo_root).resolve() not in Path(module.__file__ or "").resolve().parents:
        raise ImportError("integrations.supabase.mapping was loaded from outside UC03's repo root")
    return dict(module.DOCUMENT_TYPES)
