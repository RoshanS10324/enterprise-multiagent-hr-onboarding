"""Presentation-only execution tracing for UC-01 verification runs.

This module prints; it never decides. Nothing here reads a repository on
its own initiative, mutates a record, or re-implements a business rule:
callers hand it values the real services already returned, and it formats
them. That keeps `scripts/verify_requirements.py --verbose` honest -- what
it shows is what actually executed.

Two jobs:

* `Redactor` -- a single, consistent mask for anything sensitive, applied
  to every value on its way to the terminal. Salary, bank and loan data
  stay inside HR and payroll scopes (CLAUDE.md), so they must never reach
  a console, a CI log or a screenshot. The redactor masks by field name
  AND by registered literal, because a salary string can also turn up
  embedded in free text (an approval `reason`, an error message).
* `Tracer` -- the labelled sections (INPUT / PROCESSING / BUSINESS RULE /
  ... / ASSERTION RESULT) the detailed report is built from.

Synthetic data only, like everything else in UC-01.
"""

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Optional, TextIO

from pydantic import BaseModel, SecretStr

from .models import AuditEntry

MASK = "***REDACTED***"

# Printed where the implementation genuinely stores nothing, so a reader
# can tell "the code does not record this" apart from "the value was
# empty". Never use it to paper over a value we simply did not look up.
NOT_CAPTURED = "<not captured by the implementation>"

# Printed where a value exists but is computed on read rather than stored.
def derived(what: str) -> str:
    return f"<derived at read time by {what}, not stored>"

# Field names whose VALUE is never printed, whatever it holds. Matched
# case-insensitively against both our field names and the target-system
# field names they map to (see hris_mapping.HRIS_FIELD_MAPPING).
SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "salary_and_terms",
        "compensation_terms",
        "salary",
        "offer_terms",
        "sensitive_payload",
        "bank",
        "bank_account",
        "account_number",
        "iban",
        "loan",
        "loan_amount",
        "password",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "dedup_hmac_key",
        "other_fingerprint",
    }
)

# Substrings that make a field name sensitive even when the exact name is
# not listed above (e.g. "employee_salary", "zoho_access_token").
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "salary",
    "compensation",
    "bank",
    "iban",
    "loan",
    "password",
    "secret",
    "token",
    "api_key",
)

# A registered secret is also scrubbed token by token, so a leak of just
# the figure ("118000") out of "Base: 118000 SYNTHETIC/yr" is caught as
# well as a verbatim one. Only tokens containing a digit and long enough
# to be worth hiding are registered that way: "Base" and "standard" are
# not secrets, and masking them would make the report unreadable.
_MIN_TOKEN_LEN = 4
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def is_sensitive_field(name: str) -> bool:
    lowered = name.strip().lower()
    if lowered in SENSITIVE_FIELD_NAMES:
        return True
    return any(part in lowered for part in _SENSITIVE_SUBSTRINGS)


class Redactor:
    """Masks sensitive values on their way to the terminal.

    Two independent defences, because either one alone has a gap:

    * by field name -- catches a value under a known-sensitive key even if
      the exact text was never registered;
    * by registered literal -- catches a known secret that turns up under
      an innocuous key, or inside a free-text string.
    """

    def __init__(self) -> None:
        self._literals: set[str] = set()

    def register_secret(self, value: Any) -> None:
        """Remember a literal to scrub from every string printed later.

        Accepts a SecretStr or a plain string.
        """
        if value is None:
            return
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        text = str(value)
        if not text.strip():
            return
        self._literals.add(text)
        for token in _TOKEN_RE.findall(text):
            if len(token) >= _MIN_TOKEN_LEN and any(ch.isdigit() for ch in token):
                self._literals.add(token)

    @property
    def registered(self) -> frozenset[str]:
        return frozenset(self._literals)

    def scrub_text(self, text: str) -> str:
        """Replace every registered literal inside `text` with the mask.

        Longest first, so masking "118000" does not stop the longer
        "Base: 118000 SYNTHETIC/yr" from being recognised first.
        """
        out = text
        for literal in sorted(self._literals, key=len, reverse=True):
            if literal in out:
                out = out.replace(literal, MASK)
        return out

    def redact(self, value: Any, key: Optional[str] = None) -> Any:
        """Return a JSON-safe copy of `value` with sensitive parts masked.

        `key` is the field name `value` was found under, when known: a
        sensitive name masks the value outright without inspecting it.
        """
        if key is not None and is_sensitive_field(key):
            return MASK
        if isinstance(value, SecretStr):
            return MASK
        if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
            return value
        if isinstance(value, Enum):
            return self.redact(value.value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, BaseModel):
            # mode="json" already renders SecretStr as "**********"; the
            # per-key pass below still runs, so a sensitive field ends up
            # with our mask rather than pydantic's placeholder.
            return self.redact(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {str(k): self.redact(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            return self.scrub_text(value)
        return self.scrub_text(str(value))

    def dumps(self, value: Any, indent: int = 2) -> str:
        return json.dumps(self.redact(value), indent=indent, sort_keys=False, default=str)


# --- what kind of external system a gateway actually talks to -------------

# Decided from the object that is really wired in, never assumed. A
# successful write through StubHrisGateway is a SIMULATED write: no Zoho
# record exists anywhere as a result of it.
_SIMULATED_PREFIXES = ("Stub", "InMemory", "Fake")
_MOCKED_PREFIXES = ("Mock",)


def external_mode(gateway: Any) -> str:
    """SIMULATED / MOCKED / LIVE, from the gateway class actually in use."""
    name = type(gateway).__name__
    if name.startswith(_SIMULATED_PREFIXES):
        return "SIMULATED"
    if name.startswith(_MOCKED_PREFIXES):
        return "MOCKED"
    return "LIVE"


def external_mode_detail(gateway: Any) -> dict[str, str]:
    return {
        "mode": external_mode(gateway),
        "gateway_class": type(gateway).__name__,
        "gateway_module": type(gateway).__module__,
    }


# --- state snapshots and diffing -----------------------------------------


def diff_states(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Only the keys whose value changed, as {"before": .., "after": ..}."""
    changed: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        old = before.get(key, "<absent>")
        new = after.get(key, "<absent>")
        if old != new:
            changed[key] = {"before": old, "after": new}
    return changed


class AuditTap:
    """Marks a position in the audit log so a caller can report exactly
    the entries one operation appended -- never a guess, never a replay.
    """

    def __init__(self, audit_log: Any) -> None:
        self._audit_log = audit_log
        self._mark = len(audit_log.entries())

    def reset(self) -> None:
        self._mark = len(self._audit_log.entries())

    def new_entries(self) -> list[AuditEntry]:
        return self._audit_log.entries()[self._mark :]


def audit_summary(entries: Iterable[AuditEntry], redactor: Redactor) -> list[dict[str, Any]]:
    """One compact dict per audit entry, redacted."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        out.append(
            {
                "action": entry.action,
                "actor": f"{entry.actor_type.value}:{entry.actor_id}",
                "timestamp": entry.timestamp.isoformat(),
                "case_id": entry.case_id,
                "before": redactor.redact(entry.before),
                "after": redactor.redact(entry.after),
            }
        )
    return out


# --- the tracer ------------------------------------------------------------

SECTION_KINDS = (
    "INPUT",
    "PROCESSING",
    "BUSINESS RULE",
    "POLICY CHECK",
    "APPROVAL REQUIRED",
    "APPROVAL DECISION",
    "STATE CHANGE",
    "EXTERNAL OP",
    "OUTPUT",
    "AUDIT EVENT",
    "ASSERTION RESULT",
    "ERROR",
)


@dataclass
class RunLedger:
    """Totals for the end-of-run summary.

    Recorded whether or not the tracer is printing, so the concise run
    reports the same counts as the verbose one. Everything here is a
    tally of what actually happened -- no expected values are seeded.
    """

    scenarios: list[str] = field(default_factory=list)
    assertions: list[tuple[bool, str, str]] = field(default_factory=list)
    external_ops: Counter = field(default_factory=Counter)
    external_gateways: set[str] = field(default_factory=set)
    approval_events: list[dict[str, Any]] = field(default_factory=list)
    policy_flags: list[dict[str, Any]] = field(default_factory=list)

    def record_scenario(self, scenario_id: str) -> None:
        self.scenarios.append(scenario_id)

    def record_assertion(self, ok: bool, requirement: str, label: str) -> None:
        self.assertions.append((ok, requirement, label))

    def record_external_op(self, mode: str, gateway_class: str, count: int = 1) -> None:
        self.external_ops[mode] += count
        self.external_gateways.add(gateway_class)

    def record_approval(self, checkpoint: Any, status: Any, approval_id: Optional[str]) -> None:
        self.approval_events.append(
            {
                "checkpoint": getattr(checkpoint, "value", checkpoint),
                "status": getattr(status, "value", status),
                "approval_id": approval_id,
            }
        )

    def record_policy_flag(self, flag: dict[str, Any]) -> None:
        self.policy_flags.append(flag)

    # -- derived totals ---------------------------------------------------

    @property
    def passed(self) -> int:
        return sum(1 for ok, *_ in self.assertions if ok)

    @property
    def failed(self) -> int:
        return sum(1 for ok, *_ in self.assertions if not ok)

    def approval_outcomes(self) -> dict[str, Counter]:
        """Final status per checkpoint, counted per approval id.

        An approval that was reported PENDING and later decided is counted
        once, at its last reported status -- otherwise every gate would
        look like it both pended and resolved.
        """
        latest: dict[str, tuple[str, str]] = {}
        for index, event in enumerate(self.approval_events):
            key = event["approval_id"] or f"<unidentified-{index}>"
            latest[key] = (event["checkpoint"], event["status"])
        outcomes: dict[str, Counter] = {}
        for checkpoint, status in latest.values():
            outcomes.setdefault(checkpoint, Counter())[status] += 1
        return outcomes


def check_approver_against_policy(
    approval: Any,
    allowed_roles: Iterable[str],
    actor_roles: dict[str, str],
) -> dict[str, Any]:
    """Compare the actor an approval is addressed to against the roles the
    policy file actually permits for its checkpoint.

    `actor_roles` maps user_id -> role for the actors this run created, so
    an actor the run never built is reported as unknown rather than
    guessed at. Returns a verdict dict; it never changes the approval.
    """
    allowed = sorted(allowed_roles)
    requested_to = getattr(approval, "requested_to", None)
    role = actor_roles.get(requested_to) if requested_to else None

    if requested_to is None:
        verdict, detail = "UNKNOWN", "the approval names no requested_to actor"
    elif role is None:
        verdict, detail = (
            "UNKNOWN",
            f"actor {requested_to!r} was not created by this run, so its role is unknown here",
        )
    elif role in allowed:
        verdict, detail = "OK", f"{requested_to} holds {role}, which the policy permits"
    else:
        verdict, detail = (
            "MISMATCH",
            f"{requested_to} holds {role}, but the policy permits only {allowed} "
            f"-- this actor cannot action the approval addressed to them",
        )

    return {
        "checkpoint": getattr(getattr(approval, "checkpoint", None), "value", None),
        "approval_id": getattr(approval, "approval_id", None),
        "requested_to": requested_to,
        "requested_to_role": role or NOT_CAPTURED,
        "allowed_roles (policy)": allowed,
        "verdict": verdict,
        "detail": detail,
    }

_RULE = "=" * 78
_SUBRULE = "-" * 78


class Tracer:
    """Prints the detailed report. A disabled tracer is a no-op, so the
    same call sites serve both the concise and the verbose run.
    """

    def __init__(
        self,
        enabled: bool,
        redactor: Optional[Redactor] = None,
        stream: Optional[TextIO] = None,
        ledger: Optional[RunLedger] = None,
    ) -> None:
        self.enabled = enabled
        self.redactor = redactor or Redactor()
        # Totals are tallied even when printing is off, so the concise
        # run's summary matches the verbose run's.
        self.ledger = ledger if ledger is not None else RunLedger()
        # Left as None rather than resolved to sys.stdout here: a tracer
        # built at import time would otherwise capture whatever stdout
        # was then, and ignore a later redirect (which is how the tests
        # and `... > report.txt` both read it).
        self._stream = stream

    # -- low level ---------------------------------------------------------

    def _write(self, text: str = "") -> None:
        if self.enabled:
            print(text, file=self._stream if self._stream is not None else sys.stdout)

    def emit(
        self,
        kind: str,
        title: str,
        data: Any = None,
        fields: Optional[dict[str, Any]] = None,
        note: Optional[str] = None,
    ) -> None:
        """One labelled section. `kind` must be one of SECTION_KINDS so a
        typo cannot invent a label the reader has to guess at.
        """
        if kind not in SECTION_KINDS:
            raise ValueError(f"unknown trace section kind {kind!r}")
        if not self.enabled:
            return
        self._write(f"  [{kind}] {self.redactor.scrub_text(title)}")
        if note:
            self._write(f"      note: {self.redactor.scrub_text(note)}")
        if fields:
            width = max(len(str(k)) for k in fields)
            for name, value in fields.items():
                shown = self.redactor.redact(value, key=str(name))
                if isinstance(shown, (dict, list)):
                    shown = json.dumps(shown, default=str)
                self._write(f"      {str(name).ljust(width)} : {shown}")
        if data is not None:
            for line in self.redactor.dumps(data).splitlines():
                self._write(f"      {line}")

    # -- structure ---------------------------------------------------------

    def scenario(
        self,
        scenario_id: str,
        requirement: str,
        purpose: str,
        expected: str,
    ) -> None:
        self.ledger.record_scenario(scenario_id)
        if not self.enabled:
            return
        self._write()
        self._write(_RULE)
        self._write(f"SCENARIO {scenario_id}")
        self._write(f"  REQUIREMENT : {requirement}")
        self._write(f"  PURPOSE     : {purpose}")
        self._write(f"  EXPECTED    : {expected}")
        self._write(_SUBRULE)

    def heading(self, text: str) -> None:
        self._write()
        self._write(_SUBRULE)
        self._write(text)
        self._write(_SUBRULE)

    # -- convenience wrappers ---------------------------------------------

    def input(self, title: str, data: Any = None, **kwargs: Any) -> None:
        self.emit("INPUT", title, data=data, **kwargs)

    def processing(self, title: str, **kwargs: Any) -> None:
        self.emit("PROCESSING", title, **kwargs)

    def rule(self, title: str, **kwargs: Any) -> None:
        self.emit("BUSINESS RULE", title, **kwargs)

    def output(self, title: str, data: Any = None, **kwargs: Any) -> None:
        self.emit("OUTPUT", title, data=data, **kwargs)

    def error(self, title: str, **kwargs: Any) -> None:
        self.emit("ERROR", title, **kwargs)

    def state_change(self, title: str, before: dict, after: dict) -> None:
        changed = diff_states(self.redactor.redact(before), self.redactor.redact(after))
        self.emit("STATE CHANGE", title, data=changed if changed else "<no field changed>")

    def transition(self, title: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        """Every tracked field as `before -> after`, including the ones
        that did not move.

        Unlike state_change (which prints only the diff), this is for the
        named transition table: case stage, form version, approval status,
        HRIS record status, external reference and employee count. Showing
        the unchanged ones is the point -- "the employee count did not
        move" is the evidence that no duplicate was created.
        """
        before_r = self.redactor.redact(before)
        after_r = self.redactor.redact(after)
        rows: dict[str, Any] = {}
        for key in sorted(set(before_r) | set(after_r)):
            old = before_r.get(key, "<absent>")
            new = after_r.get(key, "<absent>")
            if isinstance(old, (dict, list)):
                old = json.dumps(old, default=str)
            if isinstance(new, (dict, list)):
                new = json.dumps(new, default=str)
            rows[key] = f"{old}  ->  {new}" if old != new else f"{old}   (unchanged)"
        self.emit("STATE CHANGE", title, fields=rows)

    def external_op(self, title: str, gateway: Any, fields: Optional[dict] = None, **kwargs: Any) -> None:
        detail = external_mode_detail(gateway)
        self.ledger.record_external_op(detail["mode"], detail["gateway_class"])
        merged = {**detail, **(fields or {})}
        self.emit("EXTERNAL OP", f"[{detail['mode']}] {title}", fields=merged, **kwargs)

    def external_op_not_attempted(self, title: str, gateway: Any, why: str) -> None:
        """Record that no call was made. Counted separately from a real
        call so the summary cannot overstate how much ran."""
        detail = external_mode_detail(gateway)
        self.ledger.record_external_op("NOT_ATTEMPTED", detail["gateway_class"])
        self.emit(
            "EXTERNAL OP",
            f"[NOT ATTEMPTED] {title}",
            fields={
                "mode if it had run": detail["mode"],
                "gateway_class": detail["gateway_class"],
                "why no call was made": why,
            },
        )

    def external_operations(
        self,
        title: str,
        gateway: Any,
        operations: list[dict[str, Any]],
        employees_before: Optional[int] = None,
        employees_after: Optional[int] = None,
    ) -> None:
        """One labelled line per target-system call, each carrying its own
        mode.

        `operations` is built by the caller from the HrisRecords actually
        involved -- which system, write() vs update(), and the resulting
        status and external reference. The mode is read off the gateway
        class every time rather than carried over from a previous
        section, so no call can inherit a stale label.

        An operation may set `"_called": False` to say the gateway was
        never invoked for it. Those are labelled NOT ATTEMPTED rather
        than given the gateway's mode, so a record that was skipped can
        never read as one that ran.
        """
        detail = external_mode_detail(gateway)
        for op in operations:
            self.ledger.record_external_op(
                detail["mode"] if op.get("_called", True) else "NOT_ATTEMPTED",
                detail["gateway_class"],
            )
        if not self.enabled:
            return
        self._write(f"  [EXTERNAL OP] {title}")
        self._write(f"      gateway       : {detail['gateway_class']} ({detail['gateway_module']})")
        if detail["mode"] == "SIMULATED":
            self._write(
                "      meaning       : SIMULATED -- an in-memory stand-in. No record exists in "
                "any Zoho tenant, and every external_ref below was minted locally."
            )
        elif detail["mode"] == "MOCKED":
            self._write("      meaning       : MOCKED -- a recorded/scripted double, not a real tenant.")
        else:
            self._write("      meaning       : LIVE -- calls a real external system.")
        for op in operations:
            label = detail["mode"] if op.get("_called", True) else "NOT ATTEMPTED"
            self._write(f"      - [{label}] {op.get('system')} {op.get('operation')}")
            for name, value in op.items():
                if name in ("system", "operation") or name.startswith("_"):
                    continue
                shown = self.redactor.redact(value, key=str(name))
                if isinstance(shown, (dict, list)):
                    shown = json.dumps(shown, default=str)
                self._write(f"          {str(name).ljust(26)} : {shown}")
        if employees_before is not None or employees_after is not None:
            self._write(
                f"      distinct employees in the target system: "
                f"{employees_before} -> {employees_after}"
            )

    def policy_check(self, verdict: dict[str, Any], flag_on: tuple[str, ...] = ("MISMATCH",)) -> None:
        """Report the approver-vs-policy comparison, and flag a mismatch
        into the run summary rather than quietly rendering it."""
        if verdict.get("verdict") in flag_on:
            self.ledger.record_policy_flag(verdict)
        self.emit(
            "POLICY CHECK",
            f"approver vs policy: {verdict.get('verdict')}",
            fields=verdict,
        )

    def audit(self, entries: Iterable[AuditEntry], title: str = "entries appended") -> None:
        summary = audit_summary(entries, self.redactor)
        self.emit(
            "AUDIT EVENT",
            f"{title} ({len(summary)})",
            data=summary if summary else "<none>",
        )

    # -- approvals ---------------------------------------------------------

    def approval_required(
        self,
        approval: Any,
        why: str,
        blocks: str,
        approver_roles: Iterable[str],
        separation_of_duties: Optional[bool] = None,
        awaiting: Any = None,
    ) -> None:
        """Everything a reader needs to judge one approval gate: why it
        exists, who may decide it, what is blocked meanwhile, and what is
        sitting in front of the approver.

        `approver_roles` and `separation_of_duties` come from the loaded
        policy file, not from this module.
        """
        self.ledger.record_approval(
            getattr(approval, "checkpoint", None),
            getattr(approval, "status", None),
            getattr(approval, "approval_id", None),
        )
        fields: dict[str, Any] = {
            "approval_id": getattr(approval, "approval_id", None),
            "checkpoint": getattr(approval, "checkpoint", None),
            "subject_ref (subject)": getattr(approval, "subject_ref", None),
            "status BEFORE decision": getattr(approval, "status", None),
            "requested_by (actor)": getattr(approval, "requested_by", None) or NOT_CAPTURED,
            "requested_to (intended approver)": getattr(approval, "requested_to", None),
            "allowed_roles (policy)": sorted(approver_roles),
            "created_at": getattr(approval, "created_at", None),
            "decided_by": getattr(approval, "decided_by", None) or "<undecided>",
            "decided_at": getattr(approval, "decided_at", None) or "<undecided>",
            "decision_note": getattr(approval, "decision_note", None) or "<undecided>",
            "why approval is needed": why,
            "blocked while PENDING": blocks,
        }
        if separation_of_duties is not None:
            fields["separation_of_duties"] = separation_of_duties
        if getattr(approval, "reason", None):
            fields["reason"] = approval.reason
        self.emit("APPROVAL REQUIRED", "human decision gate", fields=fields)
        if awaiting is not None:
            self.emit("APPROVAL REQUIRED", "data awaiting this decision", data=awaiting)

    def approval_decision(
        self,
        approval: Any,
        effect: str,
        next_step: str,
        status_before: Any = None,
        blocked_if_rejected: Optional[str] = None,
        resulting_state: Optional[dict[str, Any]] = None,
    ) -> None:
        """The other half of the gate: what was decided, by whom, when, and
        what the decision left behind.

        `status_before` is read off the approval before the decision call,
        so the transition is observed rather than assumed.
        """
        status = getattr(approval, "status", None)
        self.ledger.record_approval(
            getattr(approval, "checkpoint", None), status, getattr(approval, "approval_id", None)
        )
        fields: dict[str, Any] = {
            "approval_id": getattr(approval, "approval_id", None),
            "checkpoint": getattr(approval, "checkpoint", None),
            "subject_ref (subject)": getattr(approval, "subject_ref", None),
            "requested_by (actor)": getattr(approval, "requested_by", None) or NOT_CAPTURED,
            "requested_to (intended approver)": getattr(approval, "requested_to", None),
            "status transition": f"{getattr(status_before, 'value', status_before)} -> "
            f"{getattr(status, 'value', status)}",
            "decision": status,
            "decided_by (actor)": getattr(approval, "decided_by", None),
            "decided_at": getattr(approval, "decided_at", None),
            "decision_note": getattr(approval, "decision_note", None) or "<none given>",
            "effect": effect,
            "next step": next_step,
        }
        if blocked_if_rejected is not None:
            fields["blocked while REJECTED"] = blocked_if_rejected
        self.emit("APPROVAL DECISION", "decided", fields=fields)
        if resulting_state:
            self.emit("APPROVAL DECISION", "resulting state", fields=resulting_state)

    # -- assertions --------------------------------------------------------

    def assertion(
        self,
        ok: bool,
        requirement: str,
        label: str,
        expected: Any = None,
        actual: Any = None,
        reason: str = "",
    ) -> None:
        # Recorded by check() in the script, not here, so the tally is the
        # same whether or not this section prints.
        self.emit(
            "ASSERTION RESULT",
            f"{'PASS' if ok else 'FAIL'}  {requirement}  {label}",
            fields={
                "expected": expected,
                "actual": actual,
                "reason": reason or ("expected matches actual" if ok else "expected != actual"),
            },
        )


def render_summary(
    ledger: RunLedger,
    unverified_paths: list[str],
    write_line: Any = print,
) -> None:
    """Print the end-of-run summary.

    Every number comes off the ledger, which was filled as the run went.
    `unverified_paths` is supplied by the caller and is the one place the
    report states what it did NOT establish.
    """
    write_line("=" * 78)
    write_line("RUN SUMMARY")
    write_line("=" * 78)

    write_line(f"Scenarios run      : {len(ledger.scenarios)}")
    if ledger.scenarios:
        write_line(f"  {', '.join(ledger.scenarios)}")

    total = len(ledger.assertions)
    write_line(f"Assertions         : {total} total, {ledger.passed} passed, {ledger.failed} failed")
    by_requirement: dict[str, Counter] = {}
    for ok, requirement, _ in ledger.assertions:
        by_requirement.setdefault(requirement, Counter())["pass" if ok else "fail"] += 1
    for requirement in sorted(by_requirement):
        counts = by_requirement[requirement]
        write_line(
            f"  {requirement:<4} {counts['pass']} passed"
            + (f", {counts['fail']} FAILED" if counts["fail"] else "")
        )

    write_line("External operation modes used:")
    if ledger.external_ops:
        for mode, count in sorted(ledger.external_ops.items()):
            write_line(f"  {mode:<14} {count}")
        write_line(f"  gateways       : {', '.join(sorted(ledger.external_gateways))}")
        if set(ledger.external_ops) <= {"SIMULATED", "NOT_ATTEMPTED"}:
            write_line(
                "  NO LIVE OR MOCKED EXTERNAL CALL WAS MADE. Every Zoho People / Zoho Payroll "
                "operation above ran against an in-memory stand-in."
            )
    else:
        write_line("  <none recorded>")

    write_line("Approval outcomes (final status per approval):")
    outcomes = ledger.approval_outcomes()
    if outcomes:
        for checkpoint in sorted(outcomes):
            rendered = ", ".join(f"{s}={n}" for s, n in sorted(outcomes[checkpoint].items()))
            write_line(f"  {checkpoint:<18} {rendered}")
    else:
        write_line("  <none recorded>")

    write_line("Approval policy flags:")
    if ledger.policy_flags:
        for flag in ledger.policy_flags:
            write_line(f"  [{flag['verdict']}] {flag['checkpoint']} {flag['approval_id']}")
            write_line(f"      {flag['detail']}")
    else:
        write_line("  none -- every approval's intended approver matched the policy roles")

    write_line("Incomplete or unverified integration paths:")
    for path in unverified_paths:
        write_line(f"  - {path}")
