from pydantic import BaseModel, ConfigDict, field_validator

from .enums import Role


class Viewer(BaseModel):
    # [Proposed]: HR staff may serve several clients, hence client_ids is a
    # set rather than a single client_id. Confirm with process SME.
    model_config = ConfigDict(frozen=True)

    user_id: str
    role: Role
    client_ids: frozenset[str]

    @field_validator("client_ids")
    @classmethod
    def _non_empty(cls, v: frozenset[str]) -> frozenset[str]:
        if not v:
            raise ValueError("client_ids must not be empty")
        return v
