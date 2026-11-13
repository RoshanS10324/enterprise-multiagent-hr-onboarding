"""Tests for the project's structural contracts.

The modules exercised here are the seams between layers -- the Outlook
connector, the `sub_agent` facade, the guardrail declaration, the eval
entry point. The behaviour behind them is covered by the `test_*.py`
files alongside this one; what matters here is that the seams exist and
hold their shape, so a later refactor cannot quietly break an import
path or drop a declared guardrail.

Collected via `python_files` in pyproject.toml, since the name does not
match pytest's default `test_*.py`.
"""

import importlib
from pathlib import Path

import pytest
import yaml

from connectors.outlook import MailMessage, MockOutlookConnector, OutlookConnector, OutlookConnectorError
from connectors.outlook.mock import DEFAULT_MAILBOX

REPO_ROOT = Path(__file__).resolve().parent.parent

ACCEPTANCE_EMAIL = (
    "From: Taylor Synthetic Candidate <taylor.synthetic.candidate@example.test>\n"
    "To: onboarding@enterprise.example.com\n"
    "Subject: Re: Your offer [OFFER-1001] - I accept\n"
    "\n"
    "Body text that must never be read.\n"
)


# ---------------------------------------------------------------------
# connectors/outlook
# ---------------------------------------------------------------------


def test_mock_connector_satisfies_the_protocol():
    assert isinstance(MockOutlookConnector(), OutlookConnector)


def test_connector_has_no_send_method():
    # Nothing reaches a joiner without a person's approval, so the
    # transport interface is read-only by construction.
    connector = MockOutlookConnector()
    for name in ("send", "send_mail", "reply", "forward"):
        assert not hasattr(connector, name)


def test_delivered_message_exposes_sender_and_subject():
    connector = MockOutlookConnector()
    message = connector.deliver(ACCEPTANCE_EMAIL)

    assert message.sender == "taylor.synthetic.candidate@example.test"
    assert message.subject == "Re: Your offer [OFFER-1001] - I accept"
    assert message.raw == ACCEPTANCE_EMAIL


def test_fetch_returns_messages_until_acknowledged():
    connector = MockOutlookConnector()
    message = connector.deliver(ACCEPTANCE_EMAIL)

    # At-least-once: the same message comes back until acknowledged.
    assert [m.message_id for m in connector.fetch_unprocessed(DEFAULT_MAILBOX)] == [message.message_id]
    assert [m.message_id for m in connector.fetch_unprocessed(DEFAULT_MAILBOX)] == [message.message_id]

    connector.acknowledge(DEFAULT_MAILBOX, message.message_id)
    assert connector.fetch_unprocessed(DEFAULT_MAILBOX) == []


def test_acknowledging_an_unknown_id_is_a_no_op():
    MockOutlookConnector().acknowledge(DEFAULT_MAILBOX, "never-delivered")


def test_fetch_respects_limit_and_oldest_first():
    connector = MockOutlookConnector()
    first = connector.deliver(ACCEPTANCE_EMAIL)
    connector.deliver(ACCEPTANCE_EMAIL)

    fetched = connector.fetch_unprocessed(DEFAULT_MAILBOX, limit=1)
    assert [m.message_id for m in fetched] == [first.message_id]


def test_unknown_mailbox_is_a_transport_error():
    with pytest.raises(OutlookConnectorError):
        MockOutlookConnector().fetch_unprocessed("someone.else@example.test")


def test_fail_next_raises_once_then_recovers():
    connector = MockOutlookConnector()
    connector.deliver(ACCEPTANCE_EMAIL)
    connector.fail_next = True

    with pytest.raises(OutlookConnectorError):
        connector.fetch_unprocessed(DEFAULT_MAILBOX)

    assert len(connector.fetch_unprocessed(DEFAULT_MAILBOX)) == 1


def test_fixtures_load_from_the_demo_mailbox():
    connector = MockOutlookConnector.from_fixtures()
    messages = connector.fetch_unprocessed(DEFAULT_MAILBOX)

    assert messages, "expected the synthetic .eml fixtures in demo/emails/"
    assert all(isinstance(m, MailMessage) for m in messages)
    # Deterministic ordering, so eval runs are repeatable.
    ids = [m.message_id for m in messages]
    assert ids == sorted(ids)


def test_fixture_senders_are_synthetic():
    synthetic_suffixes = (".test", ".example", ".invalid")
    for message in MockOutlookConnector.from_fixtures().fetch_unprocessed(DEFAULT_MAILBOX):
        assert message.sender is None or message.sender.endswith(synthetic_suffixes), (
            f"non-synthetic sender in fixtures: {message.sender}"
        )


# ---------------------------------------------------------------------
# connectors -> intake
# ---------------------------------------------------------------------


def test_connector_output_feeds_the_intake_parser():
    from sub_agent.intake.email_intake import ParsedAcceptance, parse_acceptance_email

    message = MockOutlookConnector().deliver(ACCEPTANCE_EMAIL)
    parsed = parse_acceptance_email(message.raw)

    assert isinstance(parsed, ParsedAcceptance)
    assert parsed.offer_ref == "OFFER-1001"


# ---------------------------------------------------------------------
# src/sub_agent facade
# ---------------------------------------------------------------------


def test_sub_agent_reexports_match_the_underlying_implementation():
    from sub_agent.intake import email_intake
    from sub_agent.service import CaseService

    from sub_agent import agent

    assert agent.CaseService is CaseService
    assert agent.handle_acceptance_email is email_intake.handle_acceptance_email
    assert agent.handle_form_approved is email_intake.handle_form_approved


@pytest.mark.parametrize("module_name", ["sub_agent", "sub_agent.agent", "sub_agent.api", "sub_agent.fakes"])
def test_every_name_in_dunder_all_actually_resolves(module_name):
    module = importlib.import_module(module_name)
    missing = [name for name in module.__all__ if not hasattr(module, name)]
    assert not missing, f"{module_name}.__all__ names nothing: {missing}"


def test_sub_agent_api_exposes_an_asgi_app():
    from sub_agent.api import app, create_app

    assert callable(create_app)
    assert hasattr(app, "router")


def test_sub_agent_fakes_bundles_adapters_and_the_mock_connector():
    from sub_agent import fakes

    assert fakes.MockOutlookConnector is MockOutlookConnector
    # The in-memory adapters are the only ones that exist today.
    assert fakes.InMemoryAuditLog().entries() == []


# ---------------------------------------------------------------------
# guardrails/policy.yaml
# ---------------------------------------------------------------------

GUARDRAILS_PATH = REPO_ROOT / "guardrails" / "policy.yaml"


def test_guardrails_file_exists():
    assert GUARDRAILS_PATH.is_file()


def test_guardrails_declare_the_rules_from_claude_md():
    text = GUARDRAILS_PATH.read_text(encoding="utf-8")
    for required in (
        "sensitive_fields",
        "no_transformation",
        "scope_confinement",
        "human_approval",
        "no_self_approval",
        "source_of_truth",
        "audit",
        "kill_switches",
    ):
        assert required in text, f"guardrails/policy.yaml no longer declares {required}"


def _referenced_paths(node):
    """Every `enforced_by` value in the document that looks like a path."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "enforced_by":
                values = value if isinstance(value, list) else [value]
                yield from (v for v in values if isinstance(v, str) and "/" in v)
            else:
                yield from _referenced_paths(value)
    elif isinstance(node, list):
        for item in node:
            yield from _referenced_paths(item)


def test_every_enforced_by_path_exists():
    document = yaml.safe_load(GUARDRAILS_PATH.read_text(encoding="utf-8"))
    referenced = list(_referenced_paths(document))

    # Guard against the check passing vacuously if the document is
    # restructured and `enforced_by` stops being found.
    assert len(referenced) >= 10

    missing = [p for p in referenced if not (REPO_ROOT / p).exists()]
    assert not missing, f"guardrails point at files that do not exist: {missing}"


# ---------------------------------------------------------------------
# context_layer/skills
# ---------------------------------------------------------------------

SKILL_CONTEXT_DIR = REPO_ROOT / "context_layer" / "skills"
SKILL_CONTEXT_KEYS = {
    "skill",
    "purpose",
    "roles",
    "form_fields",
    "records",
    "tools",
    "withheld",
    "enforced_by",
}
# The one skill that may hold a salary value, and only at write time.
SALARY_SKILL = "write_employee_record"


def _skill_context_paths():
    # `_schema.yaml` documents the shape; it is not itself a skill.
    return sorted(p for p in SKILL_CONTEXT_DIR.glob("*.yaml") if not p.name.startswith("_"))


def _load_skill_context(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_skill_context_files_exist():
    assert _skill_context_paths(), "expected one context file per skill"


@pytest.mark.parametrize("path", _skill_context_paths(), ids=lambda p: p.stem)
def test_skill_context_has_the_schema_keys(path):
    document = _load_skill_context(path)
    assert set(document) == SKILL_CONTEXT_KEYS, (
        f"{path.name} keys differ from context_layer/skills/_schema.yaml"
    )
    assert document["skill"] == path.stem


@pytest.mark.parametrize("path", _skill_context_paths(), ids=lambda p: p.stem)
def test_skill_context_fields_are_real_form_fields(path):
    from sub_agent.models import OnboardingForm

    document = _load_skill_context(path)
    in_scope = set(document["form_fields"]["in_scope"])
    excluded = set(document["form_fields"]["excluded"])
    real_fields = set(OnboardingForm.model_fields)

    unknown = (in_scope | excluded) - real_fields
    assert not unknown, f"{path.name} names fields that are not on OnboardingForm: {sorted(unknown)}"

    overlap = in_scope & excluded
    assert not overlap, f"{path.name} lists the same field as both in scope and excluded: {sorted(overlap)}"


@pytest.mark.parametrize("path", _skill_context_paths(), ids=lambda p: p.stem)
def test_skill_context_accounts_for_every_sensitive_field(path):
    """A new sensitive field must be decided on explicitly, not defaulted
    into or out of scope by being left unmentioned."""
    document = _load_skill_context(path)
    declared = set(document["form_fields"]["in_scope"]) | set(document["form_fields"]["excluded"])
    assert "salary_and_terms" in declared, f"{path.name} does not say whether it sees salary_and_terms"


@pytest.mark.parametrize("path", _skill_context_paths(), ids=lambda p: p.stem)
def test_only_the_write_skill_sees_salary(path):
    document = _load_skill_context(path)
    in_scope = set(document["form_fields"]["in_scope"])

    if path.stem == SALARY_SKILL:
        assert "salary_and_terms" in in_scope
    else:
        assert "salary_and_terms" not in in_scope, (
            f"{path.name} puts salary in scope. Salary stays inside HR and payroll scopes; "
            f"only {SALARY_SKILL} reads the value, and only at write time."
        )


@pytest.mark.parametrize("path", _skill_context_paths(), ids=lambda p: p.stem)
def test_skill_context_roles_are_real_roles(path):
    from sub_agent.models import Role

    document = _load_skill_context(path)
    valid = {r.value for r in Role}
    unknown = set(document["roles"]) - valid
    assert not unknown, f"{path.name} names roles that do not exist: {sorted(unknown)}"
    assert document["roles"], f"{path.name} must name at least one role"


@pytest.mark.parametrize("path", _skill_context_paths(), ids=lambda p: p.stem)
def test_skill_context_enforcement_path_exists(path):
    document = _load_skill_context(path)
    enforced_by = document["enforced_by"]
    assert (REPO_ROOT / enforced_by).exists(), f"{path.name} points at a missing file: {enforced_by}"


@pytest.mark.parametrize("path", _skill_context_paths(), ids=lambda p: p.stem)
def test_only_the_write_skill_declares_a_write_tool(path):
    document = _load_skill_context(path)
    tools = document["tools"] or []

    if path.stem != SALARY_SKILL:
        writes = [t for t in tools if t.endswith(".write")]
        assert not writes, f"{path.name} declares a write tool: {writes}"


def test_skill_context_readme_lists_every_skill():
    readme = (SKILL_CONTEXT_DIR / "README.md").read_text(encoding="utf-8")
    for path in _skill_context_paths():
        assert path.name in readme, f"README.md does not link {path.name}"


# ---------------------------------------------------------------------
# evaluation entry point
# ---------------------------------------------------------------------


def test_run_evals_lists_the_same_scenarios_the_runner_uses(capsys):
    from evaluation.run_evals import main

    assert main(["--list"]) == 0
    listed = set(capsys.readouterr().out.split())
    on_disk = {p.stem for p in (REPO_ROOT / "evaluation" / "uc01" / "scenarios").glob("*.json")}
    assert listed == on_disk


def test_run_evals_rejects_an_unknown_scenario():
    from evaluation.run_evals import main

    with pytest.raises(SystemExit):
        main(["--scenario", "no_such_scenario"])


def test_run_evals_runs_a_single_scenario():
    from evaluation.run_evals import main

    assert main(["--scenario", "t01_happy_path"]) == 0


# ---------------------------------------------------------------------
# Document comparison is out of UC-01's scope
# ---------------------------------------------------------------------
#
# Passport and other identity-document checks belong to the
# document-validation use case. UC-01 compares the approved form with the
# offer and other HR records (R6) and treats `source` as opaque free
# text. These tests keep the removed behaviour from creeping back.
#
# Narrowed 2026-11-04 (approved by the UC-01 owner): the onboarding-form
# reader in src/sub_agent/documents/ RECORDS what the 2026 form states -
# the documents checklist as received/pending, and the visa offer terms -
# without validating any document. Outside that package the original word
# ban still applies in full; inside it, actual document-validation logic
# (document numbers, expiry checks, MRZ/OCR, document_type branching) stays
# forbidden. Validation remains UC-03's job.

SRC_DIR = REPO_ROOT / "src" / "sub_agent"
FORM_READER_DIR = SRC_DIR / "documents"
DOCUMENT_TERMS = ("passport", "visa", "emirates id", "id_document", "document_type")
DOCUMENT_VALIDATION_TERMS = (
    "passport_number", "document_number", "emirates_id_number", "expiry", "expiration",
    "mrz", "ocr", "id_document", "document_type",
)


def test_no_document_specific_logic_in_the_source_tree():
    """No module outside the form reader branches on a document-shaped name."""
    offenders = []
    for path in SRC_DIR.rglob("*.py"):
        if FORM_READER_DIR in path.parents:
            continue
        text = path.read_text(encoding="utf-8").lower()
        for term in DOCUMENT_TERMS:
            if term in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {term}")
    assert not offenders, f"document-specific logic found in UC-01: {offenders}"


def test_the_form_reader_records_documents_but_never_validates_them():
    """documents/ may name what the form lists; it may not validate a document."""
    offenders = []
    for path in FORM_READER_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for term in DOCUMENT_VALIDATION_TERMS:
            if term in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {term}")
    assert FORM_READER_DIR.is_dir()
    assert not offenders, f"document-validation logic found in UC-01's form reader: {offenders}"


def test_comparison_reason_is_the_same_for_every_source():
    """The removed DL-05 passport rule produced a different reason for
    one source. Every source must now read identically."""
    from factories import make_compare_deps, make_linked_case_and_form

    from sub_agent.compare_sources import compare_sources
    from sub_agent.models import SourceSnapshot

    case, form = make_linked_case_and_form(candidate_name="Jordan A. Lee")
    reasons = set()
    for source in ("hr_system", "offer_letter", "passport", "anything_at_all"):
        deps = make_compare_deps()
        compare_sources(
            case,
            form,
            [SourceSnapshot(source=source, values={"candidate_name": "Jordan Alexander Lee"})],
            deps,
        )
        reason = deps.approval_repo.list_for_case(case.case_id)[0].reason
        reasons.add(reason.replace(source, "<source>"))

    assert reasons == {"candidate_name differs between the approved form and <source>"}


def test_no_eval_scenario_exercises_document_comparison():
    scenarios = (REPO_ROOT / "evaluation" / "uc01" / "scenarios").glob("*.json")
    offenders = [p.name for p in scenarios if "passport" in p.read_text(encoding="utf-8").lower()]
    assert not offenders, f"document-comparison eval scenarios remain: {offenders}"


def test_runbook_has_no_document_comparison_step():
    """No passport snapshot payload and no document-comparison step. The
    runbook may still *mention* passports to say they are out of scope,
    so this looks for the payload, not the word."""
    runbook = (REPO_ROOT / "docs" / "manual-testing.md").read_text(encoding="utf-8").lower()

    for payload in ('"source": "passport"', '"source":"passport"'):
        assert payload not in runbook, f"runbook still feeds a passport snapshot: {payload}"


def test_case_folder_creation_is_preserved():
    """DocumentStore is case-folder creation at record creation, not
    document verification. Its name mentions documents; its role does
    not. It must survive the removal."""
    from sub_agent.ports import DocumentStore

    assert hasattr(DocumentStore, "create_case_folder")

    from sub_agent.fakes import InMemoryDocumentStore

    folder = InMemoryDocumentStore().create_case_folder("acme", "case-1")
    assert folder


def test_discrepancy_flagging_is_preserved():
    """R6 is an approved UC-01 requirement and survives the removal:
    the compare endpoint, the decision endpoint, and the models."""
    from sub_agent.api.app import create_app
    from sub_agent.api.container import build_container
    from sub_agent.api.settings import Settings

    app = create_app(build_container(Settings(app_env="dev")))
    routes = {getattr(r, "path", "") for r in app.routes}

    assert "/cases/{case_id}/compare" in routes
    assert "/discrepancies/{discrepancy_id}/decision" in routes
