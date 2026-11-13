"""Shared harness for the HR onboarding agent.

Independently packaged fork of hr_recruitment_common, trimmed to onboarding's actual usage and
carrying fixes for findings S1-S6 documented in HR-Onboarding-Agent/docs/ONBOARDING_COMMON_ARCHITECTURE.md.
Not a drop-in replacement for hr_recruitment_common: Purpose, Scope.subject_ids, Actor.client_id,
ApprovalItem.client_id, and ToolGateway's kill-switch check are intentional, documented breaks.
"""
