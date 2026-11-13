"""Start the LOCAL development API for UC-04 with Swagger UI. Local use only: it binds to 127.0.0.1 and has no authentication.

    python run_api.py

Swagger UI: http://127.0.0.1:8000/docs    OpenAPI schema: http://127.0.0.1:8000/openapi.json
Needs the API extras (fastapi, uvicorn); see docs/LOCAL_API.md.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("sub_agent.api.app:app", host="127.0.0.1", port=8000, log_level="info")
