"""Enumerations for UC-04. The sources do not define state names; these are a local proposal."""

from enum import StrEnum


class InvocationMode(StrEnum):
    DIRECT = "DIRECT"
    PARENT_ORCHESTRATED = "PARENT_ORCHESTRATED"


class ItemKind(StrEnum):
    EQUIPMENT = "EQUIPMENT"
    ACCESS = "ACCESS"


class WorkMode(StrEnum):
    OFFICE = "OFFICE"
    REMOTE = "REMOTE"
    OVERSEAS = "OVERSEAS"


class ConfigState(StrEnum):
    MISSING = "MISSING"  # nobody has supplied this configuration
    VERIFIED_EMPTY = "VERIFIED_EMPTY"  # supplied and verified to be empty (nothing allowed)
    VERIFIED = "VERIFIED"


class ItemState(StrEnum):
    DRAFT_PREPARED = "DRAFT_PREPARED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    EXCEPTION_PENDING = "EXCEPTION_PENDING"
    APPROVAL_RECEIVED = "APPROVAL_RECEIVED"
    EXCEPTION_RECEIVED = "EXCEPTION_RECEIVED"
    ELIGIBLE_FOR_SIMULATED_SUBMISSION = "ELIGIBLE_FOR_SIMULATED_SUBMISSION"
    MOCK_TICKET_CREATED = "MOCK_TICKET_CREATED"
    IT_IN_PROGRESS = "IT_IN_PROGRESS"
    REJECTED_CORRECTION_REQUIRED = "REJECTED_CORRECTION_REQUIRED"
    PROVISIONING_CONFIRMED = "PROVISIONING_CONFIRMED"
    EQUIPMENT_DELIVERED = "EQUIPMENT_DELIVERED"
    EQUIPMENT_COLLECTED = "EQUIPMENT_COLLECTED"
    CLIENT_SITE_CLEARANCE_CONFIRMED = "CLIENT_SITE_CLEARANCE_CONFIRMED"
    ITEM_COMPLETED = "ITEM_COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class CaseStatus(StrEnum):
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    IN_PROGRESS = "IN_PROGRESS"
    AT_RISK = "AT_RISK"
    READY = "READY"
    CANCELLED = "CANCELLED"


class ApprovalClass(StrEnum):
    EQUIPMENT = "EQUIPMENT"
    ACCESS = "ACCESS"
    EXCEPTION = "EXCEPTION"


class ApprovalType(StrEnum):
    STANDARD = "STANDARD"
    EXCEPTION = "EXCEPTION"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    RECEIVED = "RECEIVED"
    REJECTED = "REJECTED"


class TicketStatus(StrEnum):
    """Synthetic vocabulary. The real IT service-desk status list is an open question."""

    CREATED = "CREATED"
    IN_PROGRESS = "IN_PROGRESS"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"


class ProvisioningStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    CONFIRMED_ACTIVE = "CONFIRMED_ACTIVE"
    FAILED = "FAILED"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAULTY = "FAULTY"
    WRONG_MODEL = "WRONG_MODEL"


class CollectionStatus(StrEnum):
    NOT_COLLECTED = "NOT_COLLECTED"
    COLLECTED = "COLLECTED"
    MISSED_SLOT = "MISSED_SLOT"


class ClearanceStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    DENIED = "DENIED"


class HandoverMode(StrEnum):
    COLLECT = "COLLECT"
    DELIVER = "DELIVER"
    READY_AT_DESK = "READY_AT_DESK"


class EvidenceType(StrEnum):
    PROVISIONING = "PROVISIONING"
    DELIVERY = "DELIVERY"
    COLLECTION = "COLLECTION"
    CLIENT_SITE_CLEARANCE = "CLIENT_SITE_CLEARANCE"


class LeadKey(StrEnum):
    EQUIPMENT = "EQUIPMENT"
    ACCESS = "ACCESS"
    CLIENT_SITE_CLEARANCE = "CLIENT_SITE_CLEARANCE"


class Severity(StrEnum):
    BLOCKING = "BLOCKING"  # case becomes BLOCKED
    REVIEW = "REVIEW"  # case becomes NEEDS_HUMAN_REVIEW


class ParentCaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ON_HOLD = "ON_HOLD"
    WITHDRAWN = "WITHDRAWN"
    CANCELLED = "CANCELLED"


class Autonomy(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class Owner:
    """Owner roles named in the onboarding framework (HR officer, hiring manager, IT service desk, account manager,
    HR operations lead) plus two local markers."""

    HR_OFFICER = "HR_OFFICER"
    HR_OPERATIONS_LEAD = "HR_OPERATIONS_LEAD"
    HIRING_MANAGER = "HIRING_MANAGER"
    IT_SERVICE_DESK = "IT_SERVICE_DESK"
    ACCOUNT_MANAGER = "ACCOUNT_MANAGER"
    CLIENT_HR_ADMIN = "CLIENT_HR_ADMIN"
    UC04_SERVICE = "UC04_SERVICE"  # the simulated next step performed by this capability itself
    OWNER_CONFIG_MISSING = "OWNER_CONFIG_MISSING"  # BR-19: owner could not be resolved from configuration
