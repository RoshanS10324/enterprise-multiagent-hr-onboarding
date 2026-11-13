from __future__ import annotations


class AccessDeniedError(Exception):
    def __init__(self, case_id: str, actor_id: str) -> None:
        self.case_id = case_id
        self.actor_id = actor_id
        super().__init__(
            f"Actor '{actor_id}' is not authorised to access case '{case_id}'"
        )


def assert_case_access(
    requesting_actor_id: str,
    case_actor_ids: set[str],
    case_id: str,
) -> None:
    """Raise AccessDeniedError if the requesting actor is not in the authorised set."""
    if requesting_actor_id not in case_actor_ids:
        raise AccessDeniedError(case_id=case_id, actor_id=requesting_actor_id)
