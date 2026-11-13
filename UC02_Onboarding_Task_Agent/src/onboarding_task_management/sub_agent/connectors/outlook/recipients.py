"""Who a message goes to, decided by APPROVED CONFIGURATION, never by code.

A message carries a role (``recipient_role``) and, for team assignments, a team. A :class:`RecipientDirectory` turns
``(client_id, role, team)`` into the approved mail addresses. UC-02 contains no address, and nothing is guessed: a team or
role with no approved entry makes the connector refuse before any network call.

Approved mapping format (a JSON file owned by the deployment, path in ``UC02_RECIPIENTS_FILE``):

    {
      "<client_id>": {
        "roles": {"hr_officer": ["..."]},                      # role-level mail (the HR review email, notices)
        "teams": {"<team label>": ["..."]},                    # one entry per approved team (assignment emails)
        "allow_role_fallback": false                           # explicit: may a team with no entry use its role's address?
      }
    }

A team message is addressed from ``teams``. ``allow_role_fallback`` is false unless the client says otherwise, and even when
true it is refused while two teams in the same plan share the owner role (``role_shared``): they cannot be told apart, so
dispatch is blocked and the gap reported until each team has its own entry. The older flat format
(``{"<client_id>": {"<role>": [...]}}``) is still read, as role-level addresses only, with no fallback.

Where the approved mapping should live long term (for example the client service policy) is a decision for the
configuration owners; no such field exists in the schema today, so none is assumed here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from onboarding_task_management.sub_agent.connectors.outlook.base import OutlookConnectorError

_ADDRESS = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")
MAX_RECIPIENTS = 10
_STRUCTURED_KEYS = ("roles", "teams", "allow_role_fallback")


def valid_address(value: str) -> bool:
    return bool(_ADDRESS.match(value)) and len(value) <= 254


@runtime_checkable
class RecipientDirectory(Protocol):
    def resolve(self, client_id: str, role: str, team: str = "", role_shared: bool = False) -> tuple[str, ...]: ...


class StaticRecipientDirectory:
    """An in-memory directory built from the approved mapping."""

    def __init__(self, mapping: Mapping[str, Mapping[str, Any]]) -> None:
        self._clients: dict[str, dict[str, Any]] = {}
        for client, entry in mapping.items():
            if any(k in entry for k in _STRUCTURED_KEYS):
                roles, teams, fallback = entry.get("roles", {}), entry.get("teams", {}), bool(entry.get("allow_role_fallback", False))
            else:
                roles, teams, fallback = entry, {}, False
            self._clients[client] = {"roles": {r: tuple(a) for r, a in roles.items()},
                                     "teams": {t: tuple(a) for t, a in teams.items()}, "fallback": fallback}

    @staticmethod
    def _checked(addresses: tuple[str, ...]) -> tuple[str, ...]:
        if any(not valid_address(a) for a in addresses):
            raise OutlookConnectorError("an approved recipient is not a valid address")
        if len(addresses) > MAX_RECIPIENTS:
            raise OutlookConnectorError("too many approved recipients for one entry")
        return addresses

    def resolve(self, client_id: str, role: str, team: str = "", role_shared: bool = False) -> tuple[str, ...]:
        entry = self._clients.get(client_id)
        if entry is None:
            return ()
        if not team:
            return self._checked(entry["roles"].get(role, ()))
        if team in entry["teams"]:
            return self._checked(entry["teams"][team])
        if not entry["fallback"]:
            return ()  # no team entry and no explicit permission to use the role's address
        if role_shared:
            raise OutlookConnectorError("ambiguous recipients: two teams share this owner role and the team has no "
                                        "approved entry of its own; a team-level mapping is required")
        return self._checked(entry["roles"].get(role, ()))


class JsonFileRecipientDirectory(StaticRecipientDirectory):
    """Loads the approved recipient mapping from a JSON file owned by the deployment (never from a request)."""

    def __init__(self, path: str | Path) -> None:
        file = Path(path)
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OutlookConnectorError("the approved recipient file could not be read") from exc
        if not isinstance(data, dict):
            raise OutlookConnectorError("the approved recipient file must be a JSON object")
        super().__init__(data)


__all__ = ["MAX_RECIPIENTS", "JsonFileRecipientDirectory", "RecipientDirectory", "StaticRecipientDirectory",
           "valid_address"]
