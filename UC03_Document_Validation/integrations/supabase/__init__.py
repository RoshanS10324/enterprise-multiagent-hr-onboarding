"""UC-03 persistence on Supabase PostgreSQL (through the shared Common client). Selected by
UC03_STORE_MODE. See README.md.
"""

from .runtime import (
    MODES,
    StoreConfigurationError,
    UC03Stores,
    build_stores,
    configure_api,
    store_mode,
)
from .tables import DuplicateKey, SharedSupabaseTables, StorageError, TableClient

__all__ = [
    "MODES",
    "DuplicateKey",
    "SharedSupabaseTables",
    "StorageError",
    "StoreConfigurationError",
    "TableClient",
    "UC03Stores",
    "build_stores",
    "configure_api",
    "store_mode",
]
