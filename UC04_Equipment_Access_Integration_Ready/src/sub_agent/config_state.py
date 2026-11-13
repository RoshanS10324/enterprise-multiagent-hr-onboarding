"""Configuration tri-state: MISSING is never silently turned into an empty allowlist or a default value."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, model_validator

from sub_agent.states import ConfigState

T = TypeVar("T")


class ConfigBlock(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    state: ConfigState = ConfigState.MISSING
    value: T | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.state == ConfigState.MISSING and self.value is not None:
            raise ValueError("a MISSING config block must not carry a value")
        if self.state == ConfigState.VERIFIED and self.value is None:
            raise ValueError("a VERIFIED config block needs a value")
        if self.state == ConfigState.VERIFIED_EMPTY and self.value not in (None, [], {}):
            raise ValueError("a VERIFIED_EMPTY config block must be empty")
        return self

    @property
    def is_missing(self) -> bool:
        return self.state == ConfigState.MISSING

    @property
    def is_empty(self) -> bool:
        return self.state == ConfigState.VERIFIED_EMPTY

    @property
    def is_usable(self) -> bool:
        return self.state == ConfigState.VERIFIED

    @classmethod
    def verified(cls, value):
        return cls(state=ConfigState.VERIFIED, value=value)

    @classmethod
    def verified_empty(cls):
        return cls(state=ConfigState.VERIFIED_EMPTY)
