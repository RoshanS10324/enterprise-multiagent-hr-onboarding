"""One adapter module per UC agent, translating the shared OnboardingCaseState to/from each UC's
real public interface. No UC's business logic is duplicated here -- only translation, routing,
and the orchestrator's own cross-stage bookkeeping."""
