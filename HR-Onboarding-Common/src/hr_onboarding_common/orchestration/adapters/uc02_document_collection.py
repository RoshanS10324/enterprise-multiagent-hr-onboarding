"""DEPRECATED import path, kept for backward compatibility.

Document collection is UC03's responsibility (docs/UC02_UC03_RESPONSIBILITY_MIGRATION.md). The stage
adapter is `adapters/uc03_document_collection.call_uc03_document_collection`: it opens UC03's collection
for the current UC02 plan version and sends the (mock) document request. This module only re-exports it
under its old name so existing imports keep working; it holds no logic and never calls UC02.
"""

from __future__ import annotations

from hr_onboarding_common.orchestration.adapters.uc03_document_collection import (STAGE,
                                                                                  call_uc03_document_collection)

call_uc02_document_collection = call_uc03_document_collection

__all__ = ["STAGE", "call_uc02_document_collection"]
