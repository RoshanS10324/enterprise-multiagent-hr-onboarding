"""Object paths for source onboarding documents (decision B4 - not decided).

Only the part of the design that is the same whichever storage B4 picks:
the object path is built from server-controlled ids, never from the uploaded
file name, so it cannot traverse directories, cannot collide across clients,
and carries no personal data.

    <client_id>/<case_id>/<document_id>.docx

No bucket exists and nothing here uploads, downloads or creates storage. The
bucket name below is the PROPOSED one from
docs/schema/proposed/uc01_b2_b4_b7_b9_draft.sql, pending B4.
"""

from __future__ import annotations

import re
import uuid

PROPOSED_BUCKET = "uc01-onboarding-documents"
_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_EXTENSIONS = frozenset({"docx"})


class UnsafeObjectPath(ValueError):
    pass


def _uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise UnsafeObjectPath(f"{what} must be a UUID") from None


def case_object_path(client_id: str, case_id: str, document_id: str, extension: str = "docx") -> str:
    """The storage path for one document of one case.

    Raises UnsafeObjectPath for anything that is not a plain client id, a
    UUID case/document id, or an allowed extension - so "..", "/", "\\",
    absolute paths and file-name text can never reach the path.
    """
    if not isinstance(client_id, str) or not _CLIENT_ID.match(client_id):
        raise UnsafeObjectPath("client_id contains characters not allowed in an object path")
    if extension not in _EXTENSIONS:
        raise UnsafeObjectPath(f"extension must be one of {sorted(_EXTENSIONS)}")
    return f"{client_id}/{_uuid(case_id, 'case_id')}/{_uuid(document_id, 'document_id')}.{extension}"


def client_of(path: str) -> str:
    """The client segment of a path built by case_object_path (for isolation checks)."""
    client = path.split("/", 1)[0]
    if not _CLIENT_ID.match(client):
        raise UnsafeObjectPath("not a UC-01 document path")
    return client


#: Extensions a SOURCE onboarding document may be stored with.
SOURCE_EXTENSIONS = frozenset({"docx", "pdf"})
#: Folder, inside a client's folder, for documents received before a case exists.
INTAKE_FOLDER = "uc01-intake"
#: Namespace for document ids: the same client + the same file bytes always
#: give the same id, so a retried or repeated upload can never create a
#: second object (idempotent storage, duplicate detection by content).
_DOCUMENT_NS = uuid.UUID("6f1c2f7e-2b1a-4f53-9a37-0c2b8e5d9a11")


def source_document_id(client_id: str, sha256: str) -> str:
    if not isinstance(client_id, str) or not _CLIENT_ID.match(client_id):
        raise UnsafeObjectPath("client_id contains characters not allowed in an object path")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise UnsafeObjectPath("sha256 must be 64 lower-case hex characters")
    return str(uuid.uuid5(_DOCUMENT_NS, f"{client_id}:{sha256}"))


def intake_object_path(client_id: str, document_id: str, extension: str) -> str:
    """<client_id>/uc01-intake/<document_id>.<ext> - server-made only, so no
    file-name text (which can carry a person's name) ever reaches the path."""
    if not isinstance(client_id, str) or not _CLIENT_ID.match(client_id):
        raise UnsafeObjectPath("client_id contains characters not allowed in an object path")
    if extension not in SOURCE_EXTENSIONS:
        raise UnsafeObjectPath(f"extension must be one of {sorted(SOURCE_EXTENSIONS)}")
    return f"{client_id}/{INTAKE_FOLDER}/{_uuid(document_id, 'document_id')}.{extension}"
