"""Tamper evidence and a preventive sensitive-value guard for the audit log.

Two separate concerns live here because both sit on the same boundary --
`AuditLog.append` -- and both must run before an entry is accepted.

1. **Preventive sensitive-value guard.** `invariants.SENSITIVE_IN_AUDIT`
   already detects a salary or an email that reached the audit log, but
   it only runs after the fact (`hooks.after_hris_write`), so the value
   is already persisted by the time anyone knows. `check_entry` rejects
   the entry instead, at append time.

   The check is structural, like `llm/guard.py`: it refuses forbidden
   KEY names and `SecretStr`-shaped values, rather than pattern-matching
   for things that look like salaries. A field *name* ("salary_and_terms")
   is still allowed as a value, because audit entries are expected to
   name fields -- what they must never carry is the figure.

2. **Hash-linked sequence.** Each accepted entry is stamped with its
   position, the previous entry's digest, and its own digest over
   (previous digest + canonical content). `verify_chain` then detects a
   removed, reordered, or edited entry.

   **What this does NOT give you.** The chain is keyed by nothing: an
   actor who can write the whole log can recompute every digest and
   produce a clean chain. It is tamper *evidence* against partial edits,
   not tamper *proofing*. Real protection needs the digest anchored
   somewhere the writer does not control -- an append-only store, a
   signed periodic checkpoint, or SIEM export. See docs/known-gaps.md.
"""

import hashlib
import json
from enum import StrEnum
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ConfigDict, SecretStr

from .models import AuditEntry

# The digest that precedes the first entry in a chain.
GENESIS_HASH = "0" * 64

# What `str(SecretStr(...))` renders as; mirrors llm/guard.py.
_MASKED = "**********"

# Key names that must never appear in an audit entry's before/after.
# Deliberately the same vocabulary as llm.guard.FORBIDDEN_INPUT_NAMES:
# one list of sensitive concepts, enforced at both boundaries.
FORBIDDEN_AUDIT_KEYS = frozenset(
    {
        "salary",
        "salary_and_terms",
        "compensation",
        "compensation_terms",
        "bank",
        "bank_account",
        "iban",
        "loan",
        "loan_details",
    }
)


class SensitiveValueInAuditError(ValueError):
    """An audit entry carried a sensitive key or a secret value.

    Names the offending KEYS only -- never their values, since this
    message may itself reach a log or an error response.
    """

    def __init__(self, action: str, offending: list[str]) -> None:
        self.action = action
        self.offending = sorted(offending)
        super().__init__(
            f"audit entry {action!r} carries forbidden keys: {', '.join(self.offending)}"
        )


def _offending_keys(payload: Optional[dict]) -> set[str]:
    """Walk a before/after dict and collect keys that must not be there."""
    found: set[str] = set()

    def walk(node: Any, key: Optional[str]) -> None:
        if key is not None and key.lower() in FORBIDDEN_AUDIT_KEYS:
            found.add(key)
        if isinstance(node, SecretStr):
            found.add(key or "<root>")
        elif isinstance(node, str):
            if _MASKED in node:
                found.add(key or "<root>")
        elif isinstance(node, dict):
            for child_key, child in node.items():
                walk(child, str(child_key))
        elif isinstance(node, (list, tuple, set)):
            for child in node:
                walk(child, key)

    walk(payload, None)
    return found


def check_entry(entry: AuditEntry) -> None:
    """Raise SensitiveValueInAuditError if this entry may not be stored."""
    offending = _offending_keys(entry.before) | _offending_keys(entry.after)
    if offending:
        raise SensitiveValueInAuditError(entry.action, sorted(offending))


# Fields that the digest covers. `prev_hash` and `entry_hash` are
# excluded (one is an input to the digest, the other is its output);
# `sequence` is included so a reordering changes the digest.
_DIGEST_FIELDS = (
    "log_id",
    "case_id",
    "request_id",
    "actor_type",
    "actor_id",
    "action",
    "before",
    "after",
    "timestamp",
    "reference",
    "sequence",
)


def entry_digest(entry: AuditEntry, prev_hash: str) -> str:
    """SHA-256 over the previous digest plus this entry's canonical content."""
    dumped = entry.model_dump(mode="json")
    content = {name: dumped.get(name) for name in _DIGEST_FIELDS}
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{prev_hash}\n{canonical}".encode("utf-8")).hexdigest()


def chain_entry(entry: AuditEntry, sequence: int, prev_hash: str) -> AuditEntry:
    """Return the entry stamped with its position and digests.

    `AuditEntry` is frozen, so this returns a new object rather than
    mutating the caller's.
    """
    positioned = entry.model_copy(update={"sequence": sequence, "prev_hash": prev_hash})
    return positioned.model_copy(update={"entry_hash": entry_digest(positioned, prev_hash)})


class ChainViolationCode(StrEnum):
    UNCHAINED_ENTRY = "UNCHAINED_ENTRY"
    SEQUENCE_BREAK = "SEQUENCE_BREAK"
    PREV_HASH_MISMATCH = "PREV_HASH_MISMATCH"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"


class ChainViolation(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: ChainViolationCode
    # Position in the list as given, so a caller can point at the entry
    # even when its own `sequence` is the thing that is wrong.
    index: int
    log_id: str


def verify_chain(entries: Iterable[AuditEntry]) -> list[ChainViolation]:
    """Check a sequence of entries. Empty list means the chain holds.

    Entries must be the COMPLETE log, in append order. A filtered subset
    (for example `entries_for_case`, which is what the per-case audit
    endpoint returns) has gaps in `sequence` and broken `prev_hash`
    links by construction, and will report violations on a perfectly
    intact log. Use `case_status.verify_audit_integrity`, which passes
    the whole log.

    A violation does not say *what* changed, only that the chain no
    longer reproduces -- the honest limit of a hash chain.

    Not detected, by design of the construction:
    - **Truncation of the most recent entries.** Dropping entries off
      the tail leaves a shorter but internally valid chain. Detecting it
      needs a high-water mark held outside the log.
    - **Wholesale rewriting.** With no key and no external anchor, an
      actor who can rewrite every entry can recompute every digest.
    """
    violations: list[ChainViolation] = []
    expected_prev = GENESIS_HASH

    for index, entry in enumerate(entries):
        if entry.entry_hash is None or entry.prev_hash is None or entry.sequence is None:
            violations.append(
                ChainViolation(
                    code=ChainViolationCode.UNCHAINED_ENTRY,
                    index=index,
                    log_id=entry.log_id,
                )
            )
            continue

        if entry.sequence != index:
            violations.append(
                ChainViolation(
                    code=ChainViolationCode.SEQUENCE_BREAK, index=index, log_id=entry.log_id
                )
            )

        if entry.prev_hash != expected_prev:
            violations.append(
                ChainViolation(
                    code=ChainViolationCode.PREV_HASH_MISMATCH, index=index, log_id=entry.log_id
                )
            )

        if entry.entry_hash != entry_digest(entry, entry.prev_hash):
            violations.append(
                ChainViolation(
                    code=ChainViolationCode.DIGEST_MISMATCH, index=index, log_id=entry.log_id
                )
            )

        expected_prev = entry.entry_hash

    return violations
