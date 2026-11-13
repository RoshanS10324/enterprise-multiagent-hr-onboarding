"""A configuration change is never silent: version and content digest travel with every decision, and a change after the first evaluation
triggers a reassessment so HR re-checks approvals. UC-04 itself reads no configuration from a path (the config arrives with the request)."""

import json
import re
from pathlib import Path

from helpers import ALL, approvals, make
from sub_agent.agent import Uc04Agent

SRC = Path(__file__).resolve().parents[1] / "src" / "sub_agent"


def run(agent, payload, key):
    return agent.evaluate(payload, actor_id="SYN-USER-100", idempotency_key=key)


def test_every_result_carries_the_config_version_and_a_content_digest():
    r = run(Uc04Agent(), make(), "k")
    assert r.config_version == "alpha-v1" and re.fullmatch(r"[0-9a-f]{16}", r.config_digest)


def test_the_digest_follows_content_not_the_label():
    a, b = make(), make()
    b["config"]["lead_times"]["value"]["EQUIPMENT"] = 3  # same label, different content
    assert run(Uc04Agent(), a, "k").config_digest != run(Uc04Agent(), b, "k").config_digest
    assert run(Uc04Agent(), a, "k").config_digest == run(Uc04Agent(), make(), "k").config_digest


def test_an_unchanged_config_triggers_no_reassessment():
    agent = Uc04Agent()
    run(agent, make(), "k1")
    assert not run(agent, make(), "k2").reassessment.required


def test_a_content_change_under_the_same_label_triggers_reassessment():
    agent = Uc04Agent()
    run(agent, make(), "k1")
    changed = make()
    changed["config"]["lead_times"]["value"]["EQUIPMENT"] = 3
    r = run(agent, changed, "k2")
    assert r.reassessment.required and r.reassessment.changed_fields == ["config_digest"]


def test_a_new_version_label_triggers_reassessment_and_prior_approvals_are_not_silently_kept_or_dropped():
    agent = Uc04Agent()
    payload = make(approval_records=approvals(ALL))
    first = run(agent, payload, "k1")
    changed = json.loads(json.dumps(payload))
    changed["config"]["version"] = "alpha-v2"
    r = run(agent, changed, "k2")
    assert r.reassessment.required and "config_version" in r.reassessment.changed_fields
    assert any("approvals" in a.action.lower() and a.owner == "HR_OFFICER" for a in r.next_actions)  # HR is told to re-check approvals
    assert [i.approval_state for i in r.items] == [i.approval_state for i in first.items]  # nothing was changed behind HR's back
    assert any(e["event_type"] == "reassessment.triggered" for e in agent.service.audit.events)


def test_uc04_reads_no_configuration_from_a_path_in_the_request():
    for f in ("contracts.py", "direct.py", "orchestrated.py", "service.py", "agent.py"):
        assert not re.search(r"config_ref|config_path|read_text|open\(", (SRC / f).read_text(encoding="utf-8")), f
