/**
 * Right sidebar for one stage: what the orchestrator persisted for it (stage_outcomes, the stage's
 * result field, its history, errors, blockers and approvals). Actions are shown only where a backend
 * route exists, and the backend decides whether each is allowed.
 */
import { useState } from 'react'
import { Loader, RefreshCcw, RotateCcw, X } from 'lucide-react'
import { apiErrorMessage } from '../api/apiClient'
import { decideApproval, recordJoiningDay, refreshUc05Details, resumeCase, retryApproval, retryStage } from '../api/onboardingApi'
import { allowed, usePermissions } from '../api/permissions'
import type { CaseProfile, CaseView, HealthView, StageExecution } from '../api/onboardingApi'
import { STAGES, TONE_BADGE, awaitingFormApproval, fmtDuration, fmtTime, humanize, stageOutput,
  stageStatusView } from '../lib/stages'
import BlockerCard from './BlockerCard'
import WelcomeMessage from './WelcomeMessage'
import { approvalLabel, humanizeCode, looksLikePlaceholder, personRoleLabel, sessionTime, stateLabel } from '../lib/labels'
import DocumentChecklist from './DocumentChecklist'
import FormApprovalPanel, { SourceDocumentSummary } from './FormApprovalPanel'
import ExpandableJSON from './ExpandableJSON'

interface Props {
  stage: string
  view: CaseView
  profile: CaseProfile | null
  execution: StageExecution | null
  health: HealthView | null
  onApply: (view: CaseView) => void
  onClose: () => void
}

// orchestration/state.py TECHNICAL_RETRY_CODES: a BLOCKED stage with one of these is retryable.
const TECHNICAL_RETRY_CODES = new Set(['UC05_PROVIDER_UNAVAILABLE', 'UC05_UNAVAILABLE', 'DECISION_NOT_APPLIED'])
const UC03_STAGES = new Set(['document_collection', 'document_submission', 'validate_documents'])
const UC05_STAGES = new Set(['orientation_scheduling', 'buddy_assignment', 'team_introduction', 'buddy_introduction',
  'joining_day_welcome'])

/** Demo mode only: records the joining-day event that the joining-day scheduler service sends in a real deployment
 * (route POST .../joining-day; joining_day_trigger_roles), so the walk-through can reach "Onboarding Complete". */
function SimulateJoiningDay({ caseId, joiningDate, onApply }: { caseId: string; joiningDate: string;
  onApply: (v: CaseView) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function go() {
    setBusy(true); setError(null)
    try { onApply(await recordJoiningDay(caseId, joiningDate)) } catch (e) { setError(apiErrorMessage(e)) } finally { setBusy(false) }
  }
  return (
    <div className="mt-2 rounded border border-indigo-200 bg-indigo-50 p-2.5 text-[11px] text-slate-700">
      <p>Demo mode: the joining-day scheduler service is not running here. Record the joining day
        ({joiningDate}) to run UC05's welcome.</p>
      <button disabled={busy} onClick={() => void go()}
        className="mt-1.5 inline-flex items-center gap-1 rounded bg-indigo-600 px-2.5 py-1 text-white hover:bg-indigo-700 disabled:opacity-50">
        {busy && <Loader className="w-3 h-3 animate-spin" />} Simulate joining day
      </button>
      {error && <p className="mt-1 text-red-600">{error}</p>}
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="px-4 py-3 border-b border-slate-100">
      <h4 className="text-xs font-bold text-slate-500 uppercase tracking-wide mb-2">{title}</h4>
      {children}
    </div>
  )
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="mb-1.5">
      <span className="text-xs text-slate-400 block">{label}</span>
      <span className="text-xs font-medium text-slate-800 break-words">{value === null || value === undefined || value === '' ? '—' : value}</span>
    </div>
  )
}

/** The stage an approval belongs to: its own `stage`, else its gate or owning agent. */
export function approvalStage(a: CaseView['pending_approvals'][number]): string {
  if (a.stage) return a.stage
  const agent = a.agent ?? ''
  if (a.gate?.includes('day_one') || a.gate?.includes('signoff')) return 'day_one_readiness'
  if (agent.startsWith('uc01')) return 'intake'
  if (agent.startsWith('uc02')) return 'plan_tasks'
  if (agent.startsWith('uc04')) return 'equipment_access'
  if (agent.startsWith('uc05')) return 'orientation_scheduling'
  return 'day_one_readiness'
}

function scalar(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'boolean') return v ? 'Yes' : 'No'
  if (typeof v === 'object') return Array.isArray(v) ? v.map(scalar).join(', ') : JSON.stringify(v)
  return String(v)
}

/** Key/values of a flat-ish object; nested values are summarized, the raw JSON stays one click away. */
function KeyValues({ data, omit = [] }: { data: Record<string, unknown> | null | undefined; omit?: string[] }) {
  if (!data || Object.keys(data).length === 0) return <p className="text-xs text-slate-400">No data recorded yet.</p>
  return (
    <>
      {Object.entries(data).filter(([k]) => !omit.includes(k)).map(([k, v]) => (
        <Row key={k} label={humanize(k)} value={typeof v === 'object' && v !== null && !Array.isArray(v)
          ? `${Object.keys(v).length} field(s)` : Array.isArray(v) && v.length > 0 && typeof v[0] === 'object'
            ? `${v.length} item(s)` : scalar(v)} />
      ))}
    </>
  )
}

function TaskTable({ tasks }: { tasks: Array<Record<string, unknown>> }) {
  if (!tasks.length) return <p className="text-xs text-slate-400">No tasks in the plan.</p>
  return (
    <div className="overflow-x-auto -mx-1">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-left text-slate-400">
            <th className="px-1 py-1 font-medium">Task</th><th className="px-1 py-1 font-medium">Owner</th>
            <th className="px-1 py-1 font-medium">Due</th><th className="px-1 py-1 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t, i) => (
            <tr key={String(t.task_id ?? i)} className="border-t border-slate-100 align-top">
              <td className="px-1 py-1 text-slate-800">
                {scalar(t.title ?? t.name ?? t.template_key ?? t.task_id)}
                {t.priority !== undefined && <span className="text-slate-400"> · {scalar(t.priority)}</span>}
                {Array.isArray(t.depends_on) && t.depends_on.length > 0 &&
                  <span className="block text-slate-400">after {(t.depends_on as unknown[]).map(scalar).join(', ')}</span>}
                {!Array.isArray(t.depends_on) && Array.isArray(t.blocked_by) && t.blocked_by.length > 0 &&
                  <span className="block text-slate-400">after {(t.blocked_by as unknown[]).map(scalar).join(', ')}</span>}
                {t.description !== undefined && <span className="block text-slate-500">{scalar(t.description)}</span>}
              </td>
              <td className="px-1 py-1 text-slate-600">{scalar(t.owner ?? t.owner_role)}</td>
              <td className="px-1 py-1 text-slate-600 whitespace-nowrap">{scalar(t.due_date)}</td>
              <td className="px-1 py-1 text-slate-600">{scalar(t.status)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

type Rec = Record<string, unknown>

/** A UC05 session as the case record holds it: date and time in the case's timezone, status, approval, calendar
 * reference and participants. Only values that exist are shown (no link or room unless UC05 has one). */
function SessionView({ result, profile, view }: { result: Rec; profile: CaseProfile | null; view: CaseView }) {
  if (!result.session_id) return <p className="text-xs text-slate-400">No session booked yet.</p>
  const when = sessionTime(result.start_utc as string | undefined, result.end_utc as string | undefined,
    result.timezone as string | undefined)
  const participants = (Array.isArray(result.participants) ? result.participants : []) as Rec[]
  // Replies relayed after the stage last ran (the case's participant_responses), newest wins over the stored result.
  const relayed = new Map<string, Rec>()
  for (const r of (view.participant_responses ?? []) as Rec[]) {
    if (r.session_id === result.session_id) relayed.set(String(r.participant_id), r)
  }
  return (
    <>
      <Row label="Session" value={humanizeCode(String(result.session_type ?? 'session'))} />
      {when ? (
        <>
          <Row label="Date" value={when.date} />
          <Row label="Time" value={when.time} />
          <Row label="Timezone" value={when.zone} />
        </>
      ) : <Row label="Scheduled start" value={fmtTime(result.scheduled_start as string | undefined)} />}
      <Row label="Status" value={stateLabel(result.session_status as string | undefined)} />
      {result.approval_status !== undefined && result.approval_status !== null &&
        <Row label="Approval" value={`${stateLabel(String(result.approval_status))}${result.approver_role ? ` · by ${personRoleLabel(String(result.approver_role))}` : ''}`} />}
      {typeof result.meeting_link === 'string' && result.meeting_link && <Row label="Meeting link" value={result.meeting_link} />}
      {typeof result.room_location === 'string' && result.room_location && <Row label="Room" value={result.room_location} />}
      {typeof result.calendar_event_id === 'string' && result.calendar_event_id &&
        <Row label="Calendar reference" value={result.calendar_event_id} />}
      {participants.length > 0 && (
        <div className="mt-2 overflow-x-auto -mx-1">
          <span className="text-xs text-slate-400 block mb-1 px-1">Participants</span>
          <table className="w-full text-[11px]">
            <thead><tr className="text-left text-slate-400">
              <th className="px-1 py-1 font-medium">Participant</th><th className="px-1 py-1 font-medium">Role</th>
              <th className="px-1 py-1 font-medium">Invitation</th><th className="px-1 py-1 font-medium">RSVP</th>
            </tr></thead>
            <tbody>
              {participants.map(pt => {
                const id = String(pt.participant_id ?? '')
                // The joiner's name comes from the identity-gated profile; other names only where a source has one.
                const name = pt.role === 'joiner' ? profile?.candidate_name : (pt.name as string | undefined)
                const shownId = pt.role === 'joiner' ? (view.hrms_employee_no ?? id) : id
                return (
                  <tr key={id} className="border-t border-slate-100 align-top">
                    <td className="px-1 py-1 text-slate-800">{name ? `${name} · ${shownId}` : shownId}</td>
                    <td className="px-1 py-1 text-slate-600">{personRoleLabel(pt.role as string | undefined)}</td>
                    <td className="px-1 py-1 text-slate-600">{pt.invitation_status ? stateLabel(String(pt.invitation_status)) : 'Not invited yet'}</td>
                    <td className="px-1 py-1 text-slate-600">{(() => {
                      const later = relayed.get(id)
                      const rsvp = (later?.response ?? pt.rsvp) as string | undefined
                      const at = (later?.at ?? pt.responded_at) as string | undefined
                      return rsvp ? `${stateLabel(rsvp)}${at ? ` · ${fmtTime(at)}` : ''}` : 'No reply yet'
                    })()}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

/** The buddy as UC05 assigned or proposed them, with the profile from the buddy source. A profile holding placeholder
 * or invalid values is never presented as a valid buddy. */
function BuddyView({ result }: { result: Rec }) {
  const id = (result.buddy_id ?? result.proposed_buddy_id) as string | undefined
  if (!id) {
    return <p className="text-xs text-slate-500">{result.status === 'no_candidates'
      ? 'Buddy assignment pending — no eligible buddy profile configured.' : 'No buddy proposed yet.'}</p>
  }
  const invalid = result.buddy_profile_valid === false || looksLikePlaceholder(id)
  if (invalid) {
    return (
      <div className="rounded border border-red-200 bg-red-50 p-2 text-[11px] text-red-700">
        <p className="font-semibold">Invalid buddy record</p>
        <p>The {result.buddy_id ? 'assigned' : 'proposed'} buddy profile holds placeholder or invalid values, so it is not
          shown as a buddy. HR corrects the profile in buddy_profiles; re-assigning this case's buddy is an HR decision.</p>
      </div>
    )
  }
  return (
    <>
      <Row label="Buddy employee no." value={id} />
      <Row label="Buddy name" value={(result.buddy_name as string | undefined) ?? (result.buddy_profile_found === false
        ? 'Not in the buddy directory' : 'Not reported yet')} />
      {result.buddy_department ? <Row label="Department" value={String(result.buddy_department)} /> : null}
      {result.buddy_location ? <Row label="Location" value={String(result.buddy_location)} /> : null}
      <Row label="Status" value={stateLabel(result.status as string | undefined)} />
      {result.approval_id ? <Row label="Approval" value={String(result.approval_id)} /> : null}
    </>
  )
}

/** UC04's result for the case: each requested item with its kind and state, who recorded the requests, the item
 * decisions, UC04's advice and the configuration evaluated against. */
function EquipmentView({ view }: { view: CaseView }) {
  const ea = (view.equipment_access ?? {}) as Rec
  const items = (Array.isArray(ea.items) ? ea.items : []) as Rec[]
  const requests = (view.equipment_access_requests ?? null) as Rec | null
  const decisions = (view.approval_decisions as Rec[]).filter(d => d.agent === 'dact-uc04-equipment-access')
  const advice = (Array.isArray(ea.advisories) ? ea.advisories : []) as Rec[]
  const pendingRoles = Array.from(new Set(view.pending_approvals
    .filter(a => a.agent === 'dact-uc04-equipment-access' && a.role).map(a => a.role as string)))
  const kindOf = (requestId: unknown) => {
    const key = String(requestId ?? '').split(':').pop() ?? ''
    return key.startsWith('EQ-') ? 'Equipment' : key.startsWith('AC-') ? 'Access' : 'Item'
  }
  return (
    <>
      <Row label="Readiness" value={stateLabel(ea.overall_status as string | undefined)} />
      {items.length > 0 ? items.map((it, i) => {
        const decision = decisions.filter(d => String(d.reference ?? '').startsWith(`${String(it.request_id)}:`)).pop()
        return (
          <p key={i} className="text-[11px] text-slate-700">
            <span className="text-slate-400">{kindOf(it.request_id)}</span> {scalar(it.item_id)} — {stateLabel(it.state as string)}
            {it.ticket_id ? ` (ticket ${scalar(it.ticket_id)})` : ''}
            {decision ? ` · ${stateLabel(String(decision.decision))} by ${scalar(decision.approver)}` : ''}
          </p>
        )
      }) : <p className="text-[11px] text-slate-400">No items evaluated yet.</p>}
      {pendingRoles.length > 0 && <Row label="Approver" value={pendingRoles.map(r => personRoleLabel(r)).join(', ')} />}
      {requests && <Row label="Requested by" value={`${scalar(requests.recorded_by)} · ${fmtTime(requests.recorded_at as string | undefined)}`} />}
      {advice.length > 0 && (
        <div className="mt-1">
          <span className="text-xs text-slate-400 block">UC04 advice</span>
          {advice.map((a, i) => <p key={i} className="text-[11px] text-slate-700">• {scalar(a.rationale)}</p>)}
        </div>
      )}
      {ea.config_version ? <Row label="Configuration" value={`${scalar(ea.role_code)} · ${scalar(ea.config_version)}`} /> : null}
      <Uc02PlanView plan={(ea.uc02_plan ?? null) as Rec | null} />
    </>
  )
}

/** The UC-02 onboarding plan UC04 evaluated with (backend: adapters/uc04_uc02_plan.py): its service tasks and the
 * advisory warnings. Informational: the warnings never change UC04's status. */
function Uc02PlanView({ plan }: { plan: Rec | null }) {
  if (!plan) return null
  const tasks = (Array.isArray(plan.service_tasks) ? plan.service_tasks : []) as Rec[]
  const warnings = (Array.isArray(plan.warnings) ? plan.warnings : []) as Rec[]
  return (
    <div className="mt-2">
      <span className="text-xs text-slate-400 block">UC-02 onboarding plan</span>
      <p className="text-[11px] text-slate-700">
        {scalar(plan.onboarding_id)} · v{scalar(plan.plan_version)} · {humanize(String(plan.plan_status ?? '—'))}
        {plan.data_origin === 'synthetic' ? ' · synthetic' : ''}
      </p>
      {tasks.length > 0 ? tasks.map((t, i) => (
        <p key={i} className="text-[11px] text-slate-700">
          <span className="text-slate-400">{humanize(String(t.category))}</span> {scalar(t.template_key)} —{' '}
          {humanize(String(t.status))} · due {scalar(t.due_date)} · {personRoleLabel(String(t.owner))}
        </p>
      )) : null}
      {warnings.map((w, i) => (
        <p key={i} className="mt-1 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] text-amber-800">
          <span className="font-semibold">{scalar(w.code)}</span> · {scalar(w.detail)} (advisory)
        </p>
      ))}
    </div>
  )
}

/** For a UC05 result recorded before UC05 reported session/participant/buddy details: say so and offer a refresh
 * (display details only -- no status, approval or decision changes). */
function Uc05DetailsRefresh({ stage, view, onApply }: { stage: string; view: CaseView; onApply: (v: CaseView) => void }) {
  const me = usePermissions()
  const may = allowed(me, 'uc05_operations')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const result = ({ orientation_scheduling: view.orientation, team_introduction: view.team_introduction,
    buddy_introduction: view.buddy_introduction, buddy_assignment: view.buddy_assignment } as Record<string, unknown>)[stage] as Rec | null
  if (!result) return null
  const stale = stage === 'buddy_assignment'
    ? !!(result.buddy_id ?? result.proposed_buddy_id) && result.buddy_profile_found === undefined
    : !!result.session_id && result.start_utc === undefined
  if (!stale) return null
  return (
    <div className="mt-2 rounded border border-slate-200 bg-slate-50 p-2 text-[11px] text-slate-600">
      These details were recorded before UC05 reported session times, participants and buddy profiles.
      <button disabled={busy || !may.ok} title={may.reason ?? undefined}
        onClick={async () => { setBusy(true); setError(null)
          try { onApply(await refreshUc05Details(view.case_id)) } catch (e) { setError(apiErrorMessage(e)) } finally { setBusy(false) } }}
        className="ml-1 text-indigo-700 underline disabled:opacity-40">{busy ? 'Refreshing…' : 'Refresh details from UC05'}</button>
      {error && <p className="text-red-600">{error}</p>}
    </div>
  )
}

function StageOutputView({ stage, view, health, profile }: { stage: string; view: CaseView; health: HealthView | null;
                                                           profile: CaseProfile | null }) {
  const outcome = view.stage_outcomes[stage]
  switch (stage) {
    case 'intake':
      return (
        <>
          <Row label="Case ID" value={view.case_id} />
          <Row label="HRMS employee no." value={view.hrms_employee_no ?? 'Not resolved yet'} />
          <Row label="UC01 development employee no." value={view.uc01_employee_no ?? view.employee_id} />
          <Row label="Joining date" value={view.joining_date && `${view.joining_date} (revision ${view.joining_date_revision ?? 1})`} />
          <Row label="Employee record status" value={stageStatusView(stage, view.stage_statuses.intake).label} />
          <Row label="Validation results" value={view.blockers.filter(b => b.stage === 'intake').map(b => `${b.code}: ${b.reason}`).join('; ') || 'No intake discrepancies reported'} />
          <Row label="Orchestration state" value={`${health?.state_store ?? 'state store'} · version ${view.state_version ?? '—'}`} />
          <Row label="Fields set by UC01" value={(outcome?.updated_fields ?? []).join(', ')} />
        </>
      )
    case 'plan_tasks':
    case 'replan_tasks': {
      const plan = (view.onboarding_plan ?? {}) as Record<string, unknown>
      const taskPlan = (view.task_plan ?? {}) as Record<string, unknown>
      const tasks = (Array.isArray(taskPlan.tasks) ? taskPlan.tasks : []) as Array<Record<string, unknown>>
      return (
        <>
          <Row label="Plan" value={`${scalar(plan.plan_id)} · revision v${scalar(plan.plan_version)}`} />
          {stage === 'replan_tasks' && <Row label="Previous revision" value={scalar(plan.previous_plan_version)} />}
          <Row label="Planning status" value={stageStatusView(stage, view.stage_statuses[stage], view).label} />
          {taskPlan.task_count !== undefined && (
            <Row label="Planned tasks" value={`${scalar(taskPlan.task_count)}${taskPlan.required_task_count != null
              ? ` (${scalar(taskPlan.required_task_count)} required by the client's milestones)` : ''}`} />
          )}
          {taskPlan.dependency_count !== undefined && <Row label="Dependencies" value={scalar(taskPlan.dependency_count)} />}
          {Array.isArray(taskPlan.ready) && Array.isArray(taskPlan.blocked) &&
            <Row label="Ready / waiting" value={`${(taskPlan.ready as unknown[]).length} ready · ${(taskPlan.blocked as unknown[]).length} waiting on a prerequisite`} />}
          <Row label="Required documents" value={((plan.required_documents ?? []) as Array<{ document_type: string }>).map(d => d.document_type).join(', ')} />
          {taskPlan.config_version !== undefined &&
            <Row label="Client configuration" value={`version ${scalar(taskPlan.config_version)}${taskPlan.data_origin ? ` · ${scalar(taskPlan.data_origin)}` : ''}`} />}
          {taskPlan.store !== undefined &&
            <Row label="Plan stored in" value={taskPlan.store === 'memory' ? 'process memory (not durable)' : scalar(taskPlan.store)} />}
          {(taskPlan.refreshed_at ?? taskPlan.planned_at) !== undefined &&
            <Row label="Plan last updated" value={fmtTime((taskPlan.refreshed_at ?? taskPlan.planned_at) as string | undefined)} />}
          {taskPlan.refresh_pending === true &&
            <p className="text-[11px] text-orange-600">The task view could not be refreshed from UC02 after the last change; the plan revision above is current.</p>}
          <div className="mt-2"><TaskTable tasks={tasks} /></div>
          {view.plan_history.length > 0 && (
            <div className="mt-3">
              <span className="text-xs text-slate-400 block mb-1">Revision history</span>
              {view.plan_history.map((h, i) => (
                <p key={i} className="text-[11px] text-slate-600">v{scalar(h.plan_version)} · {scalar(h.action)} · {scalar(h.reason)}</p>
              ))}
            </div>
          )}
        </>
      )
    }
    case 'document_collection': {
      const c = view.document_collection
      return c ? (
        <>
          <Row label="Plan revision" value={`v${c.plan_version ?? '—'} (${c.plan_id ?? '—'})`} />
          <Row label="Requested at" value={fmtTime(c.requested_at)} />
          <Row label="Outstanding" value={c.outstanding.map(o => typeof o === 'string' ? o
            : `${o.document_type}${o.reason ? ` (${humanizeCode(o.reason)})` : ''}`).join(', ') || 'None'} />
          <Row label="Request notification" value={c.notification
            ? `${scalar(c.notification.status)} via ${scalar(c.notification.transport)}${c.notification.synthetic ? ' (mock transport)' : ''}` : '—'} />
        </>
      ) : <p className="text-xs text-slate-400">Not opened yet.</p>
    }
    case 'validate_documents': {
      const fb = (view.document_validation_feedback ?? {}) as Record<string, unknown>
      return (
        <>
          <Row label="Validation status" value={humanize(scalar(fb.validation_status))} />
          <Row label="Validated against plan" value={fb.plan_version !== undefined ? `v${scalar(fb.plan_version)}` : '—'} />
          <Row label="Verified" value={scalar(fb.verified_documents)} />
          <Row label="Missing" value={scalar(fb.missing_documents)} />
          <Row label="Pending HR review" value={scalar(fb.pending_hr_reviews)} />
          <Row label="Replanning reasons" value={scalar(fb.replanning_reasons)} />
        </>
      )
    }
    case 'equipment_access':
      return <EquipmentView view={view} />
    case 'orientation_scheduling':
      return <SessionView result={(view.orientation ?? {}) as Rec} profile={profile} view={view} />
    case 'team_introduction':
      return <SessionView result={(view.team_introduction ?? {}) as Rec} profile={profile} view={view} />
    case 'buddy_introduction':
      return <SessionView result={(view.buddy_introduction ?? {}) as Rec} profile={profile} view={view} />
    case 'buddy_assignment':
      return <BuddyView result={(view.buddy_assignment ?? {}) as Rec} />
    default:
      return <KeyValues data={stageOutput(view, stage) as Record<string, unknown> | null} />
  }
}

export default function StageSidebar({ stage, view, profile, execution, health, onApply, onClose }: Props) {
  const meta = STAGES[stage]
  const s = stageStatusView(stage, view.stage_statuses[stage], view)
  const outcome = view.stage_outcomes[stage]
  const err = view.stage_errors[stage] ?? outcome?.error ?? null
  const history = view.stage_history.filter(h => h.stage === stage)
  const blockers = view.blockers.filter(b => b.stage === stage)
  // The form approval is display-only here (decided by an authorized approver through the approval API).
  const approvals = view.pending_approvals.filter(a => approvalStage(a) === stage && a.gate !== 'onboarding_form_approval')
  const heldByFormApproval = awaitingFormApproval(view) && (stage === 'intake' || stage === 'plan_tasks')
  const retry = view.retry_metadata[stage]
  const [busy, setBusy] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const me = usePermissions()
  const mayResume = allowed(me, 'resume_case')
  const mayRetry = allowed(me, 'retry_stage')

  async function act(key: string, fn: () => Promise<CaseView>) {
    setBusy(key); setActionError(null)
    try { onApply(await fn()) } catch (e) { setActionError(apiErrorMessage(e)) } finally { setBusy(null) }
  }

  // The backend's rules (orchestrator_agent.retry_stage / resume_case): a retry is only for a technical
  // failure (RETRY_PENDING, FAILED, or BLOCKED with a technical code); a business block is resumed once
  // its cause is corrected.
  const technicalBlock = s.raw === 'blocked' && TECHNICAL_RETRY_CODES.has(err?.code ?? '')
  const canRetry = ['retry_pending', 'failed'].includes(s.raw) || technicalBlock
  // A UC04 stage awaiting approval can be re-evaluated too: UC04 re-reads the approvals recorded so far.
  const canResume = (s.raw === 'blocked' && !technicalBlock) || s.raw === 'uncertain'
    || (stage === 'equipment_access' && s.raw === 'awaiting_approval')
  const inputs = outcome?.inputs as Record<string, unknown> | undefined

  return (
    <div className="w-96 shrink-0 bg-white rounded-lg border border-slate-200 shadow-sm overflow-hidden">
      {/* Header */}
      <div className="flex items-start justify-between px-4 py-3 border-b border-slate-100 bg-indigo-50">
        <div>
          <span className="text-xs font-bold text-indigo-600 uppercase tracking-wide">{meta.uc}</span>
          <h3 className="text-sm font-semibold text-slate-900 mt-0.5">{meta.title}</h3>
          <span className={`inline-block mt-1 text-[11px] px-2 py-0.5 rounded-full font-medium ${TONE_BADGE[s.tone]}`}>{s.label}</span>
          {outcome?.data_origin === 'synthetic' &&
            <span className="inline-block ml-1 mt-1 text-[11px] px-2 py-0.5 rounded-full font-medium bg-amber-100 text-amber-700"
              title="The backend labels this outcome synthetic: synthetic test records or mock connectors">data: synthetic</span>}
        </div>
        <button onClick={onClose} className="text-slate-400 hover:text-slate-600 ml-2 mt-0.5 shrink-0" aria-label="Close"><X className="w-4 h-4" /></button>
      </div>

      <div className="overflow-y-auto max-h-[calc(100vh-220px)]">
        {stage === 'form_approval' ? (
          <>
            <Section title="Overview"><p className="text-xs text-slate-500 leading-snug">{meta.description}</p></Section>
            <FormApprovalPanel view={view} onApply={onApply} />
          </>
        ) : (<>
        {heldByFormApproval && (
          <div className="px-4 py-3 border-b border-amber-100 bg-amber-50">
            <p className="text-xs font-semibold text-amber-800">Waiting for HR Form Approval</p>
            <p className="text-[11px] text-amber-700 mt-0.5">
              {stage === 'intake'
                ? 'UC01 creates the employee record only from an approved internal onboarding form.'
                : 'UC02 plans only after UC01 has created the record from the approved form.'}
            </p>
          </div>
        )}
        {heldByFormApproval && <FormApprovalPanel view={view} onApply={onApply} />}
        <Section title="Overview">
          <p className="text-xs text-slate-500 mb-2 leading-snug">{meta.description}</p>
          <Row label="Stored status" value={<code className="text-[11px]">{s.raw}</code>} />
          <Row label="Case current stage" value={STAGES[view.current_stage]?.title ?? view.current_stage} />
          <Row label="Last executed" value={fmtTime(outcome?.finished_at ?? outcome?.started_at)} />
          <Row label="Execution duration" value={fmtDuration(outcome?.started_at, outcome?.finished_at)} />
          <Row label="Attempts" value={execution?.attempts ?? outcome?.attempt} />
          <Row label="Execution ID" value={outcome?.execution_id && <code className="text-[11px]">{outcome.execution_id}</code>} />
        </Section>

        <Section title="Input">
          {stage === 'intake' && profile && (
            <>
              <span className="text-[11px] text-slate-400 block mb-1">Submitted employee details (internal onboarding form)</span>
              <KeyValues data={profile as Record<string, unknown>} />
            </>
          )}
          {stage === 'intake' && view.form_extraction && !heldByFormApproval && (
            <div className="mt-2">
              <span className="text-[11px] text-slate-400 block mb-1">Source document (UC01 extraction)</span>
              <SourceDocumentSummary view={view} />
            </div>
          )}
          {inputs ? <KeyValues data={inputs} omit={stage === 'validate_documents' ? ['documents'] : []} />
            : !profile || stage !== 'intake' ? <p className="text-xs text-slate-400">This stage has not run yet.</p> : null}
          {execution?.dependencies && execution.dependencies.length > 0 && (
            <div className="mt-2">
              <span className="text-[11px] text-slate-400 block mb-1">Source (upstream stage results)</span>
              {execution.dependencies.map(d => (
                <p key={d.key} className="text-[11px] text-slate-600">
                  {d.satisfied ? '✓' : '✗'} {d.key} ← {STAGES[d.upstream_stage]?.title ?? d.upstream_stage} ({d.use_case_id})
                  {!d.satisfied && d.detail ? ` — ${d.detail}` : ''}
                </p>
              ))}
            </div>
          )}
          {inputs && <ExpandableJSON data={inputs} label="Raw input" />}
        </Section>

        <Section title="Output">
          <StageOutputView stage={stage} view={view} health={health} profile={profile} />
          <Uc05DetailsRefresh stage={stage} view={view} onApply={onApply} />
          {stage === 'joining_day_welcome' && (
            <div className="mt-2"><WelcomeMessage caseId={view.case_id} refreshKey={view.updated_at ?? undefined} /></div>
          )}
          {stage === 'joining_day_welcome' && health?.mode === 'demo' && !view.joining_day_event
            && view.stage_statuses.joining_day_welcome === 'waiting' && view.joining_date && (
            <SimulateJoiningDay caseId={view.case_id} joiningDate={view.joining_date} onApply={onApply} />
          )}
          {stageOutput(view, stage) != null && <ExpandableJSON data={stageOutput(view, stage)} label="Raw output" />}
        </Section>

        {UC03_STAGES.has(stage) && (
          <Section title="Documents">
            <DocumentChecklist view={view} health={health} onApply={onApply} />
          </Section>
        )}

        {blockers.length > 0 && (
          <Section title="Blockers">
            {blockers.map((b, i) => <BlockerCard key={`${b.code}-${i}`} blocker={b} view={view} onApply={onApply} />)}
          </Section>
        )}

        <Section title="Activity">
          {err && (
            <div className="mb-2 text-xs bg-red-50 border border-red-100 rounded px-2 py-1.5 text-red-700">
              <span className="font-semibold">{err.code}</span>: {err.detail}{err.retryable ? ' (retryable)' : ''}
            </div>
          )}
          {retry && <p className="text-[11px] text-slate-600 mb-1">Retries: {scalar(retry.failures)} / {scalar(retry.max_attempts)}{retry.exhausted ? ' · exhausted' : ''}</p>}
          {history.length ? history.slice(-12).map((h, i) => (
            <p key={i} className="text-[11px] text-slate-600">{fmtTime(h.at)} → {stageStatusView(stage, h.status).label}</p>
          )) : <p className="text-xs text-slate-400">No status transitions yet.</p>}
          {approvals.length > 0 && <p className="text-[11px] text-amber-700 mt-1">{approvals.length} approval(s) required</p>}
        </Section>

        {(canRetry || canResume || approvals.length > 0 || (UC05_STAGES.has(stage) && s.raw === 'uncertain') || canResume) && (
          <Section title="Actions">
            {actionError && <p className="text-xs text-red-600 mb-2">{actionError}</p>}
            {approvals.map(a => {
              // Advisory: UC05 itself refuses anyone but the person on record; say so before a click fails.
              const notMine = me && ((a.approver_id && a.approver_id !== me.user_id) || (a.role && !me.roles.includes(a.role)))
              const whoDecides = a.approver_id ? `the ${a.role ?? 'approver'} on record (${a.approver_id})` : `the '${a.role}' role`
              return (
              <div key={a.reference} className="mb-2 rounded border border-amber-200 bg-amber-50 p-2">
                <p className="text-[11px] font-medium text-slate-800">{approvalLabel(a.gate ?? a.proposal_type)}</p>
                {a.description && !/^orchestrator gate /.test(a.description) &&
                  <p className="text-[11px] text-slate-700">{a.description}</p>}
                {a.role && <p className="text-[11px] text-slate-500">Approver: {personRoleLabel(a.role)}{a.approver_id ? ` (${a.approver_id})` : ''}</p>}
                {notMine && (
                  <p className="text-[11px] text-slate-500 mt-1">Only {whoDecides} can decide this — you are acting as {me.user_id}.
                    {a.approver_id ? ` Switch identity to ${a.approver_id} to decide it.` : ''}</p>
                )}
                <div className="flex gap-2 mt-1.5">
                  <button disabled={!!busy || !!notMine} onClick={() => act(`a:${a.reference}`, () => decideApproval(view.case_id, a.reference, 'approved'))}
                    className="px-2 py-1 text-xs rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-40">Approve</button>
                  <button disabled={!!busy || !!notMine} onClick={() => act(`r:${a.reference}`, () => decideApproval(view.case_id, a.reference, 'rejected'))}
                    className="px-2 py-1 text-xs rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-40">Reject</button>
                  <button disabled={!!busy} onClick={() => act(`x:${a.reference}`, () => retryApproval(view.case_id, a.reference))}
                    title="Only when a decision was recorded but applying it failed"
                    className="px-2 py-1 text-xs rounded border border-slate-300 text-slate-600 hover:bg-white disabled:opacity-40">Retry apply</button>
                </div>
              </div>
              )
            })}
            <div className="flex flex-wrap gap-2">
              {canRetry && (
                <button disabled={!!busy || !mayRetry.ok} title={mayRetry.reason ?? undefined}
                  onClick={() => act('retry', () => retryStage(view.case_id, stage))}
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-xs rounded border border-slate-300 hover:bg-slate-50 disabled:opacity-40">
                  {busy === 'retry' ? <Loader className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />} Retry stage
                </button>
              )}
              {canResume && (
                <button disabled={!!busy || !mayResume.ok} title={mayResume.reason ?? undefined}
                  onClick={() => act('resume', () => resumeCase(view.case_id))}
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-xs rounded border border-slate-300 hover:bg-slate-50 disabled:opacity-40">
                  {busy === 'resume' ? <Loader className="w-3 h-3 animate-spin" /> : <RefreshCcw className="w-3 h-3" />}
                  {STAGES[stage]?.uc?.startsWith('UC') ? `Retry ${STAGES[stage].uc} (resume)` : 'Resume case'}
                </button>
              )}
            </div>
            {canResume && !mayResume.ok && <p className="text-[11px] text-slate-500 mt-2">Resume {mayResume.reason}.</p>}
            {canRetry && !mayRetry.ok && <p className="text-[11px] text-slate-500 mt-2">Retry {mayRetry.reason}.</p>}
            <p className="text-[11px] text-slate-400 mt-2">The orchestrator checks your role and the case state; it refuses an action that does not apply.</p>
          </Section>
        )}
        </>)}
      </div>
    </div>
  )
}
