"""DEV-ONLY identity.

Placeholder for Microsoft Entra ID tokens. Header identity is accepted
only when APP_ENV=dev. See docs/known-gaps.md.
"""

import hmac
from typing import Optional

from fastapi import Header, HTTPException
from pydantic import ValidationError

from ..models import Role, Viewer
from .settings import Settings


def get_viewer(
    x_user_id: Optional[str] = Header(default=None),
    x_user_role: Optional[str] = Header(default=None),
    x_client_ids: Optional[str] = Header(default=None),
) -> Viewer:
    if not x_user_id or not x_user_role or not x_client_ids:
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        role = Role(x_user_role)
    except ValueError:
        raise HTTPException(status_code=401, detail="unauthorized") from None

    client_ids = frozenset(c.strip() for c in x_client_ids.split(",") if c.strip())

    try:
        return Viewer(user_id=x_user_id, role=role, client_ids=client_ids)
    except ValidationError:
        raise HTTPException(status_code=401, detail="unauthorized") from None


def make_system_key_dependency(settings: Settings):
    def _require_system_key(x_system_key: Optional[str] = Header(default=None)) -> None:
        if not x_system_key or not hmac.compare_digest(x_system_key, settings.system_api_key):
            raise HTTPException(status_code=401, detail="unauthorized")

    return _require_system_key
