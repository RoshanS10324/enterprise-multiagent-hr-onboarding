"""Email-driven document ingestion package for UC-03."""

from .classifier import DocumentClassifier, VALID_DOCUMENT_TYPES
from .processor import DocumentSubmissionRecord, EmailIngestionProcessor
from .storage import AttachmentStorageManager, StoredAttachment

__all__ = [
    "AttachmentStorageManager",
    "DocumentClassifier",
    "DocumentSubmissionRecord",
    "EmailIngestionProcessor",
    "StoredAttachment",
    "VALID_DOCUMENT_TYPES",
]
