"""Environment-backed settings. No settings library beyond python-dotenv +
pydantic: just enough to read the handful of env vars UC-01 needs.

.env resolution (changed when Supabase persistence was added)
-------------------------------------------------------------
Previously this module called a bare `load_dotenv()`. That resolves via
`find_dotenv()`, which walks up from this FILE and stops at the first
.env it finds - always UC-01's own - so the repository-root .env was
never read. Worse, under a debugger or REPL `find_dotenv` switches to
walking up from the CURRENT WORKING DIRECTORY instead, so which file won
depended on how the process was started.

Both paths are now explicit and cwd-independent:

  1. UC-01's own .env    - use-case-local operational settings
  2. the repository-root .env, loaded with override=True - the SHARED
     configuration source (Supabase, LLM routing, HR_AGENT_*). Root wins on
     any key defined in both, so shared infrastructure config has one
     home.

Real process environment variables set before start always win over
both, because python-dotenv only fills in names that are absent (and
step 2's override only re-applies values from the root FILE).
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

_TRUE_VALUES = {"true", "1", "yes", "on"}

# .../UC01_Employee_Record_Creation/src/sub_agent/api/settings.py
_UC01_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _UC01_ROOT.parent


# Names already present in the real process environment when this module
# is first imported. Anything injected by the deployment (CI secrets, a
# container's -e flags, Key Vault) is in here and must not be overwritten
# by a file on disk.
_PRESET_ENV = frozenset(os.environ)


def _load_env_files() -> None:
    """Load UC-01's .env, then let the repo-root .env override it.

    Precedence, highest first:
      1. real process environment  (deployment-injected; never overwritten)
      2. repository-root .env      (the shared configuration source)
      3. UC-01's own .env          (use-case-local defaults)

    The root file beats UC-01's file - that is the point of making it the
    shared source - but neither beats a variable the deployment actually
    set. Overwriting those would mean a stale checked-out .env silently
    redirecting a production process at the wrong project.
    """
    load_dotenv(_UC01_ROOT / ".env")
    root_env = _REPO_ROOT / ".env"
    if root_env.is_file():
        for key, value in dotenv_values(root_env).items():
            if value is not None and key not in _PRESET_ENV:
                os.environ[key] = value


_load_env_files()


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_env: str = "dev"
    dedup_hmac_key: str = ""
    hris_writes_enabled: bool = True
    system_api_key: str = ""
    # Attach the advisory model. Off by default: UC-01 decides nothing
    # with a model, and every flow must work without one.
    llm_enabled: bool = False
    # Which advisory model, when llm_enabled: "offline" (default) is the
    # deterministic FakeLlm; anything else is the central
    # hr_onboarding_common gateway (same LLM_MODE rule as UC-05/UC-02).
    llm_mode: str = "offline"
    # INTERIM Supabase writes (approved by the UC-01 owner 2026-11-04 while
    # DBA decisions B2/B5/B6/B7 are pending): cases, audit, approvals,
    # employee numbers and a logical case-folder reference go to the
    # existing shared tables. Explicit opt-in; refused outside APP_ENV=dev.
    supabase_interim_writes: bool = False
    # Supabase Storage bucket for received onboarding documents. Empty = the
    # source file is not stored (only its SHA-256 and size are recorded).
    document_bucket: str = ""

    # -- Supabase persistence ----------------------------------------
    # Empty supabase_url selects the in-memory adapters. That is allowed
    # only in dev; build_container refuses it outside dev so production
    # can never silently run on memory.
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")
    supabase_schema: str = "public"
    supabase_timeout_s: float = 15.0
    # AES-256-GCM key (base64, 32 bytes) for onboarding_forms
    # .salary_and_terms_encrypted. Required whenever Supabase is on.
    field_encryption_key: SecretStr = SecretStr("")
    # Key versioning (see sub_agent.crypto): the current key's id, and
    # retired "id:base64" keys kept only to decrypt existing rows.
    field_encryption_key_id: str = ""
    field_encryption_previous_keys: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def _development_keys_stay_in_dev(self) -> "Settings":
        from ..crypto import DEV_KEY_ID_PREFIX, check_key_id, parse_previous_keys

        key_ids = list(parse_previous_keys(self.field_encryption_previous_keys.get_secret_value()))
        if self.field_encryption_key_id.strip():
            key_ids.append(check_key_id(self.field_encryption_key_id.strip()))
        if self.app_env != "dev" and any(k.startswith(DEV_KEY_ID_PREFIX) for k in key_ids):
            raise ValueError(
                f"a development encryption key (id '{DEV_KEY_ID_PREFIX}...') is configured but APP_ENV is "
                f"'{self.app_env}': outside dev only the managed key from the approved secret store may be used"
            )
        return self

    @property
    def persistence_enabled(self) -> bool:
        """True when durable (Supabase) persistence is configured."""
        return bool(self.supabase_url.strip())

    @model_validator(mode="after")
    def _supabase_config_is_complete(self) -> "Settings":
        if not self.persistence_enabled:
            return self
        missing: list[str] = []
        if not self.supabase_service_role_key.get_secret_value().strip():
            missing.append("SUPABASE_SERVICE_ROLE_KEY")
        if not self.field_encryption_key.get_secret_value().strip():
            missing.append("UC01_FIELD_ENCRYPTION_KEY")
        if missing:
            raise ValueError(
                "SUPABASE_URL is set, so these must be set too: "
                + ", ".join(missing)
                + ". Set them in the repository-root .env (the shared "
                "configuration source) - see .env.example."
            )
        if not self.supabase_url.strip().startswith("https://"):
            raise ValueError("SUPABASE_URL must start with https://")
        return self

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "Settings":
        source = env if env is not None else os.environ
        return cls(
            app_env=source.get("APP_ENV", "dev"),
            dedup_hmac_key=source.get("DEDUP_HMAC_KEY", ""),
            hris_writes_enabled=source.get("UC01_HRIS_WRITES_ENABLED", "true").strip().lower()
            in _TRUE_VALUES,
            system_api_key=source.get("UC01_SYSTEM_API_KEY", ""),
            llm_enabled=source.get("UC01_LLM_ENABLED", "false").strip().lower() in _TRUE_VALUES,
            llm_mode=source.get("LLM_MODE", "offline").strip().lower() or "offline",
            supabase_interim_writes=source.get("UC01_SUPABASE_INTERIM_WRITES", "false").strip().lower()
            in _TRUE_VALUES,
            document_bucket=source.get("UC01_DOCUMENT_BUCKET", "").strip(),
            supabase_url=source.get("SUPABASE_URL", "").strip(),
            supabase_service_role_key=SecretStr(source.get("SUPABASE_SERVICE_ROLE_KEY", "")),
            supabase_schema=source.get("SUPABASE_SCHEMA", "public").strip() or "public",
            supabase_timeout_s=float(source.get("SUPABASE_TIMEOUT_S", "15") or 15),
            field_encryption_key=SecretStr(source.get("UC01_FIELD_ENCRYPTION_KEY", "")),
            field_encryption_key_id=source.get("UC01_FIELD_ENCRYPTION_KEY_ID", "").strip(),
            field_encryption_previous_keys=SecretStr(source.get("UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS", "")),
        )
