from .models import ActorType, AuditEntry, Case, CaseOpenedEvent
from .ports import AuditLog, EventPublisher


def publish_case_opened(case: Case, publisher: EventPublisher, audit_log: AuditLog) -> CaseOpenedEvent:
    event = CaseOpenedEvent(
        case_id=case.case_id,
        client_id=case.client_id,
        form_version=case.current_form_version,
        joining_date=case.joining_date,
        work_location_type=case.work_location_type,
        variants=case.variants,
    )
    publisher.publish(event)

    audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-case-events",
            action="CASE_OPENED_EVENT_PUBLISHED",
            after={"event_id": event.event_id},
            reference=case.case_id,
        )
    )

    return event
