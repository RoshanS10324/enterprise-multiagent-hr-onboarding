"""Central HR onboarding orchestrator: drives UC01-UC05 through their real public interfaces.

Kept separate from `hr_onboarding_common.agent` (which stays generic and reusable by every
agent, including UC05 itself) because this subpackage is the only part of the harness that
knows about the five UC agents by name, their three-way module-name collision, and their five
different contracts. `agent.py` re-exports `OnboardingOrchestratorAgent` so it remains the
documented public entry point.
"""

from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent

__all__ = ["OnboardingOrchestratorAgent"]
