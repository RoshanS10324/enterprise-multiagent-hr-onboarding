"""UC-04 sub-agent fakes module.

Consolidates all fake / mock / stub objects used in tests and local simulation
under the mandatory `sub_agent` package path. Importing from here keeps test
code independent of the internal package layout.

Everything in this module is synthetic. No real employee, client or ticket data.
"""
from __future__ import annotations

# Mock adapters (in-memory; no network)
from sub_agent.adapters.approval_mock import MockApprovalDesk  # noqa: F401
from sub_agent.adapters.case_context_mock import MockCaseContext  # noqa: F401
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk  # noqa: F401

# Scenario / fixture builders
from sub_agent.fixtures import (  # noqa: F401
    ALL,
    ACCESS_KEYS,
    CALLER_ID,
    CALLER_ROLES,
    EQUIPMENT_KEYS,
    FIXED_NOW,
    ITEMS,
    approval,
    approvals,
    build_input,
    build_input_dict,
    collection,
    deep_merge,
    delivery,
    fixed_clock,
    load,
    make,
    new_environment,
    provisioning,
    ready_payload,
    rid,
    ticket,
)

# Outlook mock connector
from connectors.outlook.mock import MockOutlookConnector  # noqa: F401

# Fake LLM advisory layer
from sub_agent.advisor import (  # noqa: F401
    ADVISORY_BANNER,
    FALLBACK_ADVISORY,
    AdvisoryReport,
    FakeLlmAdvisor,
)

__all__ = [
    # adapters
    "MockApprovalDesk",
    "MockCaseContext",
    "MockITServiceDesk",
    "MockOutlookConnector",
    # fake llm advisor
    "ADVISORY_BANNER",
    "FALLBACK_ADVISORY",
    "AdvisoryReport",
    "FakeLlmAdvisor",
    # builders
    "ALL", "ACCESS_KEYS", "CALLER_ID", "CALLER_ROLES", "EQUIPMENT_KEYS",
    "FIXED_NOW", "ITEMS",
    "approval", "approvals", "build_input", "build_input_dict",
    "collection", "deep_merge", "delivery", "fixed_clock", "load",
    "make", "new_environment", "provisioning", "ready_payload", "rid", "ticket",
]
