/**
 * Typed wrappers around the orchestrator API (HR-Onboarding-Common/src/hr_onboarding_common/orchestration/api.py).
 * Base: /v1/orchestration. The backend's orchestrator owns every transition; nothing here decides one.
 */
import apiClient from './apiClient'

const BASE = '/v1/orchestration'

// ── Types (mirroring api.py response models) ─────────────────────────────────────

export type Json = Record<string, unknown>

export interface StageOutcome {
  stage: string
  uc: string
  operation?: string
  status: string
  attempt?: number
  started_at?: string
  finished_at?: string
  inputs?: Json
  result_key?: string
  next_action?: string | null
  error?: { code: string; detail: string; retryable: boolean } | null
  data_origin?: string
  canonical_status?: string
  execution_id?: string
  trace_id?: string
  correlation_id?: string
  dependencies?: Json[]
  warnings?: string[]
  updated_fields?: string[]
  /** Compact persisted result (ids, versions, counts, codes); set for UC02's plan_tasks / replan_tasks. */
  result_summary?: Json | null
  /** The stage's typed handoff (UC01Output .. UC05Output); null for orchestrator-internal stages. */
  handoff?: StageHandoff | null
}

export interface StageHandoff {
  contract: 'UC01Output' | 'UC02Output' | 'UC03Output' | 'UC04Output' | 'UC05Output'
  contract_version: string
  case_id: string
  client_id: string
  stage: string
  status: string
  result: Record<string, unknown>
  warnings: string[]
  blockers: Array<{ code: string; reason?: string; owner?: string; next_action?: string; required_table?: string;
    required_record?: string; required_configuration?: string }>
  approval_requirements: Array<{ reference: string; gate?: string; role?: string; proposal_type?: string;
    record_id?: string }>
  audit_reference: string | null
  timestamp: string
  version: number
}

export interface PendingAction {
  action: string
  stage: string
  owner?: string
  reference?: string | null
  agent?: string | null
  documents?: string[]
  reason?: string
  [k: string]: unknown
}

export interface Blocker {
  stage: string
  code: string
  reason: string
  owner?: string
  next_action?: string
  // Structured dependency fields (backend orchestration/dependency_blockers.py), for known codes:
  required_from?: string
  required_action?: string
  required_data?: string
  dependency?: string
  resolution?: 'external' | 'provide_equipment_access_requests' | 'provide_offer_event' | string
  retryable?: boolean
  // NO_ITEMS_REQUESTED only: UC04's advisory skill recommend_role_standard_items (the role's standard package from its
  // configuration). A suggestion for HR to review; nothing is requested until HR records it.
  recommendation?: RolePackageRecommendation
  // retry_apply_decision: the decided approval whose follow-up is re-run (nothing is decided again).
  approval_id?: string
}

export interface RolePackageRecommendation {
  skill: string
  skill_version?: string
  source: 'uc04'
  role_code: string
  config_version?: string
  equipment: string[] | null  // null: no recommendation (the role's list is missing in its configuration)
  access: string[] | null
  gaps: string[]
  advisory_only: true
  note?: string
}

export interface PendingApproval {
  id?: string
  reference: string
  agent?: string
  role?: string
  description?: string
  stage?: string
  gate?: string
  // The one person UC05 lets decide it (the team lead / buddy on record); absent for a role-wide approval.
  approver_id?: string
  proposal_type?: string  // the owning agent's kind of approval (session_booking, uc04_item_approval, ...)
  record_id?: string
  raised_at?: string  // when the orchestrator first surfaced it
}

export interface RequiredDocument {
  document_type: string
  label?: string
  required?: boolean
  due_date?: string | null
  reason?: string
}

export interface DocumentValidation {
  document_status?: string
  compliance_item_status?: string
  hr_decision?: string
  evidence_status?: string
  findings?: string[]
  required_next_action?: string
  audit_reference?: string
  detail?: string
  revision?: number
  review_decision?: string | null
  classification?: string
  plan_version?: number
}

export interface CaseView {
  case_id: string
  client_id: string
  employee_id?: string | null  // UC01's employee_no (compatibility); prefer the two named fields below
  hrms_employee_no?: string | null  // the canonical HRMS number every UC05 call uses
  uc01_employee_no?: string | null  // UC01's own number (in development it repeats across restarts)
  joining_date?: string | null
  joining_date_revision?: number | null
  joining_date_history: Json[]
  joining_date_consistency?: Json | null
  current_stage: string
  overall_status: string
  workflow_status?: string | null
  data_origin: string
  stage_statuses: Record<string, string>
  branch_statuses: Record<string, { status: string; stages: Record<string, string>; next_action?: string | null; error?: unknown }>
  completed_stages: string[]
  pending_actions: PendingAction[]
  document_collection?: {
    required_documents: RequiredDocument[]
    // Each outstanding document as the backend stores it: {document_type, reason} (older states: a bare type).
    outstanding: Array<string | { document_type: string; reason?: string }>
    requested_at?: string
    source?: string
    plan_id?: string
    plan_version?: number
    notification?: Json | null
  } | null
  documents: Record<string, { document_ref?: string; source?: string; revision?: number; submitted_at?: string;
    hr_review?: { decision?: string; revision?: number; reviewed_at?: string } | null }>
  document_validation: Record<string, DocumentValidation>
  validation_issues: Json[]
  correction_requirements: Array<{ document_type: string; revision?: number; action: string; reason_codes: string[]; detail?: string }>
  verified_context?: Json | null
  task_plan?: Json | null
  onboarding_plan?: Json | null
  plan_history: Json[]
  revised_plan?: Json | null
  document_validation_feedback?: Json | null
  requirement_change_requests: Json[]
  equipment_access?: Json | null
  stage_outcomes: Record<string, StageOutcome>
  retry_metadata: Record<string, Json>
  created_at?: string | null
  updated_at?: string | null
  next_action?: string | null
  error_code?: string | null
  stage_errors: Record<string, { code: string; detail: string; retryable: boolean }>
  blockers: Blocker[]
  pending_approvals: PendingApproval[]
  approval_decisions: Json[]
  employee_ref?: Json | null
  participant_responses: Json[]
  outstanding_tasks: string[]
  orientation?: Json | null
  buddy_assignment?: Json | null
  team_introduction?: Json | null
  buddy_introduction?: Json | null
  welcome_message?: Json | null
  joining_day_event?: Json | null
  stage_history: Array<{ stage: string; status: string; at: string; [k: string]: unknown }>
  form_approval?: FormApproval | null
  form_extraction?: FormExtractionSummary | null
  equipment_access_requests?: { requested_equipment: string[]; requested_access: string[]; recorded_by?: string;
    recorded_at?: string } | null
  approval_persistence: Record<string, { persisted: boolean; reason: string | null; outcome: string; table: string; at: string }>
  state_version?: number | null
  field_provenance: Record<string, Json>
}

/** The internal onboarding form's approval request (orchestrator gate onboarding_form_approval). */
export interface FormApproval {
  required: boolean
  approval_type?: string
  gate?: string
  status?: 'not_requested' | 'pending' | 'approved' | 'rejected'
  approval_id?: string | null
  requested_at?: string | null
  required_approver_role?: string
  separation_of_duties?: boolean
  assignment?: { mode: string; assigned_approver_id: string | null; status: string; detail: string }
  decision?: { decision: 'approved' | 'rejected'; decided_by?: string; decided_by_role: string; decided_at: string;
    reason_code?: string | null; note?: string | null } | null
  persistence?: { persisted: boolean; reason: string | null; outcome: string; table: string; at: string } | null
  // Only from GET .../form-approval, for case_identity_reader_roles:
  requested_by?: string
  employee_name?: string | null
}

export interface CaseProfile {
  candidate_name?: string | null
  offer_reference?: string | null
  designation?: string | null
  work_location_type?: string | null
  role_id?: string | null
  department?: string | null
  grade?: string | null
  manager_id?: string | null
  location?: string | null
  project?: string | null
  nationality_group?: string | null
  timezone?: string | null
  requested_by?: string | null
}

export interface CaseSummary {
  case_id: string
  client_id: string
  employee_id?: string | null
  hrms_employee_no?: string | null
  uc01_employee_no?: string | null
  joining_date?: string | null
  current_stage: string
  overall_status: string
  workflow_status?: string | null
  data_origin: string
  stage_statuses: Record<string, string>
  completed_stages: string[]
  pending_action_count: number
  blocker_count: number
  next_action?: string | null
  created_at?: string | null
  updated_at?: string | null
  form_approval_status?: string | null
  profile: CaseProfile
}

export interface StageExecution {
  stage: string
  use_case_id: string
  operation?: string
  status: string
  stage_status: string
  attempts?: number
  last_execution?: Json | null
  dependencies?: Array<{ key: string; upstream_stage: string; use_case_id: string; kind: string; satisfied: boolean;
    code?: string | null; detail?: string }>
  error?: { code: string; detail: string; retryable?: boolean; category?: string } | null
  warnings?: string[]
  updated_fields?: string[]
  next_action?: string | null
  retry?: Json | null
  pending_approvals?: Json[]
}

export interface HealthView {
  status: string
  agent: string
  version: string
  enabled: boolean
  mode?: 'real' | 'demo'
  data_origin?: string
  state_store?: string
  /** Where UC02 keeps its tasks, dependencies and plan revisions: supabase | sqlite | memory. */
  uc02_store?: string
  /** Whether UC01 stores its cases in onboarding_cases: supabase | memory. */
  uc01_store?: string
  /** Where UC03 / UC04 (state, configuration) / UC05 / the joining-day jobs keep their records. */
  stores?: Record<string, string>
  identity?: string
  uc04_identity_profile?: string
  uc05_offer_event_source?: string
  approval_requests?: string
  form_approval_role?: string
  document_storage: {
    configured: boolean
    bucket?: string
    max_bytes?: number
    allowed_types?: string[]
    extraction?: { available: boolean; engine: string; reason: string | null }
  }
}

/** The start request (api.py StartCaseRequest). client_id and the HR officer come from the verified identity. */
export interface StartCaseRequest {
  offer_accepted: boolean
  offer_reference: string
  offer_acceptance_event_id?: string
  candidate_name: string
  candidate_email: string
  designation: string
  joining_date: string
  work_location_type?: 'OFFICE' | 'CLIENT_SITE'
  id_number?: string
  role_id?: string
  department?: string
  manager_id?: string
  location: string
  project?: string
  grade?: string
  nationality_group?: string
  /** Checklist attribute codes HR states for UC02 (e.g. visa_route:in_country_transfer); never inferred. */
  attributes?: string[]
  offer_accepted_at?: string
  // No form_approved_at / form_approved_by_role: a person's form is never approved by submitting it.
  employee_id?: string
  timezone?: string
  // The onboarding-form extraction this form was prefilled from; the backend records its own summary of it.
  source_extraction_id?: string
  // HR confirms having checked the fields UC01 flagged (required when it flagged any).
  extraction_reviewed?: boolean
}

/** One field of UC01's safe extraction view: status always; the value only for non-sensitive fields. */
export interface ExtractedFieldView {
  status: 'PRESENT' | 'MISSING' | 'INVALID' | 'UNCERTAIN'
  source?: string | null
  value?: string | number | boolean | null
  note?: string
}

/** POST /onboarding-documents/extract: UC01's extraction of an onboarding form, for HR to review. */
export interface OnboardingDocumentExtraction {
  extraction_id: string
  status: 'READY_FOR_REVIEW' | 'VALIDATION_REQUIRED'
  uc01_status: string
  source_document: { sha256: string; size_bytes: number; template_id: string; template_recognised: boolean;
    extracted_at: string; filename: string; content_type: string }
  extraction: {
    template_id: string; template_recognised: boolean; completeness: number
    fields: Record<string, ExtractedFieldView>
    supplementary: Record<string, ExtractedFieldView>
    responsibilities: { items: string[]; is_example: boolean; source?: string | null }
    synthetic_document: boolean
  }
  prefill: Partial<Record<'candidate_name' | 'designation' | 'joining_date' | 'candidate_email' | 'department' |
    'grade' | 'work_location_type' | 'location', string>>
  prefill_sources: Record<string, string>
  reference: Record<string, { status: string; value?: string | number | null; note?: string }>
  missing_fields: string[]
  invalid_fields: string[]
  uncertain_fields: string[]
  validation_errors: string[]
  form_inputs_required: string[]
  review_reasons: string[]
  outstanding_documents: string[]
  completeness: number
  review_required: boolean
}

/** A refused document: FAILED with UC01's (or the upload check's) code, the reason and what HR can do. */
export interface ExtractionFailure {
  status: 'FAILED'
  error: { code: string; reason: string; action: string }
}

/** What a case records about the document its form was prefilled from: statuses and field names, no values. */
export interface FormExtractionSummary {
  extraction_id: string
  status: string
  extracted_at: string
  source_document: { sha256: string; size_bytes: number; template_id: string; template_recognised: boolean;
    content_type: string }
  completeness: number
  missing_fields: string[]
  invalid_fields: string[]
  uncertain_fields: string[]
  validation_errors: string[]
  review_reasons: string[]
  outstanding_documents: string[]
  prefill_sources: Record<string, string>
  fields_changed_by_hr: string[]
  fields_entered_by_hr: string[]
  reviewed: boolean
  extracted_by?: string | null
  reviewed_by?: string | null
}

export interface UploadReceipt {
  document_id: string
  document_type: string
  document_ref: string
  bucket: string
  storage_path: string
  storage_status: 'created' | 'exists'
  file_name: string
  mime_type: string
  size_bytes: number
  checksum_sha256: string
  metadata_persisted: boolean
  metadata_reason: string | null
  extraction: {
    method: string
    engine_available: boolean
    engine_issue: string | null
    readable: boolean
    required_fields_present: boolean | null
    expiry_date: string | null
    name_found: boolean
    confidence: number
    injection_suspected: boolean
  }
  uploaded_at: string
}

// ── Reads ──────────────────────────────────────────────────────────────────────

export async function healthCheck(): Promise<HealthView> {
  const { data } = await apiClient.get<HealthView>('/healthz', { timeout: 8_000 })
  return data
}

export async function listCases(): Promise<CaseSummary[]> {
  const { data } = await apiClient.get<{ client_id: string; cases: CaseSummary[] }>(`${BASE}/cases`)
  return data.cases
}

export async function getCase(caseId: string): Promise<CaseView> {
  const { data } = await apiClient.get<CaseView>(`${BASE}/cases/${encodeURIComponent(caseId)}`)
  return data
}

export async function getProfile(caseId: string): Promise<CaseProfile> {
  const { data } = await apiClient.get<{ profile: CaseProfile }>(`${BASE}/cases/${encodeURIComponent(caseId)}/profile`)
  return data.profile
}

/** One pending approval of the tenant's cases (GET /approvals), with whether the current user may decide it. */
export interface ApprovalInboxItem {
  case_id: string
  approval_id: string
  agent?: string
  gate?: string | null
  stage?: string | null
  required_role?: string
  approver_id?: string | null
  approval_type: string
  description?: string
  // What a UC04 item approval is for (null for other approvals: their description says it).
  request?: { item_id?: string | null; item_kind?: 'EQUIPMENT' | 'ACCESS' | null; item_state?: string | null;
              approval_type?: string; request_id?: string } | null
  raised_by?: string | null  // the agent that raised it (Orchestrator / UC04 / UC05 / UC02)
  requested_at?: string | null
  requested_by?: string | null  // identity-gated
  employee_name?: string | null  // identity-gated
  employee_id?: string | null
  hrms_employee_no?: string | null
  uc01_employee_no?: string | null
  designation?: string | null
  department?: string | null
  joining_date?: string | null
  can_decide: boolean
  reason?: string | null
}

export async function listApprovals(): Promise<ApprovalInboxItem[]> {
  const { data } = await apiClient.get<{ approvals: ApprovalInboxItem[] }>(`${BASE}/approvals`)
  return data.approvals
}

export async function recordEquipmentAccessRequests(caseId: string, requestedEquipment: string[],
  requestedAccess: string[]): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/equipment-access/requests`,
    { requested_equipment: requestedEquipment, requested_access: requestedAccess })
  return data
}

/** HR changes the current plan's document requirements (UC03 reports it, UC02 replans a new plan version). */
export async function changeDocumentRequirements(caseId: string, change: { add_documents?: string[];
  remove_documents?: string[]; reason: string }): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(`${BASE}/cases/${encodeURIComponent(caseId)}/documents/requirements`,
    { add_documents: change.add_documents ?? [], remove_documents: change.remove_documents ?? [], reason: change.reason })
  return data
}

export async function recordOfferEvent(caseId: string, eventId: string): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(`${BASE}/cases/${encodeURIComponent(caseId)}/orientation/offer-event`,
    { offer_acceptance_event_id: eventId })
  return data
}

/**
 * HR restarts UC05's part of a case after UC05 lost its records (a worker restart without persistence). The backend
 * allows it only when a UC05 stage is uncertain and UC05 confirms it holds nothing; the induction is approved again.
 */
export async function restartUc05AfterRecordsLost(caseId: string, reason: string): Promise<CaseView> {
  const { data } = await apiClient.post<{ case: CaseView }>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/orientation/operations`,
    { operation: 'restart_after_records_lost', reason })
  return data.case
}

/** The joining-day welcome: UC05's draft (written on the joining day) or a preview from the client's template. */
export interface WelcomeMessageView {
  case_id: string
  status: 'draft' | 'preview' | 'no_welcome_configuration' | 'no_uc05_case' | 'uc05_unavailable' | 'uc05_error' | string
  joining_date?: string | null
  joining_day_event?: boolean
  draft_status?: string | null
  delivery_status?: string | null
  subject?: string
  body?: string
  language?: string
  personal_note?: string
  detail?: string
  source?: string
}

export async function getWelcomeMessage(caseId: string): Promise<WelcomeMessageView> {
  const { data } = await apiClient.get<WelcomeMessageView>(`${BASE}/cases/${encodeURIComponent(caseId)}/welcome-message`)
  return data
}

/** Re-read UC05's session, participant and buddy details into the case's UC05 results (display only). */
export async function refreshUc05Details(caseId: string): Promise<CaseView> {
  const { data } = await apiClient.post<{ case: CaseView }>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/orientation/refresh-details`)
  return data.case
}

export interface DevIdentityEntry { user_id: string; label: string; roles: string[]; clients: string[] }

/** The backend's development identity registry (404 when the backend trusts headers instead). */
export async function getDevIdentities(): Promise<DevIdentityEntry[] | null> {
  try {
    const { data } = await apiClient.get<{ identities: DevIdentityEntry[] }>(`${BASE}/dev/identities`)
    return data.identities
  } catch {
    return null
  }
}

export async function getFormApproval(caseId: string): Promise<FormApproval | null> {
  const { data } = await apiClient.get<{ form_approval: FormApproval | null }>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/form-approval`)
  return data.form_approval
}

export async function getStageExecutions(caseId: string): Promise<Record<string, StageExecution>> {
  const { data } = await apiClient.get<{ stages: Record<string, StageExecution> }>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/stages`)
  return data.stages
}

export async function getDocumentUrl(caseId: string, documentType: string): Promise<string> {
  const { data } = await apiClient.get<{ url: string }>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(documentType)}/url`)
  return data.url
}

// ── Commands (each one a real backend operation) ─────────────────────────────────

/** UC01 extracts and validates an onboarding form (.docx / text-based .pdf). Nothing is stored and no case is
 *  opened. A refused file rejects with the backend's FAILED body (see extractionFailure). */
export async function extractOnboardingDocument(file: File): Promise<OnboardingDocumentExtraction> {
  const { data } = await apiClient.post<OnboardingDocumentExtraction>(`${BASE}/onboarding-documents/extract`, file, {
    headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-File-Name': encodeURIComponent(file.name) },
    timeout: 90_000,  // a PDF is read page by page by UC01
  })
  return data
}

/** The FAILED body of a refused extraction (code, reason, action), or null for any other error. */
export function extractionFailure(err: unknown): ExtractionFailure | null {
  const body = (err as { response?: { data?: unknown } })?.response?.data as Partial<ExtractionFailure> | undefined
  return body && body.status === 'FAILED' && body.error?.code ? (body as ExtractionFailure) : null
}

export async function startCase(body: StartCaseRequest): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(`${BASE}/cases`, body)
  return data
}

export async function resumeCase(caseId: string): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(`${BASE}/cases/${encodeURIComponent(caseId)}/resume`)
  return data
}

/** Records the joining-day event (normally sent by the joining-day scheduler service; HR may also record it). */
export async function recordJoiningDay(caseId: string, asOf: string): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(`${BASE}/cases/${encodeURIComponent(caseId)}/joining-day`, { as_of: asOf })
  return data
}

export async function retryStage(caseId: string, stage: string): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/stages/${encodeURIComponent(stage)}/retry`)
  return data
}

export async function decideApproval(caseId: string, approvalId: string, decision: 'approved' | 'rejected',
  note?: string): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/approvals/${encodeURIComponent(approvalId)}/decision`,
    { decision, ...(note ? { note } : {}) })
  return data
}

export async function retryApproval(caseId: string, approvalId: string): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/approvals/${encodeURIComponent(approvalId)}/retry-apply`)
  return data
}

export async function reviewDocuments(caseId: string,
  reviews: Array<{ document_type: string; decision: 'approved' | 'rejected'; note?: string }>): Promise<CaseView> {
  const { data } = await apiClient.post<CaseView>(`${BASE}/cases/${encodeURIComponent(caseId)}/documents/reviews`,
    { reviews })
  return data
}

export async function uploadDocument(caseId: string, documentType: string, file: File):
  Promise<{ case: CaseView; upload: UploadReceipt }> {
  const { data } = await apiClient.post<{ case: CaseView; upload: UploadReceipt }>(
    `${BASE}/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(documentType)}/upload`,
    file,
    { headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-File-Name': encodeURIComponent(file.name) } },
  )
  return data
}
