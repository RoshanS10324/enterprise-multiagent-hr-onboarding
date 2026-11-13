"""Renders the scenario registry as docs/SWAGGER_TEST_CATALOG.md and as the "how to run" text inside Swagger.

    python -m sub_agent.api.catalog          # rewrites docs/SWAGGER_TEST_CATALOG.md (run from src, or with PYTHONPATH=src)

tests/test_api_scenarios.py fails if the committed file differs from what this module renders, so the catalog cannot drift.
"""

from pathlib import Path

from sub_agent.api.scenarios import (PATHS, RESET_PATH, SCENARIOS, STATUS_PATH, TAGS, Scenario, Step, chain, describe_expect,
                                status_label)

CATALOG_PATH = Path(__file__).resolve().parents[3] / "docs" / "SWAGGER_TEST_CATALOG.md"


def _step_text(step: Step) -> str:
    if step.kind == "reset":
        return f"DEV ONLY: open `POST {RESET_PATH}`, Try it out, Execute (clears all in-memory mock state)."
    return (f"DEV ONLY: open `PUT {STATUS_PATH}`, set `case_id` to `{step.case_id}`, choose the example "
            f"**{status_label(step.status)}** and Execute.")


def how_to_steps(scenario: Scenario) -> list[str]:
    steps: list[str] = []
    for s in chain(scenario):
        steps += [_step_text(p) for p in s.prep]
        note = "this example resets state itself" if s.body.get("dev_reset_first") else "do NOT reset"
        steps.append(f"Open `POST {PATHS[s.endpoint]}`, Try it out, choose the example **{s.label}** and Execute ({note}).")
    return steps


def render_catalog() -> str:
    out = [
        "# UC-04 Swagger test catalog",
        "",
        "Generated from `src/sub_agent.api/scenarios.py` by `python -m sub_agent.api.catalog`. Do not edit by hand: a test fails if this file "
        "differs from the registry.",
        "",
        "**Local development and testing only. Not production-ready.** Mock IT desk and mock parent-case registry; no authentication; "
        "in-memory state; synthetic data (`SYN-...`); no LLM. Start it with `python run_api.py` and open http://127.0.0.1:8000/docs.",
        "",
        "## How the console is organised",
        "- Swagger groups **HTTP endpoints**, not business capabilities. UC-04 has three business endpoints, so each is listed under "
        "every category it can demonstrate (for example `POST /api/v1/sub_agent/evaluate` appears under 01, 02, 03, 04, 05, 07 and 08). "
        "It is the same operation each time; use the numbered **Examples** drop-down to pick the scenario for the category.",
        "- Each example label reads `<category> | <title> -> <expected HTTP> <expected result>`. Selecting it shows its purpose, "
        "fixture, steps and what to inspect.",
        "- **Isolation:** every single-call example carries `\"dev_reset_first\": true` (DEV ONLY), so it starts from a clean mock "
        "state. Multi-step scenarios say which examples to run first and with `dev_reset_first` false.",
        "- A visible endpoint is not proof of a tested capability. Every scenario below is executed by `tests/test_api_scenarios.py` "
        "against the real service; the \"Asserted by test\" line is what that test checks.",
        "",
        "## DEV ONLY operations (mock state, never expose publicly)",
        f"- `POST {RESET_PATH}`: clears cases, mock tickets and audit.",
        "- `GET /api/v1/dev/mock-parent-cases`: lists the four synthetic parent cases.",
        f"- `PUT {STATUS_PATH}`: sets a mock parent status (ACTIVE, ON_HOLD, WITHDRAWN, CANCELLED).",
        "- `dev_reset_first` in the request body of the three UC-04 endpoints.",
        "All of them change only in-memory mock state, are refused when the app is created with `dev_tools=False`, and `run_api.py` "
        "binds to 127.0.0.1 only.",
        "",
        f"Scenarios: {len(SCENARIOS)}.",
    ]
    for tag_key, tag in TAGS.items():
        out += ["", f"## {tag['name']}", "", tag["description"], ""]
        if tag["limits"]:
            out += ["**Not testable, or limited, over HTTP:**"] + [f"- {limit}" for limit in tag["limits"]] + [""]
        for s in (x for x in SCENARIOS if x.tag == tag_key):
            out += [f"### {s.title} (`{s.key}`)", "",
                    f"- **Purpose:** {s.purpose}",
                    f"- **Input fixture:** {s.fixture}",
                    "- **How to execute in Swagger:**"]
            out += [f"  {i}. {step}" for i, step in enumerate(how_to_steps(s), 1)]
            out += [f"- **Expected HTTP status:** {s.http}",
                    f"- **Expected business result:** {s.result}",
                    f"- **What to inspect:** {s.inspect}",
                    f"- **Asserted by test:** {describe_expect(s.expect)}"]
            if s.compare_with:
                out.append(f"- **Compare with:** `{s.compare_with}` (same facts, other entry point or view)")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":
    CATALOG_PATH.parent.mkdir(exist_ok=True)
    CATALOG_PATH.write_text(render_catalog(), encoding="utf-8", newline="\n")
    print(f"wrote {CATALOG_PATH}")
