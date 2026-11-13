"""Supabase-backed adapters for UC-01.

Import surface kept explicit so the container wires named adapters
rather than reaching into modules.
"""

from .client import (
    CheckViolation,
    ForeignKeyViolation,
    NotNullViolation,
    PostgrestClient,
    SupabaseError,
    UniqueViolation,
)
from .repositories import (
    INTERIM_STATUS_MAPPING,
    AuditClientUnknown,
    SupabaseAuditLog,
    SupabaseLogicalDocumentStore,
    CaseStatusMappingMissing,
    SupabaseCaseRepository,
    SupabaseApprovalRepository,
    SupabaseEmployeeNumberGenerator,
    SupabaseDiscrepancyRepository,
    SupabaseEventPublisher,
    SupabaseFormRepository,
    SupabaseHolidayCalendar,
    SupabaseHrisRecordRepository,
    SupabaseOnboardingFormSource,
    SupabasePendingStartRepository,
    SupabasePersonDirectory,
    UnsupportedFieldError,
)
from .unsupported import (
    SchemaGapError,
    UnsupportedApprovalRepository,
    UnsupportedAuditLog,
    UnsupportedCaseRepository,
    UnsupportedDocumentStore,
    UnsupportedEmployeeNumberGenerator,
    UnsupportedOfferRepository,
)

__all__ = [
    "INTERIM_STATUS_MAPPING",
    "AuditClientUnknown",
    "SupabaseAuditLog",
    "SupabaseLogicalDocumentStore",
    "CaseStatusMappingMissing",
    "SupabaseCaseRepository",
    "SupabaseApprovalRepository",
    "SupabaseEmployeeNumberGenerator",
    "CheckViolation",
    "ForeignKeyViolation",
    "NotNullViolation",
    "PostgrestClient",
    "SchemaGapError",
    "SupabaseDiscrepancyRepository",
    "SupabaseError",
    "SupabaseEventPublisher",
    "SupabaseFormRepository",
    "SupabaseHolidayCalendar",
    "SupabaseHrisRecordRepository",
    "SupabaseOnboardingFormSource",
    "SupabasePendingStartRepository",
    "SupabasePersonDirectory",
    "UniqueViolation",
    "UnsupportedApprovalRepository",
    "UnsupportedAuditLog",
    "UnsupportedCaseRepository",
    "UnsupportedDocumentStore",
    "UnsupportedEmployeeNumberGenerator",
    "UnsupportedFieldError",
    "UnsupportedOfferRepository",
]
