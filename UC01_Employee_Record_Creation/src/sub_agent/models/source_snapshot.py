from pydantic import BaseModel, model_validator

ALLOWED_SOURCE_SNAPSHOT_KEYS = frozenset(
    {
        "candidate_name",
        "designation",
        "joining_date",
        "work_location_type",
        "salary_and_terms",
    }
)


class SourceSnapshot(BaseModel):
    source: str
    values: dict[str, str]

    @model_validator(mode="after")
    def _check_known_keys(self) -> "SourceSnapshot":
        unknown = set(self.values) - ALLOWED_SOURCE_SNAPSHOT_KEYS
        if unknown:
            raise ValueError(f"unknown SourceSnapshot value key(s): {sorted(unknown)}")
        return self

    def __repr__(self) -> str:
        # values may carry salary_and_terms; never render it (or anything
        # else in the dict) via repr()/logs.
        return f"SourceSnapshot(source={self.source!r}, values=<hidden>)"
