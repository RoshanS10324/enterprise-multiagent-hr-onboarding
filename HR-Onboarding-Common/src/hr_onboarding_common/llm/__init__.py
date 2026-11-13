"""LLM access for agents. Import from here; never import a provider SDK or litellm in agent code.

Build a process's LLM with `build_llm` (LLM_MODE=offline -> the agent's offline LLM, online ->
LiteLLMGateway after `validate_online_config`). For a multi-turn exchange, pass an `LLMConversation`
to `structured`."""

from hr_onboarding_common.llm.base import (BaseLLM, FakeLLM, LLMCallRecord, LLMClient, LLMError, LLMOutputError,
                                           LLMRefusal, ModelTier, extract_input_block, input_block, prompt_hash)
from hr_onboarding_common.llm.callbacks import LLMCallback, LLMEvent, logging_callback
from hr_onboarding_common.llm.conversation import ConversationScopeError, LLMConversation
from hr_onboarding_common.llm.factory import (OFFLINE, ONLINE, PROVIDER_CREDENTIALS, REASONING_EFFORTS,
                                              LLMConfigurationError, build_llm, llm_mode, llm_summary,
                                              validate_online_config)
from hr_onboarding_common.llm.gateway import LiteLLMGateway
from hr_onboarding_common.llm.redact import redact
from hr_onboarding_common.llm.routing import litellm_route, openrouter_model_id

__all__ = ["BaseLLM", "ConversationScopeError", "FakeLLM", "LLMCallRecord", "LLMCallback", "LLMClient",
           "LLMConfigurationError", "LLMConversation", "LLMError", "LLMEvent", "LLMOutputError", "LLMRefusal",
           "LiteLLMGateway", "ModelTier", "OFFLINE", "ONLINE", "PROVIDER_CREDENTIALS", "REASONING_EFFORTS",
           "build_llm", "extract_input_block", "input_block", "litellm_route", "llm_mode", "llm_summary",
           "logging_callback", "openrouter_model_id", "prompt_hash", "redact", "validate_online_config"]
