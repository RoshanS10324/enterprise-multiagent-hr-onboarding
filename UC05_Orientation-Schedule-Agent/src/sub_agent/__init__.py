"""Employee onboarding sub-agent (agent_id: employee-onboarding).

  agent.py          the onboarding workflow (LangGraph) and its entry points
  api.py            FastAPI service exposing the workflow and the individual onboarding tools
  tool_registry.py  tenant/role authorization + ToolGateway policy enforcement for every operation
  services/         deterministic business rules (intake, checklist, scheduling, welcome, policy Q&A)
  models.py         domain models and errors;  repository.py  the OnboardingPort data-access interface
  fakes.py          every in-memory test/demo double -- never a production integration
"""
