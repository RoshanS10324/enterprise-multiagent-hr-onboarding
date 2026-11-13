"""Central client configuration for UC-01..UC-05: document, projection onto the existing shared tables, dry-run/seeding, and retrieval."""

from onboarding_task_management.client_configuration.document import (
    ConfigurationDocumentError,
    document_hash,
    load_profiles,
    validate_document,
)

__all__ = ["ConfigurationDocumentError", "document_hash", "load_profiles", "validate_document"]
