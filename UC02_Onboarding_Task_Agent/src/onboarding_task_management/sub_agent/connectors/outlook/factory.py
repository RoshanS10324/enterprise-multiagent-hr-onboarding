"""Choose the Outlook connector from the environment. Never falls back from ``graph`` to the mock.

``UC02_OUTLOOK_CONNECTOR`` = ``mock`` (the default; sends nothing) or ``graph`` (real Microsoft Graph). If ``graph`` is
requested and its configuration or recipient directory is missing, this RAISES: a deployment that asked for real email
must not silently get a simulator that reports success.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from onboarding_task_management.sub_agent.connectors.outlook.base import OutlookConnector, OutlookConnectorError
from onboarding_task_management.sub_agent.connectors.outlook.graph import (
    GraphOutlookConnector,
    GraphSettings,
    Transport,
)
from onboarding_task_management.sub_agent.connectors.outlook.mock import MockOutlookConnector
from onboarding_task_management.sub_agent.connectors.outlook.recipients import (
    JsonFileRecipientDirectory,
    RecipientDirectory,
)

ENV_CONNECTOR = "UC02_OUTLOOK_CONNECTOR"
ENV_RECIPIENTS_FILE = "UC02_RECIPIENTS_FILE"
CONNECTOR_MODES = ("mock", "graph")


def build_outlook_connector(environ: Mapping[str, str] | None = None, *, directory: RecipientDirectory | None = None,
                            transport: Transport | None = None) -> OutlookConnector:
    env = os.environ if environ is None else environ
    mode = (env.get(ENV_CONNECTOR) or "mock").strip().lower()
    if mode == "mock":
        return MockOutlookConnector()
    if mode == "graph":
        settings = GraphSettings.from_env(env)
        if directory is None:
            path = env.get(ENV_RECIPIENTS_FILE)
            if not path:
                raise OutlookConnectorError(f"graph mode needs approved recipients: set {ENV_RECIPIENTS_FILE}")
            directory = JsonFileRecipientDirectory(path)
        return GraphOutlookConnector(settings, directory, transport=transport)
    raise OutlookConnectorError(f"unknown {ENV_CONNECTOR}; expected one of {CONNECTOR_MODES}")


__all__ = ["CONNECTOR_MODES", "ENV_CONNECTOR", "ENV_RECIPIENTS_FILE", "build_outlook_connector"]
