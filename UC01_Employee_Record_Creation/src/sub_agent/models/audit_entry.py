from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import new_id, utc_now
from .enums import ActorType


class AuditEntry(BaseModel):
    # Audit entries are append-only by nature, so frozen=True (a plain,
    # static class-level lock) is the right tool here, unlike OnboardingForm
    # which needs a status-dependent mutability switch.
    model_config = ConfigDict(frozen=True)

    log_id: str = Field(default_factory=new_id)
    # At U1 (start gate) no case exists yet, so an entry may be keyed by
    # request_id alone; once a case exists, case_id is used instead (or as
    # well). At least one of the two must be present.
    case_id: Optional[str] = None
    request_id: Optional[str] = None
    actor_type: ActorType
    actor_id: str
    action: str
    before: Optional[dict] = None
    after: Optional[dict] = None
    timestamp: datetime = Field(default_factory=utc_now)
    reference: str
    # Which client the entry belongs to, when the entry itself knows (e.g.
    # ACCESS_DENIED, which deliberately carries no case_id). Optional: most
    # entries are attributed through their case or the request scope
    # (audit_scope.py). NOT part of the hash-chain digest
    # (audit_integrity._DIGEST_FIELDS), so existing chains are unaffected.
    client_id: Optional[str] = None

    # Tamper-evidence fields, stamped by the AuditLog adapter on append
    # (see audit_integrity.chain_entry). Optional so that an entry
    # constructed by a caller -- which is every call site, none of which
    # knows its own position -- still validates, and so that entries
    # written before chaining existed remain readable.
    sequence: Optional[int] = None
    prev_hash: Optional[str] = None
    entry_hash: Optional[str] = None

    @model_validator(mode="after")
    def _require_case_or_request_id(self) -> "AuditEntry":
        if self.case_id is None and self.request_id is None:
            raise ValueError("AuditEntry requires at least one of case_id or request_id")
        return self
