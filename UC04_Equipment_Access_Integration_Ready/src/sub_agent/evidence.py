"""Mock IT ticket facts and authoritative completion evidence (BR-09, BR-17, GR-07). Approval or a created ticket is never
proof of activation, delivery or collection."""

from dataclasses import dataclass, field

from sub_agent.contracts import CaseConfig
from sub_agent.guardrails import sanitize_free_text
from sub_agent.states import EvidenceType, TicketStatus

_RANK = {TicketStatus.CREATED: 0, TicketStatus.IN_PROGRESS: 1, TicketStatus.COMPLETED: 2}


@dataclass
class TicketFacts:
    ticket_id: str | None = None
    latest: TicketStatus | None = None
    reason: str | None = None


def collect_ticket_facts(inp, stored_tickets: dict[str, str | None]) -> tuple[dict[str, TicketFacts], list[tuple[str, str]]]:
    """Apply mock ticket updates in order. Returns facts per request and ignored updates as (request_id, code).

    Once a ticket is REJECTED, later updates are ignored, so a rejected request is never silently retried.
    """
    facts = {rid: TicketFacts(ticket, TicketStatus.CREATED if ticket else None) for rid, ticket in stored_tickets.items()}
    ignored: list[tuple[str, str]] = []
    for u in inp.mock_it_ticket_updates:
        f = facts.get(u.request_id)
        if f is None:
            ignored.append((u.request_id, "UNKNOWN_REQUEST"))
        elif f.latest == TicketStatus.REJECTED:
            ignored.append((u.request_id, "UPDATE_AFTER_REJECTION"))
        elif f.ticket_id and u.ticket_id != f.ticket_id:
            ignored.append((u.request_id, "TICKET_ID_MISMATCH"))
        elif u.status != TicketStatus.REJECTED and f.latest is not None and _RANK[u.status] < _RANK[f.latest]:
            ignored.append((u.request_id, "STATUS_REGRESSION"))
        else:
            f.ticket_id, f.latest = u.ticket_id, u.status
            f.reason = sanitize_free_text(u.reason) if u.status == TicketStatus.REJECTED else None
    return facts, ignored


def is_authoritative(evidence_ref: str | None, asserted_by_role: str | None, kind: EvidenceType, cfg: CaseConfig) -> bool:
    """Evidence counts only with a case-stored reference asserted by a role configured for that evidence type."""
    roles = (cfg.evidence_roles.value or {}).get(kind, [])
    return bool(evidence_ref) and asserted_by_role in roles


def latest_by_request(records: list, request_id: str):
    matching = [r for r in records if r.request_id == request_id]
    return matching[-1] if matching else None


@dataclass
class EvidenceFindings:
    """Why evidence was not accepted, as (code, reason). Shown to a human; nothing is accepted on a guess."""

    problems: list[tuple[str, str]] = field(default_factory=list)
