"""Seed the synthetic UC-01 mock employees (data/mock/employees.json).

Safe by construction:

* Default target is a fresh IN-MEMORY dev container: no .env is read for
  persistence, nothing leaves the process, nothing is deleted.
* Every record is synthetic (client SYN-UC01-MOCK, .invalid emails) and goes
  through the real `open_onboarding_case` flow, so the outcome reported for
  each scenario is the outcome UC-01 actually produces.
* Idempotent: a repeat of the same candidate is linked to the existing case
  (LINKED_EXISTING), never inserted twice. `--twice` proves it.
* `--target supabase` refuses while UC-01's case/audit/approval/offer/
  employee-number/document repositories are not implemented for Supabase,
  and names exactly which ones. It never falls back to memory.

    python scripts/seed_mock_employees.py            # in-memory, report
    python scripts/seed_mock_employees.py --twice    # + idempotency check
    python scripts/seed_mock_employees.py --json     # machine-readable

Exit code 0 only when every scenario produced its expected outcome.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pydantic import SecretStr, ValidationError  # noqa: E402

if __name__ == "__main__" and "supabase" not in sys.argv[1:]:
    # Memory target: importing sub_agent.api builds UC-01's default app from
    # .env at import time. A preset empty SUPABASE_URL wins over .env
    # (api/settings.py), so a local .env can never decide where seed data goes.
    os.environ["SUPABASE_URL"] = ""

try:
    from sub_agent.api.container import Container, build_container  # noqa: E402
    from sub_agent.api.settings import Settings  # noqa: E402
except ValidationError as _exc:  # local .env is incomplete (supabase target)
    raise SystemExit("refusing: UC-01 configuration is not usable: "
                     + "; ".join(err["msg"] for err in _exc.errors())) from None

from sub_agent.adapters.supabase.unsupported import _Refusing  # noqa: E402
from sub_agent.models import (  # noqa: E402
    CasePlacement,
    FormStatus,
    OnboardingForm,
    PersonRecord,
    PersonRecordKind,
    VariantFlags,
)
from sub_agent.onboarding_entry import OpenCaseOutcome, OpenCaseRequest, open_onboarding_case  # noqa: E402

DATA = ROOT / "data" / "mock" / "employees.json"
INSERTED, SKIPPED, HELD, PAUSED, FAILED = "inserted", "skipped", "held", "paused", "failed"


def load_dataset(path: Path = DATA) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("mock") is not True or not str(data.get("client_id", "")).startswith("SYN-"):
        raise SystemExit("refusing: dataset is not marked mock or its client id is not synthetic (SYN-*)")
    for e in data["employees"]:
        if not e["candidate_email"].endswith(".invalid"):
            raise SystemExit(f"refusing: {e['scenario']} uses a deliverable email domain")
    return data


def _form(data: dict[str, Any], e: dict[str, Any]) -> OnboardingForm:
    status = FormStatus(e["form_status"])
    approved = status == FormStatus.APPROVED
    return OnboardingForm(
        client_id=data["client_id"],
        version=1,
        candidate_name=e["candidate_name"],
        candidate_email=e["candidate_email"],
        designation=e["designation"],
        joining_date=date.fromisoformat(e["joining_date"]),
        work_location_type=e["work_location_type"],
        client_site_id=e.get("client_site_id"),
        salary_and_terms=SecretStr(data["salary_placeholder"]),
        status=status,
        approved_by=data["form_approver"] if approved else None,
        approved_at=datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc) if approved else None,
    )


def _placement(e: dict[str, Any]) -> CasePlacement | None:
    if not e.get("location_name"):
        return None  # never invented: UC-01 holds the request
    return CasePlacement(
        location_name=e["location_name"],
        department=e.get("department"),
        grade=e.get("grade"),
        nationality_group=e.get("nationality_group"),
    )


def build_target(target: str) -> Container:
    if target == "memory":
        # Explicit settings: nothing from .env decides where data goes.
        return build_container(Settings(app_env="dev", dedup_hmac_key="mock-seed-only",
                                        system_api_key="mock-seed-only"))
    try:
        container = build_container(Settings.from_env())
    except ValidationError as exc:
        # Messages only: str(exc) would echo the input values (input_value=...).
        messages = "; ".join(err["msg"] for err in exc.errors())
        raise SystemExit(f"refusing --target supabase: configuration is not usable: {messages}") from None
    except (ValueError, RuntimeError) as exc:
        # Container messages name variables, never values.
        raise SystemExit(f"refusing --target supabase: configuration is not usable: {exc}") from None
    refusing = sorted(
        type(repo).__name__
        for repo in (container.onboarding_deps.case_repo, container.onboarding_deps.approval_repo,
                     container.onboarding_deps.audit_log, container.onboarding_deps.document_store,
                     container.onboarding_deps.number_generator, container.intake_deps.offer_repo)
        if isinstance(repo, _Refusing)
    )
    if refusing:
        raise SystemExit(
            "refusing --target supabase: these UC-01 repositories are not implemented for Supabase yet, "
            f"so no employee could be persisted: {', '.join(refusing)}"
        )
    return container


def seed(container: Container, data: dict[str, Any]) -> list[dict[str, Any]]:
    deps = container.onboarding_deps
    case_by_scenario: dict[str, str] = {}
    report: list[dict[str, Any]] = []

    for e in data["employees"]:
        row: dict[str, Any] = {"scenario": e["scenario"], "expected": e["expected"]["outcome"]}
        prior = e.get("prior_record")
        if prior and not deps.person_directory.find_by_email(data["client_id"], e["candidate_email"]):
            deps.person_directory.add(PersonRecord(
                client_id=data["client_id"], email=e["candidate_email"], full_name=e["candidate_name"],
                kind=PersonRecordKind(prior["kind"]), hris_external_ref=prior.get("hris_external_ref")))
        try:
            form = _form(data, e)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in err["loc"]) or err["msg"] for err in exc.errors()})
            row.update(result=FAILED, outcome="INVALID", detail="; ".join(fields) or "validation error",
                       messages=[err["msg"] for err in exc.errors()])
            report.append(row)
            continue

        result = open_onboarding_case(OpenCaseRequest(
            request_id=f"MOCK-{e['scenario']}",
            client_id=data["client_id"],
            hr_officer_id=data["hr_officer_id"],
            offer_accepted=e["offer_status"] == "ACCEPTED",
            form=form,
            hiring_freeze=bool(e.get("hiring_freeze")),
            declared_variants=VariantFlags(**e["declared_variants"]) if e.get("declared_variants") else None,
            placement=_placement(e),
        ), deps)

        row["outcome"] = result.outcome.value
        row["reasons"] = list(result.reasons)
        if result.outcome == OpenCaseOutcome.CASE_OPENED:
            case = result.case
            case_by_scenario[e["scenario"]] = case.case_id
            row.update(result=INSERTED, case_id=case.case_id, employee_no=case.employee_no,
                       case_status=case.status.value, approvals=len(result.approvals_created),
                       variants=[k for k, v in case.variants.model_dump().items() if v])
        elif result.outcome == OpenCaseOutcome.LINKED_EXISTING:
            row.update(result=SKIPPED, existing_case_id=result.existing_case_id)
        elif result.outcome == OpenCaseOutcome.HELD:
            row["result"] = HELD
        else:
            row["result"] = PAUSED
        report.append(row)

    for row, e in zip(report, data["employees"]):
        row["ok"] = _matches(row, e["expected"], case_by_scenario)
    return report


def _matches(row: dict[str, Any], expected: dict[str, Any], cases: dict[str, str]) -> bool:
    if row.get("outcome") != expected["outcome"]:
        return False
    if "reasons" in expected and row.get("reasons") != expected["reasons"]:
        return False
    if "approvals" in expected and row.get("approvals") != expected["approvals"]:
        return False
    if "variant" in expected and expected["variant"] not in row.get("variants", []):
        return False
    if "case_status" in expected and row.get("case_status") != expected["case_status"]:
        return False
    if "links_to_scenario" in expected and row.get("existing_case_id") != cases.get(expected["links_to_scenario"]):
        return False
    if "error_contains" in expected and expected["error_contains"] not in " ".join(row.get("messages", [])):
        return False
    return True


def summarise(report: list[dict[str, Any]]) -> dict[str, int]:
    counts = {k: 0 for k in (INSERTED, SKIPPED, HELD, PAUSED, FAILED)}
    for row in report:
        counts[row["result"]] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=["memory", "supabase"], default="memory")
    parser.add_argument("--twice", action="store_true", help="seed again and require zero new inserts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    data = load_dataset()
    container = build_target(args.target)
    first = seed(container, data)
    runs = {"first": first}
    if args.twice:
        runs["second"] = seed(container, data)

    ok = all(r["ok"] for r in first)
    if args.twice:
        second_counts = summarise(runs["second"])
        ok = ok and second_counts[INSERTED] == 0

    if args.json:
        print(json.dumps({"target": args.target, "ok": ok,
                          **{name: {"summary": summarise(rep), "records": rep} for name, rep in runs.items()}},
                         indent=1, default=str))
    else:
        print(f"UC-01 mock seed  target={args.target}  client={data['client_id']}  (synthetic data only)")
        for name, rep in runs.items():
            print(f"-- {name} run")
            for r in rep:
                extra = r.get("employee_no") or r.get("existing_case_id") or ", ".join(r.get("reasons") or []) \
                    or r.get("detail", "")
                print(f"  [{'OK ' if r['ok'] or name == 'second' else 'BAD'}] {r['scenario']:26s} "
                      f"{r['result']:8s} {r.get('outcome', ''):16s} {extra}")
            print(f"   summary: {summarise(rep)}")
        if args.target == "memory":
            print("NOTE: in-memory target - nothing was persisted; the records vanish when this process exits.")
        print("RESULT:", "ALL SCENARIOS AS EXPECTED" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
