"""Builds the wiring UC-01's API needs.

Two adapter sets exist:

  * in-memory  - selected when SUPABASE_URL is empty. Allowed in dev
    only, for tests and explicitly-configured local development.
  * Supabase   - selected when SUPABASE_URL is set. Durable; survives a
    restart. Startup performs a REAL round-trip against the project and
    refuses to start if it fails.

FAIL CLOSED, two ways:
  1. Outside dev, an empty SUPABASE_URL is an error. Production can
     never silently run on memory.
  2. When SUPABASE_URL is set, a failed connection is an error. There is
     no fallback to memory on a connection problem, in any environment.

Not every port has a compatible table in the confirmed Onboarding_Agent
schema. Those are wired to adapters in adapters/supabase/unsupported.py
that raise an actionable error before any write, rather than inventing
columns or fabricating values. See docs/schema/live-schema-comparison.md.
"""

from dataclasses import dataclass
from typing import Any, Optional

from ..adapters.memory import (
    InMemoryApprovalRepository,
    InMemoryAuditLog,
    InMemoryCaseRepository,
    InMemoryDiscrepancyRepository,
    InMemoryDocumentStore,
    InMemoryEmployeeNumberGenerator,
    InMemoryEventPublisher,
    InMemoryFormRepository,
    InMemoryHolidayCalendar,
    InMemoryHrisRecordRepository,
    InMemoryOfferRepository,
    InMemoryOnboardingFormSource,
    InMemoryPendingStartRepository,
    InMemoryPersonDirectory,
    StubHrisGateway,
)
from ..adapters.supabase import (
    PostgrestClient,
    INTERIM_STATUS_MAPPING,
    SupabaseApprovalRepository,
    SupabaseAuditLog,
    SupabaseCaseRepository,
    SupabaseEmployeeNumberGenerator,
    SupabaseLogicalDocumentStore,
    SupabaseDiscrepancyRepository,
    SupabaseEventPublisher,
    SupabaseFormRepository,
    SupabaseHolidayCalendar,
    SupabaseHrisRecordRepository,
    SupabaseOnboardingFormSource,
    SupabasePendingStartRepository,
    SupabasePersonDirectory,
    UnsupportedApprovalRepository,
    UnsupportedAuditLog,
    UnsupportedCaseRepository,
    UnsupportedDocumentStore,
    UnsupportedEmployeeNumberGenerator,
    UnsupportedOfferRepository,
)
from ..adapters.supabase.client import SupabaseStorage
from ..adapters.supabase.unsupported import UnsupportedFormDetailsRepository
from ..crypto import FieldCipher, load_key, parse_previous_keys
from ..documents.persistence import FormDetailsRepository, InMemoryFormDetailsRepository
from ..documents.supabase_store import SupabaseFormDetailsRepository, SupabaseSourceDocumentStore
from ..deps import UC01Deps
from ..documents.service import DocumentIntakeService
from ..models import CasePlacement
from ..llm import build_uc01_llm, load_all_tasks, load_persona
from ..intake.email_intake import IntakeDeps
from ..onboarding_entry import OnboardingDeps
from ..policy import Policy, load_policy
from ..service import CaseService
from .settings import Settings

_PLACEHOLDER_SECRET = "change-me"

#: Supabase ports whose live adapter may be used. EMPTY on purpose: every
#: UC-01 flow audits first, and the audit store, atomic writes and the
#: employee-number function are undecided (B2, B6, B7), so wiring any of
#: these alone would either never complete a flow or consume live counter
#: values for flows that then fail. When a decision is signed off, its port
#: is added here - nothing else changes. Possible entries:
#:   "approvals"         -> SupabaseApprovalRepository (uc01_approvals)
#:   "employee_numbers"  -> SupabaseEmployeeNumberGenerator (rpc/next_employee_no)
#: The case, audit, offer and document ports have no live adapter wired yet.
APPROVED_SUPABASE_PORTS: frozenset[str] = frozenset()

#: Ports UC01_SUPABASE_INTERIM_WRITES=1 turns on (dev only). The offers port
#: is NOT among them: its live table needs values UC-01 does not have (B3),
#: so the email-intake path stays refused in Supabase mode.
#:   "form_details"      -> SupabaseFormDetailsRepository (the five uc01_onboarding_*
#:                          tables of the PROPOSED migration 20261105120000; until
#:                          it is applied, ensure_ready() refuses BEFORE any case
#:                          is written)
#:   "source_documents"  -> SupabaseSourceDocumentStore (Storage; needs
#:                          UC01_DOCUMENT_BUCKET, otherwise the file is not stored)
#: The source file's metadata is stored on the details row, not in the shared
#: candidate_documents table (pending decision; see docs/uc01-budget-and-persistence.md).
INTERIM_SUPABASE_PORTS: frozenset[str] = frozenset(
    {"cases", "audit", "approvals", "employee_numbers", "documents", "form_details", "source_documents"})


@dataclass
class Container:
    settings: Settings
    deps: UC01Deps
    onboarding_deps: OnboardingDeps
    intake_deps: IntakeDeps
    service: CaseService
    policy: Policy
    # Set only when Supabase persistence is active; held so callers can
    # close the HTTP connection pool on shutdown.
    postgrest: Optional[PostgrestClient] = None
    # Onboarding-document intake over the same deps and advisory model.
    documents: Optional[DocumentIntakeService] = None
    # Where the document's details and budget are stored (in-memory in dev).
    form_details: Optional[FormDetailsRepository] = None
    # Supabase Storage client, when a document bucket is configured.
    storage: Optional[SupabaseStorage] = None


def build_container(
    settings: Settings,
    *,
    postgrest: Optional[PostgrestClient] = None,
    llm_gateway: Optional[Any] = None,
) -> Container:
    """Wire UC-01.

    `postgrest` is an injection point for tests: pass a client built on a
    stub transport to exercise the Supabase adapters without a network.
    When None and Supabase is configured, a real client is built and
    pinged.

    `llm_gateway` injects an `hr_onboarding_common.llm` client (a
    `LiteLLMGateway` over a stand-in transport, or a common `FakeLLM`).
    Used only when UC01_LLM_ENABLED is set; otherwise LLM_MODE decides.
    """
    use_supabase = settings.persistence_enabled or postgrest is not None

    if not use_supabase:
        if settings.app_env != "dev":
            raise RuntimeError(
                f"APP_ENV={settings.app_env!r} requires durable persistence, but "
                "SUPABASE_URL is empty. UC-01 will not start on in-memory "
                "adapters outside dev: data would be lost on every restart. "
                "Set SUPABASE_URL (and SUPABASE_SERVICE_ROLE_KEY, "
                "UC01_FIELD_ENCRYPTION_KEY) in the repository-root .env."
            )

    if settings.app_env != "dev":
        if not settings.dedup_hmac_key or settings.dedup_hmac_key == _PLACEHOLDER_SECRET:
            raise RuntimeError("DEDUP_HMAC_KEY must be set to a real secret outside dev")
        if not settings.system_api_key or settings.system_api_key == _PLACEHOLDER_SECRET:
            raise RuntimeError("UC01_SYSTEM_API_KEY must be set to a real secret outside dev")

    hris_gateway = StubHrisGateway()

    if use_supabase:
        client = postgrest or PostgrestClient(
            settings.supabase_url,
            settings.supabase_service_role_key,
            schema=settings.supabase_schema,
            timeout=settings.supabase_timeout_s,
        )
        # NOTE: no connection test here. Building a container must stay
        # free of network I/O so that merely importing the app (which the
        # test suite does) cannot reach the live project. The real
        # round-trip runs in the ASGI startup hook in create_app(), i.e.
        # when a server actually starts. Nothing reports "connected"
        # before that ping succeeds, and a failed ping aborts startup
        # rather than degrading to memory.
        cipher = FieldCipher(load_key(settings.field_encryption_key.get_secret_value()),
                             key_id=settings.field_encryption_key_id or None,
                             previous=parse_previous_keys(settings.field_encryption_previous_keys.get_secret_value()))

        form_repo = SupabaseFormRepository(client, cipher)
        person_directory = SupabasePersonDirectory(client)
        discrepancy_repo = SupabaseDiscrepancyRepository(client)
        hris_record_repo = SupabaseHrisRecordRepository(client)
        publisher = SupabaseEventPublisher(client)
        holiday_calendar = SupabaseHolidayCalendar(client)
        pending_start_repo = SupabasePendingStartRepository(client)
        form_source = SupabaseOnboardingFormSource(client, form_repo)
        # Undecided or unimplemented ports refuse loudly instead of
        # inventing a table or falling back to memory. A port with a live
        # adapter is used only once it is listed in APPROVED_SUPABASE_PORTS,
        # or - interim, dev only - in INTERIM_SUPABASE_PORTS with
        # UC01_SUPABASE_INTERIM_WRITES=1.
        ports = set(APPROVED_SUPABASE_PORTS)
        if settings.supabase_interim_writes:
            if settings.app_env != "dev":
                raise RuntimeError(
                    "UC01_SUPABASE_INTERIM_WRITES is a development-only interim design (pending DBA "
                    "decisions B2/B5/B6/B7) and is refused outside APP_ENV=dev")
            ports |= INTERIM_SUPABASE_PORTS
        case_repo = (SupabaseCaseRepository(client, status_mapping=INTERIM_STATUS_MAPPING)
                     if "cases" in ports else UnsupportedCaseRepository())
        approval_repo = (SupabaseApprovalRepository(client) if "approvals" in ports
                         else UnsupportedApprovalRepository())
        if "audit" in ports:
            def _case_client(case_id: str):
                case = case_repo.get(case_id)
                return case.client_id if case is not None else None

            audit_log = SupabaseAuditLog(client, case_client=_case_client)
        else:
            audit_log = UnsupportedAuditLog()
        document_store = (SupabaseLogicalDocumentStore() if "documents" in ports
                          else UnsupportedDocumentStore())
        number_generator = (SupabaseEmployeeNumberGenerator(client) if "employee_numbers" in ports
                            else UnsupportedEmployeeNumberGenerator())
        offer_repo = UnsupportedOfferRepository()
        form_details: FormDetailsRepository = (
            SupabaseFormDetailsRepository(client, cipher) if "form_details" in ports
            else UnsupportedFormDetailsRepository())
        storage = (SupabaseStorage(settings.supabase_url, settings.supabase_service_role_key,
                                   timeout=settings.supabase_timeout_s)
                   if "source_documents" in ports and settings.document_bucket and postgrest is None else None)
        source_store = SupabaseSourceDocumentStore(storage, settings.document_bucket) if storage else None
    else:
        client = None
        case_repo = InMemoryCaseRepository()
        form_repo = InMemoryFormRepository()
        approval_repo = InMemoryApprovalRepository()
        audit_log = InMemoryAuditLog()
        publisher = InMemoryEventPublisher()
        discrepancy_repo = InMemoryDiscrepancyRepository()
        hris_record_repo = InMemoryHrisRecordRepository()
        holiday_calendar = InMemoryHolidayCalendar()
        person_directory = InMemoryPersonDirectory()
        document_store = InMemoryDocumentStore()
        number_generator = InMemoryEmployeeNumberGenerator()
        offer_repo = InMemoryOfferRepository()
        form_source = InMemoryOnboardingFormSource()
        pending_start_repo = InMemoryPendingStartRepository()
        form_details = InMemoryFormDetailsRepository()
        # No durable file store in memory mode: source_document_stored stays False.
        storage, source_store = None, None

    policy = load_policy()

    deps = UC01Deps(
        case_repo=case_repo,
        form_repo=form_repo,
        approval_repo=approval_repo,
        discrepancy_repo=discrepancy_repo,
        hris_record_repo=hris_record_repo,
        hris_gateway=hris_gateway,
        audit_log=audit_log,
        publisher=publisher,
        holiday_calendar=holiday_calendar,
        hris_writes_enabled=lambda: settings.hris_writes_enabled,
        policy=policy,
    )
    # Advisory only, and off unless UC01_LLM_ENABLED is set: every flow
    # must work without a model, and the eval suite runs with it off so
    # results stay comparable. When on, LLM_MODE picks the deterministic
    # FakeLlm (offline) or the central gateway (online); see
    # llm/gateway_client.py.
    llm = (
        build_uc01_llm(
            load_all_tasks(),
            load_persona(),
            gateway=llm_gateway,
            environ={"LLM_MODE": settings.llm_mode},
        )
        if settings.llm_enabled
        else None
    )

    onboarding_deps = OnboardingDeps(
        case_repo=case_repo,
        form_repo=form_repo,
        approval_repo=approval_repo,
        person_directory=person_directory,
        document_store=document_store,
        number_generator=number_generator,
        event_publisher=publisher,
        audit_log=audit_log,
        llm=llm,
    )

    intake_deps = IntakeDeps(
        offer_repo=offer_repo,
        form_source=form_source,
        pending_repo=pending_start_repo,
        onboarding_deps=onboarding_deps,
        llm=llm,
        # location_name is a REQUIRED caller-supplied value (approved
        # 2026-11-04), and nothing in an offer or an onboarding form
        # carries it. The [Proposed] email-intake simulator therefore
        # has no source for it.
        #
        # In dev, supply a clearly synthetic stand-in so the simulator
        # still runs end to end - the same scaffolding role as
        # StubHrisGateway and the InMemory* adapters next to it.
        #
        # OUTSIDE DEV THIS STAYS None, and try_start parks the offer as
        # WAITING with reason "location_name_required" rather than
        # inventing a location on a real employee record. The real
        # trigger must carry the value end to end (OQ-01/Q-11).
        placement=(
            CasePlacement(location_name="Dev Simulator Office")
            if settings.app_env == "dev"
            else None
        ),
    )

    fingerprint_key = (settings.dedup_hmac_key or "dev-only-fingerprint-key").encode("utf-8")
    service = CaseService(deps, fingerprint_key, policy, llm=llm)

    return Container(
        documents=DocumentIntakeService(onboarding_deps, llm=llm, details_repo=form_details,
                                        source_store=source_store),
        form_details=form_details,
        storage=storage,
        postgrest=client,
        settings=settings,
        deps=deps,
        onboarding_deps=onboarding_deps,
        intake_deps=intake_deps,
        service=service,
        policy=policy,
    )
