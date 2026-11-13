/**
 * Every pending approval of the client's cases (GET /v1/orchestration/approvals), with Approve / Reject for
 * the ones the current user may decide. Polls while mounted; refreshed right after a decision.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { ExternalLink, ShieldCheck } from 'lucide-react'
import { apiErrorMessage, currentIdentity } from '../api/apiClient'
import { listApprovals } from '../api/onboardingApi'
import type { ApprovalInboxItem, CaseView } from '../api/onboardingApi'
import { approvalLabel } from '../lib/labels'
import { STAGES, fmtTime, roleLabel } from '../lib/stages'
import ApprovalActions from './ApprovalActions'

interface Props {
  enabled: boolean
  onOpenCase: (caseId: string, stage: string) => void
  onDecided: (view: CaseView, item: ApprovalInboxItem) => void
}

function typeLabel(item: ApprovalInboxItem): string {
  return approvalLabel(item.gate === 'day_one_signoff' ? item.gate : item.approval_type || item.gate)
}

/** What this approval decides, from the backend's own fields: the UC04 item, else the approval's description. */
function requestLine(item: ApprovalInboxItem): string | null {
  const r = item.request
  if (r?.item_id) {
    const kind = r.item_kind === 'EQUIPMENT' ? 'Equipment' : r.item_kind === 'ACCESS' ? 'Access' : 'Item'
    return `${kind}: ${r.item_id}${r.approval_type ? ` · ${r.approval_type.toLowerCase()} approval` : ''}`
  }
  return item.description ?? null
}

export default function ApprovalsInbox({ enabled, onOpenCase, onDecided }: Props) {
  const [items, setItems] = useState<ApprovalInboxItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(false)

  const refresh = useCallback(async () => {
    if (!enabled) return
    try {
      const data = await listApprovals()
      if (mounted.current) { setItems(data); setError(null) }
    } catch (e) {
      if (mounted.current) setError(apiErrorMessage(e))
    }
  }, [enabled])

  useEffect(() => {
    mounted.current = true
    void refresh()
    const id = setInterval(() => { if (document.visibilityState === 'visible') void refresh() }, 10_000)
    return () => { mounted.current = false; clearInterval(id) }
  }, [refresh])

  const me = currentIdentity()
  const mine = (items ?? []).filter(i => i.can_decide).length

  return (
    <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-6">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold text-slate-900 flex items-center gap-2">
          <ShieldCheck className="w-5 h-5 text-amber-600" /> Pending Approvals
          {items && <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-amber-100 text-amber-700">{items.length}</span>}
        </h2>
        <span className="text-xs text-slate-400">
          Acting as {me.userId} ({me.roles}) · {items ? `${mine} you can decide` : enabled ? 'loading…' : 'offline'}
        </span>
      </div>
      {error && <p className="text-xs text-red-600 mb-2">{error}</p>}
      {items && items.length === 0 && <p className="text-sm text-slate-500 py-2 text-center">No approvals are waiting.</p>}
      {items && items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-400 border-b border-slate-100">
                <th className="py-2 pr-3 font-medium">Employee / case</th>
                <th className="py-2 pr-3 font-medium">Approval</th>
                <th className="py-2 pr-3 font-medium">Required approver</th>
                <th className="py-2 pr-3 font-medium">Requested</th>
                <th className="py-2 font-medium">Decision</th>
              </tr>
            </thead>
            <tbody>
              {items.map(item => {
                const stage = item.approval_type === 'INTERNAL_ONBOARDING_FORM' ? 'form_approval' : item.stage ?? 'intake'
                return (
                  <tr key={`${item.case_id}:${item.approval_id}`} className="border-b border-slate-50 align-top">
                    <td className="py-2 pr-3">
                      {item.employee_name
                        ? <div className="font-medium text-slate-800">{item.employee_name}</div>
                        : <div className="font-medium text-slate-800">Employee {item.hrms_employee_no ?? item.uc01_employee_no ?? ''}
                            <span className="block text-[11px] font-normal text-slate-400">name shown to HR roles only</span></div>}
                      <div className="text-[11px] text-slate-500">
                        {item.hrms_employee_no ? `HRMS employee no. ${item.hrms_employee_no}` : 'HRMS employee no. not resolved yet'}
                        {item.uc01_employee_no && item.uc01_employee_no !== item.hrms_employee_no && ` · UC01 dev no. ${item.uc01_employee_no}`}
                      </div>
                      <div className="text-[11px] text-slate-400">
                        {[item.designation, item.department, item.joining_date && `joins ${item.joining_date}`].filter(Boolean).join(' · ')}
                      </div>
                      <button onClick={() => onOpenCase(item.case_id, stage)}
                        className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-indigo-600 hover:text-indigo-800">
                        {item.case_id} <ExternalLink className="w-3 h-3" />
                      </button>
                    </td>
                    <td className="py-2 pr-3 text-slate-700">
                      {typeLabel(item)}
                      {requestLine(item) && <div className="text-[11px] font-medium text-slate-700">{requestLine(item)}</div>}
                      <div className="text-[11px] text-slate-400">{STAGES[stage]?.title ?? item.stage}</div>
                    </td>
                    <td className="py-2 pr-3 text-slate-700">{roleLabel(item.required_role)}
                      {item.approver_id && <div className="text-[11px] text-slate-500">on record: {item.approver_id}</div>}
                    </td>
                    <td className="py-2 pr-3 text-[11px] text-slate-500">
                      {item.requested_by ?? item.raised_by ?? '—'}<div>{item.requested_at ? fmtTime(item.requested_at) : ''}</div>
                    </td>
                    <td className="py-2 min-w-[220px]">
                      <ApprovalActions caseId={item.case_id} approvalId={item.approval_id} canDecide={item.can_decide}
                        reason={item.reason} compact
                        subject={`the ${typeLabel(item).toLowerCase()} for ${item.employee_name ?? item.case_id}`}
                        effect={item.approval_type === 'INTERNAL_ONBOARDING_FORM'
                          ? 'UC01 then creates the employee record and the workflow continues.' : undefined}
                        onDecided={view => { onDecided(view, item); void refresh() }} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
