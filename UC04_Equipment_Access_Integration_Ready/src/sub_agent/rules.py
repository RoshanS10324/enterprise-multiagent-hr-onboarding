"""Deterministic start gate and configuration checks (BR-01, BR-02, BR-03). No LLM and no default values."""

from dataclasses import dataclass

from sub_agent.contracts import Blocker, Uc04Input
from sub_agent.states import ItemKind, LeadKey, ParentCaseStatus, Owner, Severity, WorkMode

MAX_LEAD_DAYS = 365


@dataclass(frozen=True)
class ConfigGap:
    code: str
    scope: str  # ALL | ACCESS | EQUIPMENT | CASE
    reason: str

    def blocks(self, kind: ItemKind) -> bool:
        return self.scope in ("ALL", kind.value)


def blocker(code: str, severity: Severity, reason: str, owner: str, next_action: str, rule_id: str,
            request_id: str | None = None) -> Blocker:
    return Blocker(code=code, severity=severity, request_id=request_id, reason=reason, owner=owner,
                   next_action=next_action, rule_id=rule_id)


def start_gate(inp: Uc04Input, parent_status: ParentCaseStatus) -> list[Blocker]:
    """DL-01 / EC-19 / EC-20: nothing is prepared before the offer is accepted and the form approved, or while on hold."""
    found: list[Blocker] = []
    if not inp.offer_accepted:
        found.append(blocker("START_GATE_OFFER_NOT_ACCEPTED", Severity.BLOCKING, "The offer has not been accepted.",
                             Owner.HR_OFFICER, "Hold the case. Do not contact the candidate.", "DL-01"))
    if not inp.onboarding_form_approved:
        found.append(blocker("START_GATE_FORM_NOT_APPROVED", Severity.BLOCKING,
                             "The internal onboarding form is not approved.", Owner.HR_OFFICER,
                             "Hold the case until HR approves the onboarding form.", "DL-01"))
    if parent_status == ParentCaseStatus.ON_HOLD:
        found.append(blocker("CASE_ON_HOLD", Severity.BLOCKING, "The parent onboarding case is on hold.", Owner.HR_OFFICER,
                             "Ask HR whether to resume. Do not contact the employee.", "EC-20"))
    return found


def _lead_gap(inp: Uc04Input, key: LeadKey, scope: str, found: list[ConfigGap]) -> None:
    values = inp.config.lead_times.value or {}
    if key not in values:
        found.append(ConfigGap(f"CONFIG_MISSING:lead_times.{key.value}", scope, f"No lead time configured for {key.value}."))
    elif not 0 <= values[key] <= MAX_LEAD_DAYS:
        found.append(ConfigGap(f"CONFIG_INVALID:lead_times.{key.value}", scope, f"Lead time for {key.value} is out of range."))


def check_config(inp: Uc04Input) -> list[ConfigGap]:
    """Every missing or invalid configuration becomes an explicit gap. Nothing is defaulted (BR-03, BR-20)."""
    cfg, found = inp.config, []
    if cfg.client_id != inp.client_id or cfg.role_id != inp.role_id:
        return [ConfigGap("CONFIG_SCOPE_MISMATCH", "ALL", "The supplied configuration belongs to another client or role.")]

    if cfg.role_access_matrix.is_missing:
        found.append(ConfigGap("CONFIG_MISSING:role_access_matrix", "ACCESS", "No access list exists for this role."))
    if cfg.role_equipment_matrix.is_missing:
        found.append(ConfigGap("CONFIG_MISSING:role_equipment_matrix", "EQUIPMENT", "No equipment list exists for this role."))

    for name, block in (("approver_config", cfg.approver_config), ("work_calendar", cfg.work_calendar),
                        ("evidence_roles", cfg.evidence_roles), ("lead_times", cfg.lead_times)):
        if block.is_missing:
            found.append(ConfigGap(f"CONFIG_MISSING:{name}", "ALL", f"{name} is not configured."))
        elif block.is_empty:
            found.append(ConfigGap(f"CONFIG_EMPTY:{name}", "ALL", f"{name} is verified empty, so nothing can be routed."))

    if cfg.lead_times.is_usable:
        if inp.requested_equipment:
            _lead_gap(inp, LeadKey.EQUIPMENT, "EQUIPMENT", found)
        if inp.requested_access:
            _lead_gap(inp, LeadKey.ACCESS, "ACCESS", found)
        if inp.client_site_required:
            _lead_gap(inp, LeadKey.CLIENT_SITE_CLEARANCE, "CASE", found)

    if inp.requested_equipment and not cfg.handover_rules.is_usable:
        found.append(ConfigGap("CONFIG_MISSING:handover_rules", "EQUIPMENT",
                               "Delivery, collection or ready-at-desk is not configured."))
    if inp.work_mode != WorkMode.OFFICE:
        if not cfg.country_rules.is_usable:
            found.append(ConfigGap("CONFIG_MISSING:country_rules", "EQUIPMENT",
                                   "Remote or overseas rules and courier setup are not configured."))
        elif cfg.country_rules.value.courier_extra_days < 0:
            found.append(ConfigGap("CONFIG_INVALID:country_rules", "EQUIPMENT", "Courier lead time is negative."))
    if inp.client_site_required and not cfg.client_site_rules.is_usable:
        found.append(ConfigGap("CONFIG_MISSING:client_site_rules", "CASE", "Client-site requirements are not configured."))
    return found


def gap_for(gaps: list[ConfigGap], kind: ItemKind) -> ConfigGap | None:
    return next((g for g in gaps if g.blocks(kind)), None)


def config_gap_blockers(gaps: list[ConfigGap]) -> list[Blocker]:
    return [blocker(g.code, Severity.BLOCKING, g.reason, Owner.HR_OFFICER,
                    "Ask HR for the missing configuration. Do not guess.", "BR-03") for g in gaps]
