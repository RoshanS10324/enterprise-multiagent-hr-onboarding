"""Identifier profiles: which shapes of case, employee, client, actor and request identifiers UC-04 accepts.

LOCAL_SIMULATION (the default) is exactly what UC-04 has always accepted: synthetic `SYN-*` identifiers only (GR-13). Nothing changes unless
the profile is switched on explicitly.

PLATFORM accepts the shapes the shared platform uses (uuid case id, `EMP-nnnnnn` employee numbers, upper-case and hyphenated client ids,
`user:...` actor ids). It exists so that UC-04 can run against real parent records, and it is a shared-contract decision: it must be agreed
with the integration team and the governance owner before anyone sets it outside a test. It never accepts an e-mail address, a space or a
free-text value as an identifier.

UC-04 never reads its configuration from the environment (a packaging safeguard forbids it). The layer that embeds UC-04 (the worker, the
API launcher, a test) reads its own setting and calls `guardrails.identity.configure(name)` **before anything from `sub_agent` is imported**
(importing any `sub_agent` module loads the contracts, which fix the profile). This module lives outside the package, in `guardrails/`, for
that reason: it imports nothing from `sub_agent`. The profile is then
fixed for the life of the process: a call to `configure` after the contracts were loaded raises, so it cannot silently change what the
running process accepts. An unknown profile name is refused, so a typo can never widen what UC-04 accepts.
"""

from dataclasses import dataclass

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_TOKEN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"


@dataclass(frozen=True)
class IdentityPatterns:
    name: str
    case_id: str
    employee_ref: str
    request_key: str
    user_ref: str
    evidence_ref: str
    approval_id: str
    ticket_id: str
    role: str  # also used for client ids, role ids and approver roles


LOCAL_SIMULATION = IdentityPatterns(
    name="LOCAL_SIMULATION", case_id=r"^SYN-CASE-\d{3,}$", employee_ref=r"^SYN-EMP-\d{3,}$", request_key=r"^SYN-REQ-\d{3,}$",
    user_ref=r"^SYN-USER-\d{3,}$", evidence_ref=r"^SYN-EVD-\d{3,}$", approval_id=r"^SYN-APR-\d{3,}$", ticket_id=r"^SYN-TKT-\d{4,}$",
    role=r"^[a-z][a-z0-9_]{1,40}$")

PLATFORM = IdentityPatterns(
    name="PLATFORM", case_id=_UUID, employee_ref=r"^[A-Za-z][A-Za-z0-9_-]{2,40}$", request_key=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    user_ref=_TOKEN, evidence_ref=_TOKEN, approval_id=_TOKEN, ticket_id=_TOKEN, role=r"^[A-Za-z][A-Za-z0-9_-]{1,40}$")

PROFILES = {p.name: p for p in (LOCAL_SIMULATION, PLATFORM)}


def resolve(name: str | None) -> IdentityPatterns:
    key = (name or "").strip().upper() or "LOCAL_SIMULATION"
    if key not in PROFILES:
        raise ValueError(f"identity profile must be one of {sorted(PROFILES)}, not an unknown value")  # never echo the value
    return PROFILES[key]


_selected: IdentityPatterns = LOCAL_SIMULATION
_frozen = False


def configure(name: str | None) -> IdentityPatterns:
    """Choose the profile. Must run before `sub_agent.contracts` is imported; choosing the profile already in force is allowed."""
    global _selected
    chosen = resolve(name)
    if _frozen and chosen is not _selected:
        raise RuntimeError("the identity profile is fixed once the contracts are loaded; configure it before importing sub_agent.contracts")
    _selected = chosen
    return _selected


def active() -> IdentityPatterns:
    """The profile in force. Reading it freezes the choice for the rest of the process (called once, by `sub_agent.contracts`)."""
    global _frozen
    _frozen = True
    return _selected
