from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/sub-agent", tags=["Sub-Agent"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
