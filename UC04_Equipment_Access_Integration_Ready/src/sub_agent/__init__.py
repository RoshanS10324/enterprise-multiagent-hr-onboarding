"""UC-04 sub-agent package.

Re-exports the public surface of the UC-04 Equipment & Approved Access Management
sub-agent so the parent orchestrator (and integration tests) can import from
a single, stable location: `src.sub_agent`.

All business logic lives in `src/sub_agent/`. This package is a thin integration
shim that wires the sub-agent to the mandatory agent folder structure.
"""
IMPLEMENTATION_MODE = "LOCAL_SIMULATION"

from sub_agent.service import Uc04Service
from sub_agent.contracts import Uc04Input, Uc04Result
from sub_agent.fixtures import new_environment
from sub_agent.agent import Uc04Agent

from sub_agent.direct import DirectEntry
from sub_agent.orchestrated import (
    Actor,
    OrchestratedEntry,
    OrchestratorRequest,
    OrchestratorResponse,
    WorkflowRef,
)

__all__ = [
    "IMPLEMENTATION_MODE",
    "Uc04Agent",
    "Uc04Service",
    "Uc04Input",
    "Uc04Result",
    "new_environment",
    "DirectEntry",
    "OrchestratedEntry",
    "OrchestratorRequest",
    "OrchestratorResponse",
    "Actor",
    "WorkflowRef",
]
