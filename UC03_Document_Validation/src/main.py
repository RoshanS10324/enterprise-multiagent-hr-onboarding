from fastapi import FastAPI

from src.api.ingestion import router as ingestion_router
from src.api.uc03 import router as uc03_router
from src.sub_agent.api import router as sub_agent_router

app = FastAPI(title="Enterprise HR Onboarding Platform", version="0.1.0")

# Composition root: the ONE place UC-03's storage backend is chosen (UC03_STORE_MODE: memory by
# default, or supabase). The core never imports integrations/; this entry point wires them in
# once, so the API never builds a database client per request. In supabase mode a missing
# setting stops start-up here instead of silently falling back to memory.
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from integrations.supabase import configure_api  # noqa: E402

# The repository-root .env is the shared configuration source (as for UC-01). Values already set in the real
# environment win (override=False), so a deployment, or the test guard in tests/conftest.py, is never overridden.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
configure_api()
app.include_router(uc03_router)
app.include_router(sub_agent_router)
app.include_router(ingestion_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
