"""Storage manager for candidate email attachments.

Handles structured directory storage, SHA-256 fingerprinting,
and password-protected PDF detection.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("uc03.ingestion.storage")


@dataclass
class StoredAttachment:
    """Metadata for an encrypted/stored document attachment."""
    document_ref: str
    sha256_hash: str
    size_bytes: int
    is_password_protected: bool = False
    stored_at: datetime = None

    def __post_init__(self):
        if self.stored_at is None:
            self.stored_at = datetime.now(timezone.utc)


class AttachmentStorageManager:
    """Manages secure local/cloud storage of candidate documents."""

    def __init__(self, base_storage_dir: Path | str | None = None) -> None:
        if base_storage_dir:
            self.base_dir = Path(base_storage_dir)
        else:
            self.base_dir = Path(tempfile.gettempdir()) / "dact_onboarding_storage"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def is_pdf_password_protected(self, file_bytes: bytes) -> bool:
        """Check if PDF is encrypted or password-protected."""
        if not file_bytes.startswith(b"%PDF"):
            return False
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            is_encrypted = doc.is_encrypted
            doc.close()
            return bool(is_encrypted)
        except Exception:
            # Fallback byte pattern inspection for /Encrypt dictionary in PDF catalog
            return b"/Encrypt" in file_bytes

    def store(
        self,
        case_id: str,
        document_type: str,
        original_filename: str,
        file_bytes: bytes,
    ) -> StoredAttachment:
        """Save attachment bytes with SHA-256 checksum in partitioned case directory."""
        clean_case = re.sub(r"[^a-zA-Z0-9_-]", "_", case_id)
        clean_type = re.sub(r"[^a-zA-Z0-9_-]", "_", document_type)
        clean_filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", original_filename)

        case_folder = self.base_dir / "onboarding" / clean_case / clean_type
        case_folder.mkdir(parents=True, exist_ok=True)

        timestamp_prefix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target_path = case_folder / f"{timestamp_prefix}_{clean_filename}"

        target_path.write_bytes(file_bytes)
        sha256_hash = hashlib.sha256(file_bytes).hexdigest()
        is_encrypted = self.is_pdf_password_protected(file_bytes)

        log.info(
            "Stored document %s in %s (SHA256: %s, Encrypted: %s)",
            original_filename,
            target_path,
            sha256_hash[:8],
            is_encrypted,
        )

        return StoredAttachment(
            document_ref=str(target_path.resolve()),
            sha256_hash=sha256_hash,
            size_bytes=len(file_bytes),
            is_password_protected=is_encrypted,
        )
