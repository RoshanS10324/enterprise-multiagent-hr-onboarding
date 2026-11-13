"""Production safety of test data: synthetic and mock client configuration is used ONLY with an explicit UC02_ENV=development|test AND
UC02_ALLOW_SYNTHETIC_CONFIG=1. Missing, empty, unknown, staging and production environments deny it; unsafe combinations are rejected loudly; a
caller-supplied ``allow_synthetic=True`` cannot bypass the policy. Local mock database only (loaded from the read-only live snapshot)."""

from __future__ import annotations

import copy
import json
import os
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from onboarding_task_management.client_configuration import seeding
from onboarding_task_management.client_configuration.retrieval import EmployeeProfile, retrieve
from onboarding_task_management.config_safety import (
    UnsafeConfigurationError,
    require_synthetic_allowed,
    synthetic_config_allowed,
    validate_runtime_environment,
)
from onboarding_task_management.errors import MissingConfigurationError
from onboarding_task_management.persistence import OnboardingStore, PersistenceError
from onboarding_task_management.sub_agent import runtime
from onboarding_task_management.sub_agent import supabase_demo as demo
from onboarding_task_management.supabase_config import SupabaseConfigSource
from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management.supabase_store import SupabaseTableClient
from onboarding_task_management.supabase_synthetic import _guard as fixture_guard
from tests.test_supabase_store import FakePostgrest

SNAP = json.loads((Path(__file__).parent / "fixtures" / "existing_clients_config_snapshot.json").read_text(encoding="utf-8"))
TABLES = ["client_configs", "client_config_versions", "task_templates", "client_document_requirements", "client_service_policies",
          "client_milestone_templates", "client_holidays"]
OPT = "UC02_ALLOW_SYNTHETIC_CONFIG"
ON = date(2026, 12, 14)
SYN = "SYN-UAE-TEST"
STORE = {"UC02_STORE_MODE": "supabase", "SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k"}

MISSING = {OPT: "1"}
EMPTY = {"UC02_ENV": "", OPT: "1"}
BLANK = {"UC02_ENV": "   ", OPT: "1"}
PROD_OPT_IN = {"UC02_ENV": "production", OPT: "1"}
STAGING_OPT_IN = {"UC02_ENV": "staging", OPT: "1"}
UNKNOWN = {"UC02_ENV": "qa", OPT: "1"}
DEV_NO_OPT_IN = {"UC02_ENV": "development"}
DEV_OPT_IN = {"UC02_ENV": "development", OPT: "1"}
TEST_OPT_IN = {"UC02_ENV": "test", OPT: "1"}
PROD = {"UC02_ENV": "production"}
DENIED = {"missing": MISSING, "empty": EMPTY, "blank": BLANK, "production+opt-in": PROD_OPT_IN, "staging+opt-in": STAGING_OPT_IN,
          "unknown": UNKNOWN, "development without opt-in": DEV_NO_OPT_IN, "production": PROD}


def db_with(mutate=None):
    db = FakePostgrest()
    tables = copy.deepcopy({t: SNAP[t] for t in TABLES})
    if mutate:
        mutate(tables)
    for t in TABLES:
        db.tables[t] = tables[t]
    return db, SupabaseRestClient("https://example.supabase.co", "k", transport=db)


def make_real_client(tables):
    """In the MOCK copy only: SYN-UAE-TEST with its synthetic marker removed stands in for a real, approved client."""
    row = next(r for r in tables["client_configs"] if r["client_id"] == SYN)
    row["settings_payload"] = {"country": "AE"}
    row["config_hash"] = "approved-real-hash"


class PolicyTests(unittest.TestCase):
    def test_only_explicit_development_or_test_with_the_opt_in_allows_test_data(self) -> None:
        self.assertTrue(synthetic_config_allowed(DEV_OPT_IN))
        self.assertTrue(synthetic_config_allowed(TEST_OPT_IN))
        for name, env in DENIED.items():
            with self.subTest(name):
                self.assertFalse(synthetic_config_allowed(env))

    def test_missing_and_empty_environments_are_never_treated_as_development(self) -> None:
        for env in ({}, {"UC02_ENV": ""}, {"UC02_ENV": "  "}, MISSING, EMPTY, BLANK):
            with self.subTest(env=env), self.assertRaisesRegex(UnsafeConfigurationError, "UC02_ENV"):
                validate_runtime_environment(env)

    def test_unsafe_combinations_are_rejected_with_a_clear_reason(self) -> None:
        cases = {"production": (PROD_OPT_IN, "not allowed when UC02_ENV=production"), "staging": (STAGING_OPT_IN, "not allowed when UC02_ENV=staging"),
                 "unknown": (UNKNOWN, "unknown value"), "missing": (MISSING, "UC02_ENV is missing"),
                 "bad opt-in value": ({"UC02_ENV": "development", OPT: "yes"}, "must be 1, 0 or unset")}
        for name, (env, msg) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(UnsafeConfigurationError, msg):
                validate_runtime_environment(env)

    def test_valid_environments_without_test_data_are_accepted(self) -> None:
        for env in (PROD, {"UC02_ENV": "staging"}, DEV_NO_OPT_IN, {"UC02_ENV": "Production"}, {"UC02_ENV": "production", OPT: "0"}):
            with self.subTest(env=env):
                self.assertIn(validate_runtime_environment(env), {"production", "staging", "development"})
        with self.assertRaisesRegex(UnsafeConfigurationError, "UC02_ALLOW_SYNTHETIC_CONFIG=1"):
            require_synthetic_allowed(DEV_NO_OPT_IN)


class ReaderCannotBeBypassedTests(unittest.TestCase):
    def test_direct_supabase_config_source_with_allow_synthetic_true_is_refused_in_every_denied_environment(self) -> None:
        db, rest = db_with()
        for name, env in DENIED.items():
            with self.subTest(name), self.assertRaises(UnsafeConfigurationError):
                SupabaseConfigSource(rest, allow_synthetic=True, environ=env)

    def test_direct_reader_defaults_to_the_process_environment_and_fails_closed_when_it_is_unset(self) -> None:
        db, rest = db_with()
        clean = {k: v for k, v in os.environ.items() if k not in ("UC02_ENV", OPT)}
        with mock.patch.dict(os.environ, clean, clear=True):
            with self.assertRaises(UnsafeConfigurationError):
                SupabaseConfigSource(rest, allow_synthetic=True)
        with mock.patch.dict(os.environ, {**clean, **PROD_OPT_IN}, clear=True):
            with self.assertRaises(UnsafeConfigurationError):
                SupabaseConfigSource(rest, allow_synthetic=True)

    def test_direct_retrieve_with_allow_synthetic_true_is_refused_in_every_denied_environment(self) -> None:
        db, rest = db_with()
        p = EmployeeProfile(SYN, ON, "AE", "Dubai", "IN", "eng", "L4", "full_time")
        for name, env in DENIED.items():
            with self.subTest(name), self.assertRaises(UnsafeConfigurationError):
                retrieve(rest, p, allow_synthetic=True, environ=env)

    def test_a_reader_built_in_an_allowed_environment_stops_serving_test_data_if_the_environment_changes(self) -> None:
        db, rest = db_with()
        env = dict(DEV_OPT_IN)
        reader = SupabaseConfigSource(rest, allow_synthetic=True, environ=env)
        self.assertEqual(reader.load(SYN, ON).client_id, SYN)
        env["UC02_ENV"] = "production"
        with self.assertRaisesRegex(MissingConfigurationError, "denied in this environment"):
            reader.load(SYN, ON)
        env.update(UC02_ENV="development")
        env.pop(OPT)
        with self.assertRaisesRegex(MissingConfigurationError, "denied in this environment"):
            reader.load(SYN, ON)

    def test_without_the_flag_test_data_is_refused_even_in_development(self) -> None:
        db, rest = db_with()
        with self.assertRaisesRegex(MissingConfigurationError, "synthetic test fixture"):
            SupabaseConfigSource(rest, environ=DEV_OPT_IN).load(SYN, ON)
        with self.assertRaisesRegex(MissingConfigurationError, "marked mock"):
            SupabaseConfigSource(rest, environ=DEV_OPT_IN).load("CLIENT_DUBAI", ON)


class RuntimeTests(unittest.TestCase):
    def build(self, env):
        db, rest = db_with()
        store = OnboardingStore(SupabaseTableClient(rest))
        with mock.patch.object(runtime, "store_from_env", return_value=store):
            return runtime.build_service({**STORE, **env})

    def test_the_runtime_requires_an_explicit_environment(self) -> None:
        for env in ({}, {"UC02_ENV": ""}, MISSING, EMPTY):
            with self.subTest(env=env):
                with self.assertRaises(UnsafeConfigurationError):
                    runtime.require_supabase_runtime({**STORE, **env})
                with self.assertRaises(UnsafeConfigurationError):
                    self.build(env)

    def test_production_with_the_opt_in_does_not_start(self) -> None:
        with self.assertRaisesRegex(UnsafeConfigurationError, "production"):
            runtime.require_supabase_runtime({**STORE, **PROD_OPT_IN})
        with self.assertRaises(UnsafeConfigurationError):
            self.build(PROD_OPT_IN)

    def test_production_and_development_without_the_opt_in_build_a_reader_that_refuses_test_data(self) -> None:
        for env in (PROD, DEV_NO_OPT_IN):
            with self.subTest(env=env):
                svc = self.build(env)
                self.assertFalse(svc.registry.source.allow_synthetic)
                with self.assertRaises(MissingConfigurationError):
                    svc.registry.get(SYN, on=ON)

    def test_development_and_test_with_the_opt_in_serve_test_data(self) -> None:
        for env in (DEV_OPT_IN, TEST_OPT_IN):
            with self.subTest(env=env):
                svc = self.build(env)
                self.assertTrue(svc.registry.source.allow_synthetic)
                self.assertEqual(svc.registry.get(SYN, on=ON).client_id, SYN)


class WriteGuardTests(unittest.TestCase):
    def test_demo_seeding_and_fixture_guards_need_an_explicit_development_environment(self) -> None:
        db, rest = db_with()
        for env in ({}, {"UC02_ENV": ""}, {"UC02_ENV": "production"}, {"UC02_ENV": "staging"}):
            with self.subTest(env=env):
                with self.assertRaises((PersistenceError, UnsafeConfigurationError)):
                    demo.make_flow({**STORE, **env}, rest=rest)
                with self.assertRaises(PersistenceError):
                    seeding._guard({**env, seeding.ENV_ALLOW: "1"})
                with self.assertRaises(Exception):
                    fixture_guard({**env, "UC02_ALLOW_FIXTURE_WRITES": "1"})
        seeding._guard({"UC02_ENV": "development", seeding.ENV_ALLOW: "1"})
        fixture_guard({"UC02_ENV": "development", "UC02_ALLOW_FIXTURE_WRITES": "1"})


class RegressionTests(unittest.TestCase):
    def test_a_real_approved_client_still_loads_in_production_without_any_opt_in(self) -> None:
        db, rest = db_with(make_real_client)
        cfg = SupabaseConfigSource(rest, environ=PROD).load(SYN, ON)
        self.assertEqual((cfg.client_id, cfg.version, cfg.data_origin), (SYN, "1", ""))
        store = OnboardingStore(SupabaseTableClient(rest))
        with mock.patch.object(runtime, "store_from_env", return_value=store):
            svc = runtime.build_service({**STORE, **PROD})
        self.assertEqual(len(svc.registry.get(SYN, on=ON).templates), 18)

    def test_a_real_client_is_unaffected_by_the_test_data_opt_in(self) -> None:
        db, rest = db_with(make_real_client)
        self.assertEqual(SupabaseConfigSource(rest, allow_synthetic=True, environ=DEV_OPT_IN).load(SYN, ON).data_origin, "")

    def test_loading_one_client_never_returns_another_clients_rows(self) -> None:
        def add_foreign_rows(tables):
            make_real_client(tables)
            extra = copy.deepcopy(next(r for r in tables["task_templates"] if r["client_id"] == SYN))
            extra.update(client_id="CLIENT_DUBAI", template_key="dubai_only_task")
            tables["task_templates"].append(extra)
        db, rest = db_with(add_foreign_rows)
        cfg = SupabaseConfigSource(rest, environ=PROD).load(SYN, ON)
        self.assertNotIn("dubai_only_task", {t.key for t in cfg.templates})
        self.assertEqual(len(cfg.templates), 18)


if __name__ == "__main__":
    unittest.main()
