"""The Supabase REST client and gateway, exercised offline through an injected transport."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from onboarding_task_management.persistence import PersistenceError
from onboarding_task_management.supabase_rest import (
    CASE_COLUMNS,
    SupabaseGateway,
    SupabaseRestClient,
)

KEY = "synthetic-key-not-real"


class Fake:
    def __init__(self, status=200, payload=None):
        self.status, self.payload, self.calls = status, payload, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self.status, self.payload


def make(status=200, payload=None):
    fake = Fake(status, payload)
    return SupabaseRestClient("https://example.supabase.co", KEY, transport=fake), fake


class ClientTests(unittest.TestCase):
    def test_select_sends_equality_filters_columns_and_the_key_header(self) -> None:
        client, fake = make(payload=[{"a": 1}])
        self.assertEqual(client.select("t", {"client_id": "C1"}, order_by="a", columns="a"), [{"a": 1}])
        method, url, headers, _ = fake.calls[0]
        query = parse_qs(urlparse(url).query)
        self.assertEqual((method, query["client_id"], query["select"], query["order"]),
                         ("GET", ["eq.C1"], ["a"], ["a"]))
        self.assertEqual(headers["apikey"], KEY)

    def test_https_and_credentials_are_required(self) -> None:
        with self.assertRaises(PersistenceError):
            SupabaseRestClient("http://example.supabase.co", KEY)
        with self.assertRaises(PersistenceError):
            SupabaseRestClient("https://example.supabase.co", "")
        with self.assertRaises(PersistenceError) as ctx:
            SupabaseRestClient.from_env({})
        self.assertNotIn(KEY, str(ctx.exception))

    def test_errors_are_raised_and_never_contain_the_key(self) -> None:
        client, _ = make(status=401, payload={"message": "Invalid API key"})
        with self.assertRaises(PersistenceError) as ctx:
            client.select("t", {"client_id": "C1"})
        self.assertNotIn(KEY, str(ctx.exception) + repr(ctx.exception.__dict__))

    def test_a_none_filter_and_an_unfiltered_delete_are_refused(self) -> None:
        client, fake = make(payload=[])
        with self.assertRaises(PersistenceError):
            client.select("t", {"client_id": None})
        with self.assertRaises(PersistenceError):
            client.delete("t", {})
        self.assertEqual(fake.calls, [])

    def test_an_upsert_must_be_confirmed_row_for_row(self) -> None:
        client, _ = make(payload=[{"k": 1}])
        with self.assertRaises(PersistenceError):
            client.upsert("t", [{"k": 1}, {"k": 2}])
        ok, fake = make(payload=[{"k": 1}, {"k": 2}])
        self.assertEqual(ok.upsert("t", [{"k": 1}, {"k": 2}], on_conflict="k"), 2)
        self.assertIn("on_conflict=k", fake.calls[0][1])
        self.assertEqual(ok.upsert("t", []), 0)


class GatewayTests(unittest.TestCase):
    def test_cases_are_read_without_personal_columns(self) -> None:
        self.assertNotIn("candidate_name", CASE_COLUMNS)
        self.assertNotIn("candidate_email", CASE_COLUMNS)
        client, fake = make(payload=[{"case_id": "x"}])
        self.assertEqual(SupabaseGateway(client).get_case("C1", "x"), {"case_id": "x"})
        self.assertEqual(parse_qs(urlparse(fake.calls[0][1]).query)["select"], [CASE_COLUMNS])

    def test_an_unknown_case_is_none(self) -> None:
        client, _ = make(payload=[])
        self.assertIsNone(SupabaseGateway(client).get_case("C1", "x"))

    def test_config_meta_reports_a_draft_version_as_not_approved(self) -> None:
        responses = iter([
            [{"working_days": [1, 2], "timezone": "UTC", "is_active": True,
              "template_count": 0, "milestone_count": 0}],
            [{"version": 1, "status": "draft", "approved_by": None}],
        ])
        gateway = SupabaseGateway(SupabaseRestClient(
            "https://example.supabase.co", KEY,
            transport=lambda *_: (200, next(responses)),
        ))
        meta = gateway.client_config_meta("C1")
        self.assertEqual((meta["version_status"], meta["approved"]), ("draft", False))

    def test_config_meta_needs_a_named_approver_and_is_none_for_an_unknown_client(self) -> None:
        head = [{"working_days": [1], "timezone": "UTC", "is_active": True,
                 "template_count": 1, "milestone_count": 0}]
        for approver, expected in (("hr-lead", True), (None, False)):
            responses = iter([head, [{"version": 2, "status": "approved", "approved_by": approver}]])
            gateway = SupabaseGateway(SupabaseRestClient(
                "https://example.supabase.co", KEY, transport=lambda *_: (200, next(responses))))
            self.assertEqual(gateway.client_config_meta("C1")["approved"], expected)
        client, _ = make(payload=[])
        self.assertIsNone(SupabaseGateway(client).client_config_meta("NOPE"))


if __name__ == "__main__":
    unittest.main()


class SchemaAndModeTests(unittest.TestCase):
    def test_verify_schema_reports_missing_tables_and_columns(self) -> None:
        from onboarding_task_management.supabase_schema import REQUIRED, verify_schema

        full = {t: set(c) for t, c in REQUIRED.items()}
        self.assertEqual(verify_schema(full), [])
        del full["task_templates"]
        full["onboarding_tasks"].discard("status_summary")
        problems = verify_schema(full)
        self.assertIn("missing table task_templates", problems)
        self.assertTrue(any("status_summary" in p for p in problems))

    def test_supabase_mode_never_falls_back_to_memory(self) -> None:
        from onboarding_task_management.persistence import store_from_env

        with self.assertRaises(PersistenceError):
            store_from_env({"UC02_STORE_MODE": "supabase"})  # not configured
        with self.assertRaises(PersistenceError):
            store_from_env({"PERSISTENCE_BACKEND": "supabase"})


class EnvironmentNameTests(unittest.TestCase):
    def test_supabase_url_is_preferred_and_the_next_public_name_is_the_fallback(self) -> None:
        for env, expected in (
            ({"SUPABASE_URL": "https://a.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": KEY}, "https://a.supabase.co/rest/v1/"),
            ({"NEXT_PUBLIC_SUPABASE_URL": "https://b.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": KEY}, "https://b.supabase.co/rest/v1/"),
            ({"SUPABASE_URL": "https://a.supabase.co", "NEXT_PUBLIC_SUPABASE_URL": "https://b.supabase.co",
              "SUPABASE_SERVICE_ROLE_KEY": KEY}, "https://a.supabase.co/rest/v1/"),
        ):
            self.assertEqual(SupabaseRestClient.from_env(env)._base, expected)

    def test_the_error_names_both_url_variables_and_never_a_key(self) -> None:
        with self.assertRaises(PersistenceError) as ctx:
            SupabaseRestClient.from_env({"SUPABASE_SERVICE_ROLE_KEY": KEY})
        text = str(ctx.exception) + repr(ctx.exception.__dict__)
        self.assertIn("SUPABASE_URL", text)
        self.assertIn("NEXT_PUBLIC_SUPABASE_URL", text)
        self.assertNotIn(KEY, text)
