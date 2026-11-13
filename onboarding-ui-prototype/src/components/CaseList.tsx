import { AlertTriangle, ListChecks } from 'lucide-react'
import type { CaseSummary } from '../api/onboardingApi'
import { STAGES, TONE_BADGE, currentStageTitle, fmtTime, workflowStatusView } from '../lib/stages'

interface Props {
  cases: CaseSummary[]
  selected: string | null
  onSelect: (caseId: string) => void
}

export default function CaseList({ cases, selected, onSelect }: Props) {
  if (cases.length === 0) {
    return <p className="text-sm text-slate-500 py-4 text-center">No onboarding cases yet for this client. Start one with “Start New Onboarding”.</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-slate-400 border-b border-slate-100">
            <th className="py-2 pr-3 font-medium">Employee</th>
            <th className="py-2 pr-3 font-medium">Department / designation</th>
            <th className="py-2 pr-3 font-medium">Joining</th>
            <th className="py-2 pr-3 font-medium">Current stage</th>
            <th className="py-2 pr-3 font-medium">Status</th>
            <th className="py-2 pr-3 font-medium">Pending / blockers</th>
            <th className="py-2 font-medium">Last updated</th>
          </tr>
        </thead>
        <tbody>
          {cases.map(c => {
            const ws = workflowStatusView(c.workflow_status)
            return (
              <tr key={c.case_id} onClick={() => onSelect(c.case_id)}
                className={`border-b border-slate-50 cursor-pointer hover:bg-indigo-50/50 ${selected === c.case_id ? 'bg-indigo-50' : ''}`}>
                <td className="py-2 pr-3">
                  <div className="font-medium text-slate-800">{c.profile.candidate_name ?? 'Name withheld'}</div>
                  <div className="text-[11px] text-slate-400">{c.hrms_employee_no ? `HRMS ${c.hrms_employee_no}` : 'HRMS no. not resolved'}{c.uc01_employee_no && c.uc01_employee_no !== c.hrms_employee_no ? ` · UC01 dev ${c.uc01_employee_no}` : ''} · {c.case_id}</div>
                </td>
                <td className="py-2 pr-3 text-slate-600">
                  {c.profile.department ?? '—'}<div className="text-[11px] text-slate-400">{c.profile.designation ?? '—'}</div>
                </td>
                <td className="py-2 pr-3 text-slate-600 whitespace-nowrap">{c.joining_date ?? '—'}</td>
                <td className="py-2 pr-3 text-slate-600">
                  {c.form_approval_status && c.form_approval_status !== 'approved' && c.stage_statuses.intake !== 'complete'
                    ? currentStageTitle(c.current_stage, c.form_approval_status, c.stage_statuses.intake)
                    : `${STAGES[c.current_stage]?.uc ?? ''} ${STAGES[c.current_stage]?.title ?? c.current_stage}`}
                </td>
                <td className="py-2 pr-3">
                  <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${TONE_BADGE[ws.tone]}`}>{ws.label}</span>
                </td>
                <td className="py-2 pr-3 text-slate-600 whitespace-nowrap">
                  <span className="inline-flex items-center gap-1"><ListChecks className="w-3 h-3" />{c.pending_action_count}</span>
                  {/* Red only when the case itself is in a red state; a case waiting for a person's review is amber. */}
                  <span className={`inline-flex items-center gap-1 ml-2 ${c.blocker_count ? (ws.tone === 'red' ? 'text-red-600' : 'text-amber-600') : ''}`}>
                    <AlertTriangle className="w-3 h-3" />{c.blocker_count}</span>
                </td>
                <td className="py-2 text-[11px] text-slate-500 whitespace-nowrap">{fmtTime(c.updated_at)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
