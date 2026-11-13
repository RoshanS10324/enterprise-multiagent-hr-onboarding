"""Reusable agent context, independent of the onboarding workflow.

  knowledge/  HR policy retrieval (PolicyKnowledgeBase) over the ONE authoritative policy document,
              hr_onboarding_common/context_layer/HR_Policy_Consultants.md (loaded package-safely by
              hr_onboarding_common.context_layer.hr_policy), with section/page citations. The former
              synthetic per-client policies are gone; there is no fallback source.
  skills/     model instructions as SKILL.md files, loaded by name (load_skill). Services read
              their prompts from here, so the instructions are versioned and reviewable on their
              own rather than embedded in workflow code. Instructions only -- never policy content.

Nothing in this package imports the agent, the API, or the services that use it.
"""
