"""Test-session isolation from the developer's real LLM configuration.

HR-Onboarding-Common/.env may hold LLM_MODE=online and a real OPENROUTER_API_KEY. The ordinary test
suite must never reach a model: before any test runs, the .env file is pointed away, LLM_MODE is
forced offline and the provider credentials are removed from this process (and so from every UC
worker a test starts), as are the model and reasoning settings, so every test starts from the
defaults. Tests that exercise online configuration set fake values themselves.
The opt-in live check is scripts/openrouter_smoke_test.py, not a test.
"""

from __future__ import annotations

import os
from pathlib import Path

from hr_onboarding_common.config import ENV_FILE_VAR
from hr_onboarding_common.llm.redact import SECRET_ENV_VARS

os.environ[ENV_FILE_VAR] = str(Path(__file__).parent / "no-such-dir" / ".env")  # never loaded
os.environ["LLM_MODE"] = "offline"
os.environ["UC03_STORE_MODE"] = "memory"  # UC03 (in-process) must never write to a live database from a test
for _name in SECRET_ENV_VARS:
    os.environ.pop(_name, None)
for _name in ("LLM_PROVIDER", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "OPENROUTER_REQUIRE_PARAMETERS",
              "LLM_MODEL_FRONTIER", "LLM_MODEL_WORKHORSE", "LLM_MODEL_FAST", "LLM_FALLBACKS_FRONTIER",
              "LLM_FALLBACKS_WORKHORSE", "LLM_FALLBACKS_FAST", "LLM_REASONING_ENABLED", "LLM_REASONING_EFFORT",
              "LLM_REASONING_TOKEN_HEADROOM"):
    os.environ.pop(_name, None)
