"""EC-42: an approved internal transfer of an existing employee. UC-04's part is equipment and access only.

The employee keeps one identity and one parent case (UC-04 never creates a case, BR-24). The plan compares what the person holds with the
NEW role's client configuration and proposes: keep, request, remove, replace. Every proposal needs a person's approval and IT confirms every
grant, removal and handover; nothing here is assumed done until IT says so. Joining documents are not generated here: UC-03 validates any
document a client requires, UC-02 owns task changes, UC-05 owns orientation.
"""

from sub_agent.contracts import Blocker, Proposal, Uc04Input
from sub_agent.rules import blocker
from sub_agent.states import Owner, Severity


def plan_transfer(inp: Uc04Input) -> tuple[dict, list[Proposal], list[Blocker]]:
    t = inp.transfer
    cfg = inp.config
    proposals: list[Proposal] = []
    blockers: list[Blocker] = []
    held_a = {i.item_id: i for i in t.held_access}
    held_e = {i.item_id: i for i in t.held_equipment}
    req_a = {i.item_id for i in inp.requested_access}
    req_e = {i.item_id for i in inp.requested_equipment}
    cross_client = bool(t.previous_client_id and t.previous_client_id != inp.client_id)
    computable = cfg.role_access_matrix.is_usable and cfg.role_equipment_matrix.is_usable and not (
        cfg.client_id != inp.client_id or cfg.role_id != inp.role_id)
    new_a = set(cfg.role_access_matrix.value or []) if cfg.role_access_matrix.is_usable else set()
    new_e = set(cfg.role_equipment_matrix.value or []) if cfg.role_equipment_matrix.is_usable else set()

    plan = {
        "transfer_ref": t.transfer_ref, "employee_ref": inp.employee_ref, "previous_role_id": t.previous_role_id, "role_id": inp.role_id,
        "cross_client": cross_client, "complete": computable, "remove_old_access": t.remove_old_access,
        "joining_documents": ("A client rule requires transfer documents; UC-03 validates them." if t.client_requires_joining_documents
                              else "Not required by UC-04. No joining documents are generated for a transfer."),
        "access": {"request": sorted(req_a - set(held_a)), "already_held_and_requested": sorted(req_a & set(held_a))},
        "equipment": {"request": sorted(req_e - set(held_e))},
    }
    if not computable:
        blockers.append(blocker("TRANSFER_CONFIG_MISSING", Severity.REVIEW,
                                "The new role's access or equipment configuration is missing or does not match, so held items cannot be compared.",
                                Owner.HR_OFFICER, "Ask HR for the new role's configuration. Nothing is removed or kept until it is known.", "EC-42"))
        return plan, proposals, blockers

    retain_a = sorted(set(held_a) & new_a)
    remove_a = sorted(set(held_a) - new_a)
    confirmed_removed = sorted(set(remove_a) & set(t.it_confirmed_removed))
    pending_removal = [i for i in remove_a if i not in confirmed_removed]
    plan["access"].update({"retain": retain_a, "remove": remove_a, "removal_confirmed_by_it": confirmed_removed,
                           "removal_pending_it_confirmation": pending_removal if t.remove_old_access else [],
                           "old_access_left_for_review": [] if t.remove_old_access else remove_a})
    if t.remove_old_access:
        for item in pending_removal:
            proposals.append(Proposal(proposal_type="REQUEST_ACCESS_REMOVAL", description=(
                "Ask IT to remove access that the new role does not include. IT executes and confirms; it is not removed until then."),
                requires_approval_by=Owner.HR_OFFICER, payload={"transfer_ref": t.transfer_ref, "item_id": item, "removal_confirmed": False,
                                                                 "status": "DRAFT_NOT_SENT"}))
    elif remove_a:
        blockers.append(blocker("TRANSFER_OLD_ACCESS_REVIEW", Severity.REVIEW,
                                "The approved transfer plan keeps old-role access that the new role does not include.", Owner.HR_OFFICER,
                                "HR and IT review whether that access stays.", "EC-42"))

    retain_e = sorted(set(held_e) & new_e)
    change_e = sorted(set(held_e) - new_e)
    suitable = sorted(set(retain_e) & set(t.it_confirmed_suitable))
    plan["equipment"].update({"retain_if_it_confirms": retain_e, "confirmed_suitable_by_it": suitable, "change": change_e})
    for item in [i for i in retain_e if i not in suitable]:
        proposals.append(Proposal(proposal_type="CONFIRM_EQUIPMENT_SUITABLE", description=(
            "Ask IT whether the equipment the person already holds suits the new role. IT decides; UC-04 reuses it only after IT confirms."),
            requires_approval_by=Owner.HR_OFFICER, payload={"transfer_ref": t.transfer_ref, "item_id": item,
                                                             "asset_ref": held_e[item].asset_ref, "suitable_confirmed": False,
                                                             "status": "DRAFT_NOT_SENT"}))
    for item in change_e:
        proposals.append(Proposal(proposal_type="REQUEST_EQUIPMENT_CHANGE", description=(
            "The new role's list does not include this equipment. Ask IT to decide a change or replacement. IT allocates."),
            requires_approval_by=Owner.HR_OFFICER, payload={"transfer_ref": t.transfer_ref, "item_id": item, "asset_ref": held_e[item].asset_ref,
                                                             "status": "DRAFT_NOT_SENT"}))
    if cross_client:
        blockers.append(blocker("TRANSFER_CROSS_CLIENT_REVIEW", Severity.REVIEW,
                                "The transfer moves the employee to another client. No approved rule covers this case.", Owner.HR_OFFICER,
                                "HR decides whether and how access and equipment carry over.", "EC-42"))
    return plan, proposals, blockers
