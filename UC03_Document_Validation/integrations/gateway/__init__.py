"""UC-03 model help through the shared Enterprise HR Agent LLM gateway. Optional, advisory only, off by default.

See README.md in this folder.
"""

from .advisor import DRAFT_TASK, NAME_TASK, DraftReply, GatewayAdvisor, NameConcernReply
from .offline import offline_llm

__all__ = [
    "DRAFT_TASK",
    "NAME_TASK",
    "DraftReply",
    "GatewayAdvisor",
    "NameConcernReply",
    "offline_llm",
]
