"""The Microsoft Graph Outlook connector, against MOCKED Graph and Entra responses.

No test here contacts Microsoft, uses a real tenant, credential or mailbox, or sends anything. Every address, secret and
token below is synthetic. Passing proves the connector's logic against the documented request/response shapes, not that a
real tenant accepts it; that needs the setup in docs/UC02_OUTLOOK_GRAPH_SETUP.md and an approved test mailbox.
"""

from __future__ import annotations

import json
import logging
import unittest
import urllib.parse

from onboarding_task_management.sub_agent.connectors.outlook import (
    DeliveryStatus,
    OutlookConnector,
    OutlookConnectorError,
    OutlookDraftConnector,
    OutlookMessage,
)
from onboarding_task_management.sub_agent.connectors.outlook import graph as g
from onboarding_task_management.sub_agent.connectors.outlook.factory import build_outlook_connector
from onboarding_task_management.sub_agent.connectors.outlook.mock import MockOutlookConnector
from onboarding_task_management.sub_agent.connectors.outlook.recipients import StaticRecipientDirectory

TENANT, CLIENT_ID = "00000000-0000-0000-0000-00000000aaaa", "00000000-0000-0000-0000-00000000bbbb"
SECRET = "SYNTHETIC-SECRET-do-not-use"
TOKEN = "SYNTHETIC-ACCESS-TOKEN-xyz"
MAILBOX = "hr-onboarding@synthetic.invalid"
HR_ADDRESS = "hr-review@synthetic.invalid"
SUBJECT = "SYNTHETIC SUBJECT for review"
BODY = "SYNTHETIC BODY with a plan"


class FakeGraph:
    """A tiny Entra + Graph mail stand-in. Records every call; failures can be injected per operation."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.token_calls = 0
        self.token_status = 200
        self.token_expires_in = 3600
        self.messages: dict[str, dict] = {}
        self.folder: dict[str, str] = {}
        self.fail: dict[str, list] = {}  # operation -> queue of (status, headers) or an exception
        self.bodies: list[dict] = []
        self.sent_without_response = False
        self._n = 0

    def queue(self, operation: str, *responses) -> None:
        self.fail[operation] = list(responses)

    def _injected(self, operation: str):
        queue = self.fail.get(operation)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname == "login.microsoftonline.com":
            self.token_calls += 1
            if self.token_status != 200:
                return self.token_status, {}, {"error": "invalid_client", "error_description": f"secret {SECRET} tenant {TENANT}"}
            return 200, {}, {"access_token": TOKEN, "expires_in": self.token_expires_in, "token_type": "Bearer"}
        assert parsed.hostname == "graph.microsoft.com", parsed.hostname
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        path = urllib.parse.unquote(parsed.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        payload = json.loads(body) if body else None
        base = f"/v1.0/users/{MAILBOX}"
        if method == "POST" and path == f"{base}/messages":
            op = "create"
        elif method == "GET" and "/mailFolders/" in path:
            op = "lookup"
        elif method == "POST" and path.endswith("/send"):
            op = "send"
        else:
            raise AssertionError((method, path))
        self.calls.append((op, path))
        injected = self._injected(op)
        if injected:
            status, resp_headers = injected
            return status, {"request-id": "req-synthetic", **resp_headers}, {"error": {"code": "SyntheticError", "message": f"{SUBJECT} {BODY}"}}
        if op == "create":
            self._n += 1
            mid = f"AAMk-synthetic-{self._n}"
            self.messages[mid] = payload
            self.folder[mid] = "drafts"
            self.bodies.append(payload)
            return 201, {"request-id": "req-synthetic"}, {"id": mid}
        if op == "lookup":
            folder = path.split("/mailFolders/")[1].split("/")[0]
            key = query["$filter"].split("ep/value eq '")[1].rstrip("')").replace("''", "'")
            hits = [m for m, d in self.messages.items() if self.folder[m] == folder
                    and any(p["value"] == key for p in d["singleValueExtendedProperties"])]
            return 200, {"request-id": "req-synthetic"}, {"value": [{"id": hits[0]}] if hits else []}
        mid = path.split("/messages/")[1].rsplit("/send", 1)[0]
        if mid not in self.messages or self.folder[mid] != "drafts":
            return 404, {"request-id": "req-synthetic"}, {"error": {"code": "ErrorItemNotFound"}}
        self.folder[mid] = "sentitems"
        if self.sent_without_response:
            raise TimeoutError("response lost")
        return 202, {"request-id": "req-synthetic"}, None

    def count(self, op: str) -> int:
        return sum(1 for o, _ in self.calls if o == op)


def settings(**kw) -> g.GraphSettings:
    base = dict(tenant_id=TENANT, client_id=CLIENT_ID, client_secret=SECRET, mailbox=MAILBOX)
    base.update(kw)
    return g.GraphSettings(**base)


def directory(role="hr_officer", addresses=(HR_ADDRESS,)):
    return StaticRecipientDirectory({"C1": {role: list(addresses)}})


def message(key="K-1", approval="APPR-1", role="hr_officer", client="C1") -> OutlookMessage:
    return OutlookMessage(idempotency_key=key, onboarding_id="ONB-1", client_id=client, recipient_role=role,
                          subject_en=SUBJECT, body_en=BODY, approval_id=approval)


def connector(fake=None, *, sleeps=None, clock=None, **kw):
    fake = fake or FakeGraph()
    sleeps = sleeps if sleeps is not None else []
    c = g.GraphOutlookConnector(settings(**kw), directory(), transport=fake, sleep=sleeps.append,
                                clock=clock or (lambda: 1000.0))
    return c, fake, sleeps


class SettingsTests(unittest.TestCase):
    def test_missing_configuration_is_an_error_naming_what_is_missing(self) -> None:
        with self.assertRaises(OutlookConnectorError) as ctx:
            g.GraphSettings.from_env({g.ENV_TENANT: TENANT})
        text = str(ctx.exception)
        self.assertIn(g.ENV_SECRET, text)
        self.assertNotIn(TENANT, text)

    def test_hosts_must_be_https_microsoft_hosts(self) -> None:
        for kw in ({"authority": "http://login.microsoftonline.com"}, {"authority": "https://evil.example"},
                   {"graph_base": "https://graph.evil.example/v1.0"}, {"graph_base": "http://graph.microsoft.com/v1.0"}):
            with self.subTest(kw=kw):
                with self.assertRaises(OutlookConnectorError):
                    settings(**kw)

    def test_the_mailbox_must_look_like_a_mailbox_and_the_secret_is_never_in_a_repr(self) -> None:
        with self.assertRaises(OutlookConnectorError):
            settings(mailbox="not a mailbox")
        s = settings()
        self.assertNotIn(SECRET, repr(s))
        c, _, _ = connector()
        self.assertNotIn(SECRET, repr(c))
        self.assertEqual(repr(g._Token(TOKEN, 1.0)), "<access token hidden>")

    def test_redirects_are_never_followed(self) -> None:
        self.assertIsNone(g._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example"))

    def test_the_connector_satisfies_the_existing_port_and_the_draft_capability(self) -> None:
        c, _, _ = connector()
        self.assertIsInstance(c, OutlookConnector)
        self.assertIsInstance(c, OutlookDraftConnector)
        self.assertIsInstance(MockOutlookConnector(), OutlookConnector)  # the mock still does


class AuthenticationTests(unittest.TestCase):
    def test_the_token_request_uses_client_credentials_and_is_cached(self) -> None:
        sent = []
        fake = FakeGraph()

        def spy(method, url, headers, body):
            if "login.microsoftonline.com" in url:
                sent.append((url, body.decode()))
            return fake(method, url, headers, body)

        c = g.GraphOutlookConnector(settings(), directory(), transport=spy, sleep=lambda s: None, clock=lambda: 1000.0)
        c.create_draft(message("A"))
        c.create_draft(message("B"))
        self.assertEqual(fake.token_calls, 1)
        url, form = sent[0]
        self.assertIn(f"/{TENANT}/oauth2/v2.0/token", url)
        parsed = dict(urllib.parse.parse_qsl(form))
        self.assertEqual((parsed["grant_type"], parsed["scope"]), ("client_credentials", g.SCOPE))

    def test_an_expired_token_is_refreshed(self) -> None:
        now = [1000.0]
        c, fake, _ = connector(clock=lambda: now[0])
        c.create_draft(message("A"))
        now[0] += 3600
        c.create_draft(message("B"))
        self.assertEqual(fake.token_calls, 2)

    def test_a_401_refreshes_the_token_once_then_succeeds_or_fails_cleanly(self) -> None:
        c, fake, _ = connector()
        fake.queue("create", (401, {}))
        self.assertEqual(c.create_draft(message()).status, DeliveryStatus.DRAFT_CREATED)
        self.assertEqual(fake.token_calls, 2)
        c2, fake2, _ = connector()
        fake2.queue("create", (401, {}), (401, {}))
        with self.assertRaises(g.OutlookAuthError):
            c2.create_draft(message())

    def test_a_refused_token_request_fails_with_the_error_code_only(self) -> None:
        c, fake, _ = connector()
        fake.token_status = 401
        with self.assertRaises(g.OutlookAuthError) as ctx:
            c.create_draft(message())
        text = str(ctx.exception)
        self.assertIn("invalid_client", text)
        for secret in (SECRET, TENANT, CLIENT_ID):
            self.assertNotIn(secret, text)

    def test_a_403_is_permanent_and_not_retried(self) -> None:
        c, fake, sleeps = connector()
        fake.queue("create", (403, {}))
        with self.assertRaises(g.OutlookAuthError):
            c.create_draft(message())
        self.assertEqual((fake.count("create"), sleeps), (1, []))


class DraftTests(unittest.TestCase):
    def test_a_draft_is_created_in_the_hr_mailbox_with_recipients_from_the_directory(self) -> None:
        c, fake, _ = connector()
        receipt = c.create_draft(message())
        self.assertEqual(receipt.status, DeliveryStatus.DRAFT_CREATED)
        payload = fake.bodies[0]
        self.assertEqual(payload["subject"], SUBJECT)
        self.assertEqual(payload["toRecipients"], [{"emailAddress": {"address": HR_ADDRESS}}])
        self.assertEqual(payload["body"], {"contentType": "Text", "content": BODY})
        self.assertEqual(payload["singleValueExtendedProperties"], [{"id": g.KEY_PROPERTY, "value": "K-1"}])
        self.assertEqual(fake.count("send"), 0)  # a draft is never a send

    def test_a_role_with_no_approved_recipient_is_refused_before_any_graph_write(self) -> None:
        c, fake, _ = connector()
        with self.assertRaises(OutlookConnectorError):
            c.create_draft(message(role="hr_lead"))
        with self.assertRaises(OutlookConnectorError):
            c.create_draft(message(client="OTHER"))
        self.assertEqual(fake.count("create"), 0)

    def test_an_invalid_approved_address_is_refused(self) -> None:
        c = g.GraphOutlookConnector(settings(), directory(addresses=("not-an-address",)), transport=FakeGraph(),
                                    sleep=lambda s: None)
        with self.assertRaises(OutlookConnectorError):
            c.create_draft(message())

    def test_create_draft_is_idempotent_across_instances(self) -> None:
        c, fake, _ = connector()
        c.create_draft(message())
        again = c.create_draft(message())
        other = g.GraphOutlookConnector(settings(), directory(), transport=fake, sleep=lambda s: None)
        third = other.create_draft(message())
        self.assertEqual((again.status, third.status), (DeliveryStatus.DUPLICATE_IGNORED, DeliveryStatus.DUPLICATE_IGNORED))
        self.assertEqual(fake.count("create"), 1)

    def test_arabic_text_is_appended_and_keys_with_quotes_are_escaped_in_the_filter(self) -> None:
        c, fake, _ = connector()
        m = OutlookMessage("K'1", "ONB-1", "C1", "hr_officer", SUBJECT, BODY, subject_ar="AR-SUBJ", body_ar="AR-BODY")
        c.create_draft(m)
        self.assertIn("AR-BODY", fake.bodies[0]["body"]["content"])
        self.assertEqual(c.create_draft(m).status, DeliveryStatus.DUPLICATE_IGNORED)


class SendTests(unittest.TestCase):
    def test_without_approval_nothing_is_called(self) -> None:
        c, fake, _ = connector()
        receipt = c.send(message(approval=None))
        self.assertEqual(receipt.status, DeliveryStatus.BLOCKED_NO_APPROVAL)
        self.assertEqual((fake.calls, fake.token_calls), ([], 0))

    def test_an_approved_send_is_submitted_never_delivered(self) -> None:
        c, fake, _ = connector()
        receipt = c.send(message())
        self.assertEqual(receipt.status, DeliveryStatus.SUBMITTED)
        self.assertNotIn("delivered", receipt.status.value)
        self.assertIn("not confirmed", receipt.detail)
        self.assertTrue(receipt.accepted)
        self.assertEqual((fake.count("create"), fake.count("send")), (1, 1))
        self.assertEqual(len(c.outbox("C1")), 1)
        self.assertEqual(c.outbox("OTHER"), [])

    def test_a_repeat_send_is_a_duplicate_in_the_same_process_and_after_a_restart(self) -> None:
        c, fake, _ = connector()
        c.send(message())
        self.assertEqual(c.send(message()).status, DeliveryStatus.DUPLICATE_IGNORED)
        restarted = g.GraphOutlookConnector(settings(), directory(), transport=fake, sleep=lambda s: None)
        self.assertEqual(restarted.send(message()).status, DeliveryStatus.DUPLICATE_IGNORED)
        self.assertEqual(fake.count("send"), 1)

    def test_an_existing_draft_is_reused_not_recreated(self) -> None:
        c, fake, _ = connector()
        c.create_draft(message())
        c.send(message())
        self.assertEqual((fake.count("create"), fake.count("send")), (1, 1))

    def test_a_lost_response_then_a_retry_does_not_send_twice(self) -> None:
        c, fake, _ = connector()
        fake.sent_without_response = True  # Graph sent it, but the response never arrived
        first = c.send(message())
        # the retry found the draft gone (404), looked in Sent Items, and recognised its own message
        self.assertEqual(first.status, DeliveryStatus.DUPLICATE_IGNORED)
        fake.sent_without_response = False
        again = c.send(message())
        self.assertEqual(again.status, DeliveryStatus.DUPLICATE_IGNORED)
        self.assertEqual(sum(1 for f in fake.folder.values() if f == "sentitems"), 1)  # exactly one message was sent
        self.assertEqual(fake.count("create"), 1)

    def test_a_missing_draft_that_was_not_sent_is_an_error(self) -> None:
        c, fake, _ = connector()
        fake.queue("send", (404, {}))
        with self.assertRaises(g.OutlookApiError):
            c.send(message())


class RetryTests(unittest.TestCase):
    def test_429_honours_retry_after_then_succeeds(self) -> None:
        c, fake, sleeps = connector()
        fake.queue("create", (429, {"Retry-After": "3"}))
        self.assertEqual(c.create_draft(message()).status, DeliveryStatus.DRAFT_CREATED)
        self.assertEqual(sleeps, [3.0])

    def test_retry_after_is_capped(self) -> None:
        c, fake, sleeps = connector(max_backoff_s=5.0)
        fake.queue("create", (429, {"Retry-After": "9999"}))
        c.create_draft(message())
        self.assertEqual(sleeps, [5.0])

    def test_persistent_503_exhausts_a_bounded_number_of_attempts(self) -> None:
        c, fake, sleeps = connector()
        fake.queue("create", (503, {}), (503, {}), (503, {}))
        with self.assertRaises(g.OutlookApiError) as ctx:
            c.create_draft(message())
        self.assertEqual((fake.count("create"), sleeps), (3, [1.0, 2.0]))
        self.assertIn("3 attempt", str(ctx.exception))

    def test_a_network_error_is_retried(self) -> None:
        c, fake, sleeps = connector()
        fake.queue("create", ConnectionError("down"))
        self.assertEqual(c.create_draft(message()).status, DeliveryStatus.DRAFT_CREATED)
        self.assertEqual(len(sleeps), 1)

    def test_a_400_is_permanent_and_reports_only_graphs_error_code(self) -> None:
        c, fake, sleeps = connector()
        fake.queue("create", (400, {}))
        with self.assertRaises(g.OutlookApiError) as ctx:
            c.create_draft(message())
        self.assertEqual((fake.count("create"), sleeps), (1, []))
        self.assertIn("SyntheticError", str(ctx.exception))
        self.assertNotIn(SUBJECT, str(ctx.exception))  # Graph's message (which echoed the content) is not surfaced


class LoggingPrivacyTests(unittest.TestCase):
    def test_no_token_secret_address_subject_or_body_is_ever_logged(self) -> None:
        c, fake, _ = connector()
        fake.queue("create", (429, {"Retry-After": "1"}))
        with self.assertLogs("uc02.outlook.graph", level=logging.DEBUG) as logs:
            c.send(message())
            fake.token_status = 401
            c2, fake2, _ = connector()
            fake2.token_status = 401
            with self.assertRaises(g.OutlookAuthError):
                c2.create_draft(message("K-2"))
        everything = []
        for record in logs.records:
            everything.append(record.getMessage())
            everything.extend(str(v) for v in record.__dict__.values())
        text = "\n".join(everything)
        for sensitive in (TOKEN, SECRET, HR_ADDRESS, MAILBOX, SUBJECT, BODY, TENANT, CLIENT_ID):
            self.assertNotIn(sensitive, text)
        self.assertIn("req-synthetic", text)  # the Graph request id IS logged: it is what support needs


class FactoryTests(unittest.TestCase):
    ENV = {"UC02_OUTLOOK_CONNECTOR": "graph", g.ENV_TENANT: TENANT, g.ENV_CLIENT: CLIENT_ID, g.ENV_SECRET: SECRET,
           g.ENV_MAILBOX: MAILBOX}

    def test_the_default_and_explicit_mock_are_the_mock(self) -> None:
        self.assertIsInstance(build_outlook_connector({}), MockOutlookConnector)
        self.assertIsInstance(build_outlook_connector({"UC02_OUTLOOK_CONNECTOR": "mock"}), MockOutlookConnector)

    def test_graph_mode_never_falls_back_to_the_mock(self) -> None:
        for env in ({"UC02_OUTLOOK_CONNECTOR": "graph"}, dict(self.ENV)):  # no config; config but no recipients
            with self.subTest(env=sorted(env)):
                with self.assertRaises(OutlookConnectorError):
                    build_outlook_connector(env)
        with self.assertRaises(OutlookConnectorError):
            build_outlook_connector({"UC02_OUTLOOK_CONNECTOR": "smtp"})

    def test_graph_mode_with_configuration_and_recipients_builds_the_real_connector(self) -> None:
        c = build_outlook_connector(self.ENV, directory=directory(), transport=FakeGraph())
        self.assertIsInstance(c, g.GraphOutlookConnector)
        self.assertTrue(c.real)

    def test_the_recipient_file_is_read_from_the_deployment_not_the_request(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipients.json"
            path.write_text(json.dumps({"C1": {"hr_officer": [HR_ADDRESS]}}), encoding="utf-8")
            c = build_outlook_connector({**self.ENV, "UC02_RECIPIENTS_FILE": str(path)}, transport=FakeGraph())
            self.assertEqual(c.directory.resolve("C1", "hr_officer"), (HR_ADDRESS,))
            self.assertEqual(c.directory.resolve("C1", "hr_lead"), ())
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(OutlookConnectorError):
                build_outlook_connector({**self.ENV, "UC02_RECIPIENTS_FILE": str(path)}, transport=FakeGraph())

    def test_graph_py_is_the_only_module_that_talks_to_the_network(self) -> None:
        from pathlib import Path

        folder = Path(g.__file__).parent
        for name in ("base.py", "mock.py", "recipients.py", "factory.py"):
            source = (folder / name).read_text(encoding="utf-8")
            imports = [ln for ln in source.splitlines() if ln.strip().startswith(("import ", "from "))]
            for banned in ("urllib.request", "requests", "httpx", "smtplib", "msal", "socket"):
                with self.subTest(file=name, banned=banned):
                    self.assertFalse([ln for ln in imports if banned in ln], imports)


class MockStillWorksTests(unittest.TestCase):
    def test_the_mock_gained_drafts_without_changing_its_send_behaviour(self) -> None:
        m = MockOutlookConnector()
        self.assertEqual(m.create_draft(message()).status, DeliveryStatus.DRAFT_CREATED)
        self.assertEqual(m.create_draft(message()).status, DeliveryStatus.DUPLICATE_IGNORED)
        self.assertEqual(m.sent_count(), 0)
        self.assertEqual(m.send(message(approval=None)).status, DeliveryStatus.BLOCKED_NO_APPROVAL)
        self.assertEqual(m.send(message()).status, DeliveryStatus.SIMULATED_SENT)
        self.assertEqual(len(m.drafts("C1")), 1)


class FlowIntegrationTests(unittest.TestCase):
    """The existing HR email workflow, driven through the Graph connector over mocked Graph."""

    def setUp(self) -> None:
        from tests.test_onboarding_flow import COORD, LEAD, TODAY, make, to_review

        self.COORD, self.LEAD, self.TODAY = COORD, LEAD, TODAY
        self.flow, self.service, _ = make()
        self.oid, _ = to_review(self.flow, self.service)
        self.email = self.flow.prepare_hr_email(COORD, self.oid, TODAY)
        self.fake = FakeGraph()
        self.graph = g.GraphOutlookConnector(
            settings(), StaticRecipientDirectory({self.service.get(COORD, self.oid).client_id: {"hr_officer": [HR_ADDRESS]}}),
            transport=self.fake, sleep=lambda s: None, clock=lambda: 1000.0)

    def args(self):
        e = self.email
        return (self.oid, "hrmail", e.email_id, e.subject, e.body, e.recipient_role, self.graph)

    def events(self, action):
        return [e for e in self.service.audit.events_for(self.COORD) if e.action == action]

    def test_the_prepared_email_is_saved_as_a_draft_in_the_hr_mailbox_and_the_reference_is_recorded(self) -> None:
        self.assertEqual(self.flow.save_draft_message(self.COORD, *self.args()), "draft_created")
        saved = self.events("hrmail.draft_saved")[0]
        self.assertEqual((saved.outcome, saved.detail["real"]), ("draft_created", "True"))
        self.assertTrue(saved.detail["draft_ref"].startswith("AAMk-synthetic"))
        self.assertEqual(self.fake.count("send"), 0)
        self.assertEqual(self.flow.save_draft_message(self.COORD, *self.args()), "draft_created")  # idempotent
        self.assertEqual(self.fake.count("create"), 1)
        self.assertEqual(self.fake.bodies[0]["subject"], self.email.subject)
        self.assertEqual(self.fake.bodies[0]["body"]["content"], self.email.body)

    def test_sending_needs_a_different_hr_lead_then_is_submitted_and_never_called_delivered(self) -> None:
        from onboarding_task_management.errors import InvalidStateError, UnauthorizedActionError

        with self.assertRaises(InvalidStateError):
            self.flow.send_message(self.COORD, *self.args())  # not authorised yet
        with self.assertRaises(UnauthorizedActionError):
            self.flow.authorise_message(self.COORD, self.oid, "hrmail", self.email.email_id)
        self.flow.authorise_message(self.LEAD, self.oid, "hrmail", self.email.email_id)
        self.assertEqual(self.flow.send_message(self.COORD, *self.args()), "submitted_for_sending")
        result = self.events("hrmail.result")[0]
        self.assertEqual((result.outcome, result.detail["real"]), ("submitted_for_sending", "True"))
        self.assertNotIn("delivered", " ".join(e.outcome for e in self.service.audit.events_for(self.COORD)))
        self.assertEqual(self.flow.send_message(self.COORD, *self.args()), "submitted_for_sending")  # no second send
        self.assertEqual(self.fake.count("send"), 1)

    def test_the_content_cannot_be_changed_between_review_and_sending(self) -> None:
        from onboarding_task_management.errors import InvalidStateError

        self.flow.authorise_message(self.LEAD, self.oid, "hrmail", self.email.email_id)
        e = self.email
        with self.assertRaises(InvalidStateError):
            self.flow.send_message(self.COORD, self.oid, "hrmail", e.email_id, e.subject, e.body + " extra", e.recipient_role, self.graph)
        with self.assertRaises(InvalidStateError):
            self.flow.save_draft_message(self.COORD, self.oid, "hrmail", e.email_id, "other subject", e.body, e.recipient_role, self.graph)
        self.assertEqual(self.fake.calls, [])

    def test_a_graph_failure_is_recorded_as_failed_and_retries_stop_at_the_limit(self) -> None:
        from onboarding_task_management.errors import InvalidStateError
        from onboarding_task_management.sub_agent import flow as fl

        self.flow.authorise_message(self.LEAD, self.oid, "hrmail", self.email.email_id)
        self.fake.queue("create", *[(403, {})] * 10)
        for _ in range(fl.MAX_MESSAGE_SEND_ATTEMPTS):
            self.assertEqual(self.flow.send_message(self.COORD, *self.args()), "failed")
        with self.assertRaises(InvalidStateError):
            self.flow.send_message(self.COORD, *self.args())
        self.assertEqual(self.fake.count("send"), 0)

    def test_a_role_without_an_approved_recipient_fails_closed_and_is_recorded(self) -> None:
        self.graph.directory = StaticRecipientDirectory({})
        self.flow.authorise_message(self.LEAD, self.oid, "hrmail", self.email.email_id)
        self.assertEqual(self.flow.send_message(self.COORD, *self.args()), "failed")
        self.assertEqual(self.fake.calls, [])

    def test_a_connector_without_draft_support_is_refused_clearly(self) -> None:
        from onboarding_task_management.errors import InvalidStateError

        class SendOnly:
            def send(self, message):
                raise AssertionError("must not be called")

            def outbox(self, client_id):
                return []

        e = self.email
        with self.assertRaises(InvalidStateError):
            self.flow.save_draft_message(self.COORD, self.oid, "hrmail", e.email_id, e.subject, e.body, e.recipient_role, SendOnly())

    def test_the_mock_connector_still_drives_the_same_workflow(self) -> None:
        mock = MockOutlookConnector()
        e = self.email
        self.assertEqual(self.flow.save_draft_message(self.COORD, self.oid, "hrmail", e.email_id, e.subject, e.body, e.recipient_role, mock), "draft_created")
        self.flow.authorise_message(self.LEAD, self.oid, "hrmail", e.email_id)
        self.assertEqual(self.flow.send_message(self.COORD, self.oid, "hrmail", e.email_id, e.subject, e.body, e.recipient_role, mock), "simulated_sent")
        self.assertEqual(self.events("hrmail.result")[0].detail["real"], "False")


if __name__ == "__main__":
    unittest.main()
