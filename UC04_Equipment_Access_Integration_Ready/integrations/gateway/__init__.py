"""UC-04 advisory text through the shared Enterprise HR Agent LLM gateway. Optional, advisory only, off by default. See README.md."""

from .factory import GatewayConfigurationError, advisor_for_client, build_gateway_advisor
from .model_port import TASK, AdvisoryNarrative, AdvisoryTimeout, GatewayModelPort
from .offline import offline_llm

__all__ = ["TASK", "AdvisoryNarrative", "AdvisoryTimeout", "GatewayConfigurationError", "GatewayModelPort", "advisor_for_client",
           "build_gateway_advisor", "offline_llm"]
