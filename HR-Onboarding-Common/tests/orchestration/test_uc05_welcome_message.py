"""The joining-day welcome message, readable in the Control Center (GET /cases/{id}/welcome-message).

UC05 drafts the welcome only on the joining day (the joining-day event), from the client's approved "welcome" template,
and HR approves it before UC05 sends it. Before that day the orchestrator shows a PREVIEW rendered the way UC05 composes
the draft (template + facts filled by code + confirmed session + buddy), without the personal note UC05's model adds at
drafting time; nothing is drafted, approved or sent. After the event it shows UC05's actual draft. The message names
the joiner, so only case_identity_reader_roles may read it. Real UC05 worker throughout.
"""

from __future__ import annotations

import pytest
from _lifecycle import (BUDDY_CANDIDATE, CLIENT, CLIENT_CONFIG, HR, TEAM_LEAD, World, start_uc05_worker)

from hr_onboarding_common.orchestration.adapters._shared import OrchestratorAuthorizationError
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError


@pytest.fixture(scope="module")
def uc05():
    worker = start_uc05_worker()
    yield worker
    worker.stop()


@pytest.fixture
def world(uc05):
    w = World(uc05)
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("add_client_config", config=CLIENT_CONFIG)
    return w


def test_before_the_joining_day_hr_sees_a_preview_from_the_clients_template(world):
    state = world.to_signoff_pending()
    msg = world.agent.welcome_message(state["case_id"], actor=HR)
    assert msg["status"] == "preview" and msg["joining_day_event"] is False
    assert msg["subject"] == "Welcome to Test A People Team, Alex Morgan!"  # CLIENT_CONFIG's template, facts by code
    assert msg["body"].startswith("Dear Alex Morgan,\n\nWelcome aboard as Software Engineer, starting 2026-12-01.")
    assert "is confirmed for" in msg["body"] and "Your onboarding buddy is" in msg["body"]
    assert world.hook("counts")["messages_by_kind"].get("welcome", 0) == 0  # nothing drafted or sent


def test_on_the_joining_day_hr_sees_uc05s_actual_draft(world):
    state = world.to_welcome_pending()  # the joining-day event: UC05 drafted the welcome for HR approval
    msg = world.agent.welcome_message(state["case_id"], actor=HR)
    assert msg["status"] == "draft" and msg["joining_day_event"] is True
    assert msg["subject"] == "Welcome to Test A People Team, Alex Morgan!" and msg["draft_status"]


def test_a_client_without_a_welcome_template_is_named_not_invented(world):
    state = world.to_signoff_pending()
    world.hook("remove_client_config", client_id=CLIENT)
    msg = world.agent.welcome_message(state["case_id"], actor=HR)
    assert msg["status"] == "no_welcome_configuration" and msg["detail"] and "subject" not in msg


def test_only_roles_that_may_read_names_see_the_message(world):
    state = world.to_signoff_pending()
    with pytest.raises(OrchestratorAuthorizationError):
        world.agent.welcome_message(state["case_id"], actor=TEAM_LEAD)


@pytest.mark.parametrize("env", [
    {"UC05_WELCOME_CONFIG_SOURCE": "anywhere"},
    {"UC05_WELCOME_CONFIG_SOURCE": "supabase", "UC05_DATABASE_MODE": "supabase"},
    {"UC05_WELCOME_CONFIG_SOURCE": "supabase", "SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": ""},
])
def test_a_misconfigured_welcome_source_stops_the_worker(env):
    with pytest.raises(WorkerCrashedError):
        worker = start_uc05_worker(**env)
        worker.stop()
