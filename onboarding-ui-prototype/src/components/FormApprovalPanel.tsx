/**
 * The internal onboarding form's approval request, read from the backend (GET .../form-approval), with
 * Approve / Reject for an authorized approver (the policy's role, never the requester) through the decision API.
 */
import { useEffect, useState } from 'react'
import { Info, ShieldCheck } from 'lucide-react'
import { currentIdentity } from '../api/apiClient'
import { getFormApproval } from '../api/onboardingApi'
import ApprovalActions from './ApprovalActions'
import type { CaseView, FormApproval } from '../api/onboardingApi'
import { STAGES, TONE_BADGE, fmtTime, formApprovalView, roleLabel, stageStatusView } from '../lib/stages'
import { extractionIssueLabel, extractionStatusLabel } from '../lib/labels'

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="mb-1.5">
      <span className="text-xs text-slate-400 block">{label}</span>
      <span className="text-xs font-medium text-slate-800 break-words">{value === null || value === undefined || value === '' ? '—' : value}</span>
    </div>
  )
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="px-4 py-3 border-b border-slate-100">
      <h4 className="text-xs font-bold text-slate-500 uppercase tracking-wide mb-2">{title}</h4>
      {children}
    </div>
  )
}

const IMPACT = ['intake', 'plan_tasks', 'validate_documents', 'equipment_access', 'orientation_scheduling'] as const

/** Where the form's data came from, as the case records it (statuses and field names; never a document value). */
export function SourceDocumentSummary({ view }: { view: CaseView }) {
  const fx = view.form_extraction
  if (!fx) return null
  const formStatus = view.form_approval?.status
  const list = (items: string[]) => items.length ? items.join(', ') : 'none'
  return (
    <>
      <Row label="Started from" value={`Onboarding form document (${fx.source_document.content_type || 'document'}, ${
        fx.source_document.template_recognised ? fx.source_document.template_id : 'template not recognised'})`} />
      <Row label="Document status" value={`${extractionStatusLabel(fx.status)}${formStatus === 'approved'
        ? ` → form ${extractionStatusLabel('APPROVED').toLowerCase()} by HR` : ''}`} />
      <Row label="Completeness" value={`${Math.round((fx.completeness ?? 0) * 100)}%`} />
      <Row label="Fields taken from the document" value={list(Object.keys(fx.prefill_sources))} />
      <Row label="Changed by HR after extraction" value={list(fx.fields_changed_by_hr)} />
      <Row label="Entered by HR (not in the document)" value={list(fx.fields_entered_by_hr)} />
      {fx.validation_errors.length > 0 && (
        <Row label="Document lacked" value={fx.validation_errors.map(extractionIssueLabel).join('; ')} />
      )}
      {fx.review_reasons.length > 0 && (
        <Row label="Flagged for review" value={fx.review_reasons.map(extractionIssueLabel).join('; ')} />
      )}
      <Row label="Review confirmed by HR" value={fx.validation_errors.length || fx.review_reasons.length
        ? (fx.reviewed ? 'Yes' : 'No') : 'Not needed (nothing flagged)'} />
      <Row label="Document fingerprint (SHA-256)" value={<code className="text-[11px]">{fx.source_document.sha256.slice(0, 16)}…</code>} />
      <p className="text-[11px] text-slate-500">The case keeps statuses and field names only; compare the form fields above
        with the source document itself.</p>
    </>
  )
}

export default function FormApprovalPanel({ view, onApply }: { view: CaseView; onApply?: (view: CaseView) => void }) {
  const [detail, setDetail] = useState<FormApproval | null>(null)
  const [error, setError] = useState<string | null>(null)
  const summary = view.form_approval
  const approvalId = summary?.approval_id
  const status = summary?.status

  useEffect(() => {
    let alive = true
    setError(null)
    getFormApproval(view.case_id).then(fa => { if (alive) setDetail(fa) })
      .catch(() => { if (alive) setError('The approval details could not be loaded; showing the case summary.') })
    return () => { alive = false }
  }, [view.case_id, approvalId, status])

  const fa = detail ?? summary
  if (!fa) return <Block title="Approval"><p className="text-xs text-slate-500">No form approval is recorded for this case.</p></Block>
  if (!fa.required) {
    return (
      <Block title="Approval">
        <p className="text-xs text-slate-600">This case was started by a trusted integration that attested UC01’s own form
          approval, so no approval request was raised here.</p>
      </Block>
    )
  }
  const s = formApprovalView(fa)
  const assigned = fa.assignment?.assigned_approver_id

  return (
    <>
      <Block title="Approval Request">
        <Row label="Approval ID" value={fa.approval_id && <code className="text-[11px]">{fa.approval_id}</code>} />
        <Row label="Case ID" value={<code className="text-[11px]">{view.case_id}</code>} />
        <Row label="Employee name" value={fa.employee_name ?? (detail ? 'Withheld for your role' : '—')} />
        <Row label="Employee number" value={view.employee_id ?? 'Not yet allocated — UC01 allocates it after approval'} />
        <Row label="Approval type" value={fa.approval_type === 'INTERNAL_ONBOARDING_FORM' ? 'Internal Onboarding Form' : fa.approval_type} />
        <Row label="Requested by" value={fa.requested_by ?? (detail ? 'Withheld for your role' : '—')} />
        <Row label="Requested at" value={fmtTime(fa.requested_at)} />
      </Block>

      {view.form_extraction && <Block title="Source Document"><SourceDocumentSummary view={view} /></Block>}

      <Block title="Approver Information">
        <Row label="Required approver role" value={`${roleLabel(fa.required_approver_role)} (${fa.required_approver_role})`} />
        <Row label="Assigned approver" value={assigned ?? 'None assigned'} />
        <Row label="Assignment status" value={fa.assignment?.status === 'assignment_pending'
          ? 'Assignment pending configuration (role-based queue)' : fa.assignment?.status} />
        <p className="text-[11px] text-slate-500 mt-1">{fa.assignment?.detail}</p>
        {fa.separation_of_duties && (
          <p className="text-[11px] text-slate-500 mt-1">Separation of duties: the person who submitted the form cannot decide it.</p>
        )}
      </Block>

      <Block title="Approval Status">
        <span className={`inline-block text-[11px] px-2 py-0.5 rounded-full font-medium ${TONE_BADGE[s.tone]}`}>{s.label}</span>
        {fa.decision ? (
          <div className="mt-2">
            <Row label="Decision" value={fa.decision.decision === 'approved' ? 'Approved' : 'Rejected'} />
            <Row label="Decided by" value={fa.decision.decided_by ?? 'Withheld for your role'} />
            <Row label="Verified approver role" value={roleLabel(fa.decision.decided_by_role)} />
            <Row label="Decided at" value={fmtTime(fa.decision.decided_at)} />
            {fa.decision.decision === 'rejected' && <Row label="Reason" value={[fa.decision.reason_code, fa.decision.note].filter(Boolean).join(' — ')} />}
          </div>
        ) : (
          <p className="text-[11px] text-slate-500 mt-2">No decision has been made. Nothing is approved until an authorized
            {' '}{roleLabel(fa.required_approver_role)} decides.</p>
        )}
      </Block>

      <Block title="Workflow Impact">
        <div className="flex items-center justify-between text-xs mb-1">
          <span className="text-slate-700">HR Form Approval</span>
          <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${TONE_BADGE[s.tone]}`}>{s.label}</span>
        </div>
        {IMPACT.map(stage => {
          const v = stageStatusView(stage, view.stage_statuses[stage], view)
          return (
            <div key={stage} className="flex items-center justify-between text-xs mb-1">
              <span className="text-slate-700">{STAGES[stage].uc} {STAGES[stage].title}</span>
              <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${TONE_BADGE[v.tone]}`}>{v.label}</span>
            </div>
          )
        })}
      </Block>

      <Block title="Persistence">
        <Row label="HITL approval inbox" value="Stored (orchestrator approval inbox)" />
        <Row label="Supabase approval_requests" value={fa.persistence
          ? (fa.persistence.persisted ? `Written (${fa.persistence.outcome})` : `Not written: ${fa.persistence.reason}`) : '—'} />
      </Block>

      {fa.status === 'pending' && fa.approval_id && onApply && (() => {
        const me = currentIdentity()
        const roles = me.roles.split(',').map(r => r.trim())
        const isRequester = fa.separation_of_duties && fa.requested_by === me.userId
        const hasRole = !!fa.required_approver_role && roles.includes(fa.required_approver_role)
        return (
          <Block title="Decision">
            <p className="text-[11px] text-slate-500 mb-2">Acting as {me.userId} ({me.roles}).</p>
            <ApprovalActions caseId={view.case_id} approvalId={fa.approval_id} canDecide={hasRole && !isRequester}
              reason={!hasRole ? `requires the '${fa.required_approver_role}' role`
                : isRequester ? 'you submitted this form; separation of duties requires another approver' : null}
              subject={`the internal onboarding form for ${fa.employee_name ?? view.case_id}`}
              effect="UC01 then creates the employee record and the workflow continues." onDecided={onApply} />
          </Block>
        )
      })()}

      {error && (
        <div className="px-4 py-3 flex items-start gap-2 text-[11px] text-slate-500">
          <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" /><span>{error}</span>
        </div>
      )}
      {!error && fa.status !== 'pending' && (
        <div className="px-4 py-3 flex items-start gap-2 text-[11px] text-slate-500">
          <ShieldCheck className="w-3.5 h-3.5 shrink-0 mt-0.5" /><span>Decided through the approval API; the decision is recorded and audited.</span>
        </div>
      )}
    </>
  )
}
