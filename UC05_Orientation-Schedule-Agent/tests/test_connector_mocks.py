"""The connector simulations, connectors/<name>/mock.py: each implements its base.py interface,
behaves deterministically, simulates the provider outcomes callers must handle, is marked as
simulated, and can be reset between tests. The last section checks that the onboarding workflow
still needs HR approval before a meeting is confirmed or anyone is invited.

The typed assignments in test_every_mock_satisfies_its_interface_statically are what mypy checks
when run on this file (the runtime check above it compares member names, kinds and defaults).
"""

import importlib
import inspect
from datetime import datetime, timedelta, timezone
from typing import Protocol

import pytest

from connectors.calendar.base import BookingStatus, CalendarBookingError, CalendarConflictError, CalendarPort
from connectors.calendar.mock import FAILING_ATTENDEE, MockCalendar
from connectors.email.base import EmailPort, EmailServiceError, OfferAcceptanceEvent
from connectors.email.mock import FAILING_EVENT_ID, MockEmailAdapter, MockEmailSender, build_mock_email_adapter
from connectors.employee_directory.base import EmployeeDirectoryPort
from connectors.employee_directory.mock import MockEmployeeDirectory, build_mock_employee_directory
from connectors.notifications.base import (NotificationChannel, NotificationDeliveryError, NotificationMessage,
                                           NotificationPort, NotificationRejectedError)
from connectors.notifications.mock import MockNotifier
from connectors.teams.base import TeamsPort
from connectors.teams.mock import MockTeamsAdapter
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.platform.hitl import Decision
from sub_agent import fakes
from sub_agent.agent import AGENT_HOME, OnboardingAgent, OnboardingWorkflowInput, WorkflowEvent
from sub_agent.models import SessionStatus

START = datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
ACME, GLOBEX = "CLIENT-ACME", "CLIENT-GLOBEX"


def message(*, channel=NotificationChannel.EMAIL, client_id=ACME, recipient_id="EMP-001", kind="welcome",
            address: str | None = "sara.almarzooqi@example.com", key: str | None = None) -> NotificationMessage:
    return NotificationMessage(channel=channel, client_id=client_id, employee_id="EMP-001", recipient_id=recipient_id,
                               recipient_address=address, kind=kind, subject="Welcome", body="Hello",
                               idempotency_key=key)


# =========================================================================== 1-2: import and interface

@pytest.mark.parametrize("module", ["connectors.calendar.mock", "connectors.email.mock",
                                    "connectors.employee_directory.mock", "connectors.notifications.mock",
                                    "connectors.teams.mock"])
def test_every_connector_mock_imports(module):
    assert importlib.import_module(module).__doc__.startswith("TEST / DEMO ONLY")


def test_fakes_reexports_the_connector_mocks_rather_than_duplicating_them():
    assert fakes.FakeCalendar is fakes.MockCalendar is MockCalendar
    assert fakes.FakeNotifier is fakes.MockNotifier is MockNotifier
    assert fakes.MockEmailAdapter is MockEmailAdapter and fakes.MockEmployeeDirectory is MockEmployeeDirectory
    assert fakes.MockEmailSender is MockEmailSender and fakes.MockTeamsAdapter is MockTeamsAdapter
    assert fakes.build_mock_email_adapter is build_mock_email_adapter
    assert fakes.build_mock_employee_directory is build_mock_employee_directory
    deps = fakes.build_fake_dependencies()
    assert type(deps.calendar) is MockCalendar and type(deps.notifier) is MockNotifier
    assert type(deps.email) is MockEmailAdapter and type(deps.directory) is MockEmployeeDirectory


def _members(protocol: type) -> dict[str, object]:
    """Every attribute and method a Protocol (and the Protocols it extends) declares."""
    members: dict[str, object] = {}
    for cls in reversed(protocol.__mro__):
        if cls in (object, Protocol) or not getattr(cls, "_is_protocol", False):
            continue
        members.update({name: None for name in getattr(cls, "__annotations__", {}) if not name.startswith("_")})
        members.update({name: fn for name, fn in vars(cls).items()
                        if inspect.isfunction(fn) and not name.startswith("_")})  # typing adds __init__
    assert members, f"{protocol.__name__} declares no members"
    return members


CONFORMANCE = [(CalendarPort, MockCalendar), (EmailPort, build_mock_email_adapter),
               (EmployeeDirectoryPort, build_mock_employee_directory), (NotificationPort, MockNotifier),
               (NotificationPort, MockEmailSender), (NotificationPort, MockTeamsAdapter), (TeamsPort, MockTeamsAdapter)]


@pytest.mark.parametrize("protocol, factory", CONFORMANCE, ids=lambda x: getattr(x, "__name__", ""))
def test_every_mock_satisfies_its_interface(protocol, factory):
    mock = factory()
    for name, declared in _members(protocol).items():
        assert hasattr(mock, name), f"{type(mock).__name__} lacks {name}"
        if declared is None:
            continue
        expected = inspect.signature(declared).parameters
        actual = inspect.signature(getattr(type(mock), name)).parameters
        for pname, p in expected.items():
            assert pname in actual and actual[pname].kind == p.kind, f"{name}({pname}) differs"
            assert (actual[pname].default is inspect.Parameter.empty) == (p.default is inspect.Parameter.empty)


def test_every_mock_satisfies_its_interface_statically() -> None:  # annotated, so mypy checks the body
    calendar: CalendarPort = MockCalendar()
    email: EmailPort = build_mock_email_adapter()
    directory: EmployeeDirectoryPort = build_mock_employee_directory()
    notifier: NotificationPort = MockNotifier()
    email_sender: NotificationPort = MockEmailSender()
    teams: TeamsPort = MockTeamsAdapter()
    assert all([calendar, email, directory, notifier, email_sender, teams])


# =========================================================================== calendar

def test_calendar_holds_are_silent_and_attendees_are_invited_only_on_confirmation():
    calendar = MockCalendar()
    hold = calendar.book_event(attendees=["EMP-001", "TL-001"], start=START, end=END, title="Induction")

    assert hold.status == BookingStatus.TENTATIVE and not hold.invitations_sent and calendar.invited == []
    assert hold.event_ref.startswith("sim-evt-")
    assert calendar.check_availability(["TL-001"], START + timedelta(minutes=30), END) is False
    assert calendar.check_availability(["TL-001"], END, END + timedelta(hours=1)) is True  # adjacent slot is free
    assert calendar.get_booking(hold.event_ref) == hold

    confirmed = calendar.confirm_event(hold.event_ref)

    assert confirmed.status == BookingStatus.CONFIRMED and confirmed.invitations_sent
    assert sorted(a for _, a in calendar.invited) == ["EMP-001", "TL-001"]
    assert [b.event_ref for b in calendar.list_bookings(attendee="TL-001")] == [hold.event_ref]


def test_calendar_conflicts_are_business_outcomes_not_provider_errors():
    calendar = MockCalendar(busy=[("TL-001", START, END)])
    with pytest.raises(CalendarConflictError):
        calendar.book_event(attendees=["EMP-001", "TL-001"], start=START, end=END, title="Induction")
    assert calendar.list_bookings() == []


def test_a_cancelled_booking_frees_its_slot_and_stays_retrievable_as_cancelled():
    calendar = MockCalendar()
    hold = calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="Induction")

    calendar.cancel_event(hold.event_ref)
    calendar.cancel_event(hold.event_ref)  # idempotent

    assert calendar.get_booking(hold.event_ref).status == BookingStatus.CANCELLED
    assert calendar.check_availability(["EMP-001"], START, END) and calendar.list_bookings() == []
    with pytest.raises(KeyError):
        calendar.confirm_event(hold.event_ref)  # a cancelled event cannot be confirmed


@pytest.mark.parametrize("attendees, start, end", [
    ([], START, END), (["EMP-001", " "], START, END), (["EMP-001"], START.replace(tzinfo=None), END),
    (["EMP-001"], END, START), (["EMP-001"], START, START)])
def test_calendar_refuses_invalid_requests(attendees, start, end):
    calendar = MockCalendar()
    with pytest.raises(ValueError):
        calendar.book_event(attendees=attendees, start=start, end=end, title="x")
    with pytest.raises(ValueError):
        calendar.check_availability(attendees, start, end)
    assert calendar.list_bookings() == []


def test_unknown_calendar_events_are_not_found():
    calendar = MockCalendar()
    for call in (lambda: calendar.confirm_event("sim-evt-missing"), lambda: calendar.get_booking("sim-evt-missing")):
        with pytest.raises(KeyError):
            call()


def test_a_repeated_idempotency_key_returns_the_same_booking():
    calendar = MockCalendar()
    first = calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="x", idempotency_key="op-1")
    again = calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="x", idempotency_key="op-1")
    assert again.event_ref == first.event_ref and len(calendar.list_bookings()) == 1


def test_a_lost_booking_response_is_settled_by_retrying_with_the_same_key():
    calendar = MockCalendar(lose_next_booking_responses=1)
    with pytest.raises(CalendarBookingError, match="the event was created"):
        calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="x", idempotency_key="op-1")
    assert len(calendar.list_bookings()) == 1  # the outcome was uncertain: it did happen

    retried = calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="x", idempotency_key="op-1")

    assert [b.event_ref for b in calendar.list_bookings()] == [retried.event_ref]


def test_a_provider_without_idempotency_does_not_recognise_a_repeated_key():
    calendar = MockCalendar(idempotent_booking=False, lose_next_booking_responses=1)
    with pytest.raises(CalendarBookingError):
        calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="x", idempotency_key="op-1")
    # the retry is a brand-new request: here it collides with the event the lost request created
    with pytest.raises(CalendarConflictError):
        calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="x", idempotency_key="op-1")


def test_calendar_outages_create_nothing_and_recover():
    calendar = MockCalendar(fail_next_bookings=1)
    with pytest.raises(CalendarBookingError, match="transient"):
        calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="x")
    with pytest.raises(CalendarBookingError):
        calendar.book_event(attendees=["EMP-001", FAILING_ATTENDEE], start=START, end=END, title="x")
    assert calendar.list_bookings() == []
    assert calendar.book_event(attendees=["EMP-001"], start=START, end=END, title="x").status == BookingStatus.TENTATIVE


def test_a_failed_confirmation_leaves_the_hold_tentative_and_uninvited():
    calendar = MockCalendar(fail_next_confirms=1, invite_failures={"TL-001"})
    hold = calendar.book_event(attendees=["EMP-001", "TL-001"], start=START, end=END, title="x")
    with pytest.raises(CalendarBookingError):
        calendar.confirm_event(hold.event_ref)
    assert calendar.get_booking(hold.event_ref).status == BookingStatus.TENTATIVE and calendar.invited == []

    confirmed = calendar.confirm_event(hold.event_ref)

    assert confirmed.invitation_failures == ["TL-001"]  # the booking stands; one invite bounced


def test_calendar_reset_restores_the_constructed_state():
    calendar = MockCalendar(busy=[("TL-001", START, END)], fail_next_bookings=1)
    with pytest.raises(CalendarBookingError):
        calendar.book_event(attendees=["EMP-001"], start=END, end=END + timedelta(hours=1), title="x")
    hold = calendar.book_event(attendees=["EMP-001"], start=END, end=END + timedelta(hours=1), title="x")
    calendar.confirm_event(hold.event_ref)

    calendar.reset()

    assert calendar.list_bookings() == [] and calendar.invited == [] and calendar.fail_next_bookings == 1
    assert not calendar.check_availability(["TL-001"], START, END)  # the constructor's busy slot is back
    with pytest.raises(KeyError):
        calendar.get_booking(hold.event_ref)


# =========================================================================== email: inbound offer events

def test_the_mailbox_returns_copies_of_synthetic_offer_events():
    mailbox = build_mock_email_adapter()
    event = mailbox.get_event("EVT-001")
    assert event.employee_id == "EMP-TEST-001"
    event.employee_id = "EMP-TAMPERED"
    assert mailbox.get_event("EVT-001").employee_id == "EMP-TEST-001"
    assert mailbox.get_event("EVT-MALFORMED").employee_id is None  # unparseable, not missing


def test_mailbox_failures_and_missing_events_are_distinct():
    mailbox = build_mock_email_adapter()
    with pytest.raises(EmailServiceError):
        mailbox.get_event(FAILING_EVENT_ID)  # the provider failed: no data at all
    with pytest.raises(KeyError):
        mailbox.get_event("EVT-DOES-NOT-EXIST")
    with pytest.raises(KeyError):
        mailbox.mark_processed("EVT-DOES-NOT-EXIST")


def test_mailbox_processed_flag_and_reset():
    mailbox = build_mock_email_adapter()
    mailbox.events["EVT-EXTRA"] = OfferAcceptanceEvent(event_id="EVT-EXTRA", employee_id="EMP-TEST-002",
                                                       received_at=START, raw_subject="s", raw_body="b")
    mailbox.mark_processed("EVT-001")
    assert mailbox.is_processed("EVT-001") and not mailbox.is_processed("EVT-002")

    mailbox.reset()

    assert not mailbox.is_processed("EVT-001") and "EVT-EXTRA" not in mailbox.events
    assert set(mailbox.events) == {"EVT-001", "EVT-002", "EVT-010", "EVT-MALFORMED"}


# =========================================================================== email: outbound (simulated sender)

def test_the_email_sender_records_a_simulated_delivery():
    sender = MockEmailSender()
    receipt = sender.send(message())
    assert receipt.simulated and receipt.delivery_ref.startswith("sim-email-")
    (sent,) = sender.sent_to("EMP-001", kind="welcome")
    assert sent.recipient_address == "sara.almarzooqi@example.com"


@pytest.mark.parametrize("bad", [dict(channel=NotificationChannel.TEAMS), dict(address=None), dict(address="  ")])
def test_the_email_sender_refuses_what_is_not_a_deliverable_email(bad):
    sender = MockEmailSender()
    with pytest.raises(NotificationRejectedError):
        sender.send(message(**bad))
    assert sender.sent == []


def test_the_email_sender_never_sends_twice_after_a_lost_response_with_the_same_key():
    sender = MockEmailSender(lose_next_responses=1)
    with pytest.raises(NotificationDeliveryError, match="was delivered"):
        sender.send(message(key="op-welcome-1"))

    receipt = sender.send(message(key="op-welcome-1"))

    assert len(sender.sent) == 1 and receipt.delivery_ref.startswith("sim-email-")


def test_an_email_outage_delivers_nothing_and_a_retry_succeeds():
    sender = MockEmailSender(fail_next=1)
    with pytest.raises(NotificationDeliveryError, match="unavailable"):
        sender.send(message(key="op-1"))
    assert sender.sent == []
    sender.send(message(key="op-1"))
    assert len(sender.sent) == 1


# =========================================================================== employee directory

def test_the_directory_returns_team_manager_and_joining_date():
    record = build_mock_employee_directory().get_employee("EMP-TEST-001")
    assert (record.team, record.team_lead_id, record.joining_date.isoformat(), record.client_id) == (
        "Platform", "TL-TEST-001", "2026-12-01", "CLIENT-TEST-A")


def test_a_missing_employee_is_not_found_and_an_incomplete_one_is_returned_as_is():
    directory = build_mock_employee_directory()
    with pytest.raises(KeyError):
        directory.get_employee("EMP-DOES-NOT-EXIST")
    incomplete = directory.get_employee("EMP-TEST-003")
    assert incomplete.team == "" and incomplete.team_lead_id == ""  # never invented


def test_the_directory_scoped_to_a_tenant_reports_another_tenants_employee_as_missing():
    directory = build_mock_employee_directory()
    assert directory.get_employee("EMP-TEST-010", client_id="CLIENT-TEST-B").full_name == "Morgan Lee"
    with pytest.raises(KeyError) as foreign:
        directory.get_employee("EMP-TEST-010", client_id="CLIENT-TEST-A")
    with pytest.raises(KeyError) as missing:
        directory.get_employee("EMP-NOPE", client_id="CLIENT-TEST-A")
    assert foreign.value.args == ("EMP-TEST-010",) and missing.value.args == ("EMP-NOPE",)  # nothing else revealed


def test_directory_records_are_copies_and_reset_restores_them():
    directory = build_mock_employee_directory()
    directory.get_employee("EMP-TEST-001").team = "Tampered"
    assert directory.get_employee("EMP-TEST-001").team == "Platform"
    directory.records.pop("EMP-TEST-001")

    directory.reset()

    assert directory.get_employee("EMP-TEST-001").team == "Platform"


# =========================================================================== notifications

def test_the_notifier_records_messages_to_hr_buddies_team_leads_and_employees():
    notifier = MockNotifier()
    for recipient, kind, channel in [("role:hr_admin", "approval_request", NotificationChannel.TEAMS),
                                     ("TL-001", "approval_request", NotificationChannel.TEAMS),
                                     ("EMP-010", "buddy_assignment", NotificationChannel.EMAIL),
                                     ("EMP-001", "welcome", NotificationChannel.EMAIL)]:
        receipt = notifier.send(message(recipient_id=recipient, kind=kind, channel=channel))
        assert receipt.simulated and receipt.delivery_ref.startswith("sim-dlv-") and receipt.recipient_id == recipient

    assert [m.recipient_id for m in notifier.messages(kind="approval_request")] == ["role:hr_admin", "TL-001"]
    assert [m.kind for m in notifier.messages(channel=NotificationChannel.EMAIL)] == ["buddy_assignment", "welcome"]


def test_idempotency_keys_are_scoped_per_tenant():
    notifier = MockNotifier()
    acme = notifier.send(message(key="op-1"))
    assert notifier.send(message(key="op-1")).delivery_ref == acme.delivery_ref  # same tenant: deduplicated
    globex = notifier.send(message(key="op-1", client_id=GLOBEX))

    assert globex.delivery_ref != acme.delivery_ref  # another tenant's key never returns ACME's receipt
    assert len(notifier.sent_to("EMP-001", client_id=ACME)) == 1
    assert len(notifier.sent_to("EMP-001", client_id=GLOBEX)) == 1


def test_a_notifier_without_idempotency_delivers_a_resend_again():
    notifier = MockNotifier(idempotent_delivery=False)
    notifier.send(message(key="op-1"))
    notifier.send(message(key="op-1"))
    assert len(notifier.sent) == 2


@pytest.mark.parametrize("bad", [dict(recipient_id="EMP-BLOCKED"), dict(recipient_id=" "), dict(client_id="")])
def test_permanent_refusals_deliver_nothing(bad):
    notifier = MockNotifier(reject_recipients={"EMP-BLOCKED"})
    with pytest.raises(NotificationRejectedError):
        notifier.send(message(**bad))
    assert notifier.sent == []


def test_notifier_reset_clears_deliveries_and_remembered_keys():
    notifier = MockNotifier(fail_next=1)
    with pytest.raises(NotificationDeliveryError):
        notifier.send(message(key="op-1"))
    notifier.send(message(key="op-1"))
    notifier.reject_recipients.add("EMP-001")

    notifier.reset()

    assert notifier.sent == [] and notifier.fail_next == 1 and notifier.reject_recipients == set()
    notifier.fail_next = 0
    notifier.send(message(key="op-1"))  # the key is forgotten: delivered afresh
    assert len(notifier.sent) == 1


# =========================================================================== Teams

def test_the_teams_adapter_posts_only_teams_messages_marked_as_simulated():
    teams = MockTeamsAdapter()
    for kind in ("approval_request", "welcome", "team_intro"):
        receipt = teams.send(message(channel=NotificationChannel.TEAMS, recipient_id="role:hr_admin", kind=kind,
                                     address=None))
        assert receipt.simulated and receipt.delivery_ref.startswith("sim-teams-")
    assert [m.kind for m in teams.messages()] == ["approval_request", "welcome", "team_intro"]

    with pytest.raises(NotificationRejectedError, match="only delivers Teams"):
        teams.send(message(channel=NotificationChannel.EMAIL))
    assert len(teams.sent) == 3


def test_teams_duplicate_delivery_and_failure_scenarios():
    teams = MockTeamsAdapter(lose_next_responses=1)
    teams_message = message(channel=NotificationChannel.TEAMS, recipient_id="TL-001", kind="approval_request",
                            key="op-approval-1")
    with pytest.raises(NotificationDeliveryError):
        teams.send(teams_message)
    teams.send(teams_message)
    assert len(teams.sent) == 1  # the resend after the lost response was recognised

    teams.reset()
    teams.fail_next = 1
    with pytest.raises(NotificationDeliveryError):
        teams.send(teams_message)
    assert teams.sent == []


# =========================================================================== the workflow still waits for HR

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
HR = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
           client_id="CLIENT-TEST-A")


def test_the_workflow_confirms_and_invites_only_after_hr_approval_with_the_connector_mocks():
    teams = MockTeamsAdapter()  # approval requests go out on the Teams channel (policy settings)
    deps = fakes.build_fake_dependencies(notifier=teams, clock=lambda: NOW)
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=fakes.offline_llm(), connector=deps)
    agent = OnboardingAgent(ctx)
    ctx.hitl.subscribe(agent.handle_approval_decision)

    agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED, event_id="EVT-001"), actor=HR,
                    instance="EVT-001")

    (hold,) = deps.calendar.list_bookings()
    assert hold.status == BookingStatus.TENTATIVE and deps.calendar.invited == []  # nothing before approval
    (request,) = teams.messages(kind="approval_request")
    item = ctx.hitl.get(request.metadata["approval_id"], client_id="CLIENT-TEST-A")
    assert item.status == Decision.PENDING

    ctx.hitl.decide(item.id, Decision.APPROVED, approver="user:hr-admin-01", approver_roles=["hr_admin"],
                    client_id="CLIENT-TEST-A")

    assert deps.calendar.get_booking(hold.event_ref).status == BookingStatus.CONFIRMED
    assert sorted(a for _, a in deps.calendar.invited) == ["EMP-TEST-001", "TL-TEST-001"]
    assert deps.repository.get_session(item.payload["id"]).status == SessionStatus.CONFIRMED
