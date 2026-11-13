import { useState } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw, AlertTriangle } from 'lucide-react'
import { mockCases } from '../data/mockData'
import StatusBadge, { statusDotColor } from '../components/StatusBadge'
import type { WorkflowStatus } from '../types'

type Filter = 'all' | 'in_progress' | 'complete' | 'blocked' | 'failed' | 'at_risk'

const sequentialStages = ['uc01', 'document_collection', 'document_validation', 'task_planning']
const parallelBranches = {
  tasks: ['task_planning'],
  equipment: ['equipment_access'],
  orientation: ['orientation_scheduling', 'buddy_assignment', 'team_introduction'],
}
const finalStages = ['day1_readiness', 'welcome_message']

function StageCell({ stage, status }: { stage: string; status?: string }) {
  const dot = statusDotColor(status ?? 'not_started')
  return (
    <div className={`flex flex-col items-center gap-1 px-1`}>
      <div className={`w-3 h-3 rounded-full ${dot}`} />
      <span className="text-xs text-slate-500 text-center leading-tight" style={{ fontSize: '10px' }}>
        {stage.replace(/_/g, '\n')}
      </span>
    </div>
  )
}

export default function WorkflowMonitor() {
  const [filter, setFilter] = useState<Filter>('all')

  const filters: { id: Filter; label: string; color: string }[] = [
    { id: 'all', label: 'All', color: 'bg-slate-100 text-slate-700' },
    { id: 'in_progress', label: 'Running', color: 'bg-blue-100 text-blue-700' },
    { id: 'complete', label: 'Completed', color: 'bg-green-100 text-green-700' },
    { id: 'blocked', label: 'Waiting', color: 'bg-amber-100 text-amber-700' },
    { id: 'failed', label: 'Failed', color: 'bg-red-100 text-red-700' },
    { id: 'at_risk', label: 'At Risk', color: 'bg-orange-100 text-orange-700' },
  ]

  function matchesFilter(status: WorkflowStatus): boolean {
    if (filter === 'all') return true
    if (filter === 'in_progress') return status === 'in_progress'
    if (filter === 'complete') return status === 'complete'
    if (filter === 'blocked') return status === 'blocked'
    if (filter === 'failed') return status === 'failed'
    if (filter === 'at_risk') return status === 'at_risk' || status === 'failed'
    return false
  }

  const visible = mockCases.filter((c) => matchesFilter(c.overall_status))

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">Workflow Monitor</h1>
          <p className="text-sm text-slate-500 mt-1">
            Live onboarding pipeline view — <span className="text-amber-600 font-medium">⚠️ Synthetic demonstration data</span>
          </p>
        </div>
        <button className="flex items-center gap-2 text-sm text-indigo-600 hover:text-indigo-800 font-medium border border-indigo-200 px-3 py-1.5 rounded-lg hover:bg-indigo-50 transition-colors">
          <RefreshCw className="w-4 h-4" />
          Refresh
        </button>
      </div>

      {/* Filter buttons */}
      <div className="flex flex-wrap gap-2">
        {filters.map((f) => {
          const count = f.id === 'all' ? mockCases.length : mockCases.filter((c) => matchesFilter(c.overall_status) && f.id !== 'all' && c.overall_status === f.id).length
          return (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all border ${
                filter === f.id
                  ? `${f.color} border-current shadow-sm`
                  : 'bg-white text-slate-500 border-slate-200 hover:border-slate-300'
              }`}
            >
              {f.label}
              {f.id === 'all' && <span className="ml-1.5 text-xs opacity-70">{mockCases.length}</span>}
            </button>
          )
        })}
      </div>

      {/* Case cards */}
      {visible.length === 0 ? (
        <div className="text-center text-slate-400 py-16 text-sm">No cases match the selected filter.</div>
      ) : (
        <div className="space-y-4">
          {visible.map((c) => (
            <div key={c.case_id} className="bg-white rounded-lg border border-slate-200 shadow-sm overflow-hidden">
              {/* Case header */}
              <div className="flex items-center justify-between gap-4 px-5 py-4 border-b border-slate-100">
                <div className="flex items-center gap-3">
                  <div className="w-9 h-9 bg-indigo-100 rounded-full flex items-center justify-center text-sm font-bold text-indigo-700 shrink-0">
                    {c.employee_name.split(' ').map((n) => n[0]).join('')}
                  </div>
                  <div>
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-semibold text-slate-900">{c.employee_name}</span>
                      <span className="text-xs text-slate-400 font-mono">{c.case_id}</span>
                      <StatusBadge status={c.overall_status} size="xs" />
                      {c.pending_approvals.length > 0 && (
                        <span className="text-xs bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded-full font-medium">
                          {c.pending_approvals.length} approval{c.pending_approvals.length > 1 ? 's' : ''} pending
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-slate-500 mt-0.5">
                      {c.job_title} · {c.department} · {c.location} · Joining: {c.joining_date}
                    </p>
                  </div>
                </div>
                <Link
                  to={`/demo/cases/${c.case_id}`}
                  className="text-xs font-medium text-indigo-600 hover:text-indigo-800 shrink-0"
                >
                  View Workflow →
                </Link>
              </div>

              {/* Stage pipeline */}
              <div className="px-5 py-4">
                {/* Sequential stages */}
                <div className="mb-3">
                  <p className="text-xs text-slate-400 font-semibold uppercase tracking-wider mb-2">Sequential</p>
                  <div className="flex items-center gap-1 overflow-x-auto pb-1">
                    {sequentialStages.map((stage, idx) => (
                      <div key={stage} className="flex items-center">
                        <StageCell stage={stage} status={c.stage_statuses[stage]} />
                        {idx < sequentialStages.length - 1 && (
                          <div className="w-6 h-0.5 bg-slate-200 mx-1 shrink-0" />
                        )}
                      </div>
                    ))}
                    <div className="text-xs text-teal-600 font-bold ml-2 shrink-0">→ Parallel</div>
                  </div>
                </div>

                {/* Parallel branches */}
                <div className="grid grid-cols-3 gap-3 border border-dashed border-teal-200 rounded-lg p-3 bg-teal-50/40 mb-3">
                  {Object.entries(parallelBranches).map(([branch, stages]) => (
                    <div key={branch}>
                      <p className="text-xs font-semibold text-teal-700 mb-1 capitalize">{branch}</p>
                      <div className="flex flex-col gap-1">
                        {stages.map((stage) => (
                          <div key={stage} className="flex items-center gap-1.5">
                            <div className={`w-2 h-2 rounded-full shrink-0 ${statusDotColor(c.stage_statuses[stage] ?? 'not_started')}`} />
                            <span className="text-xs text-slate-600 truncate">{stage.replace(/_/g, ' ')}</span>
                            <StatusBadge status={c.stage_statuses[stage] ?? 'not_started'} size="xs" />
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>

                {/* Final stages */}
                <div>
                  <p className="text-xs text-slate-400 font-semibold uppercase tracking-wider mb-2">Final</p>
                  <div className="flex items-center gap-1">
                    {finalStages.map((stage, idx) => (
                      <div key={stage} className="flex items-center">
                        <StageCell stage={stage} status={c.stage_statuses[stage]} />
                        {idx < finalStages.length - 1 && (
                          <div className="w-6 h-0.5 bg-slate-200 mx-1 shrink-0" />
                        )}
                      </div>
                    ))}
                  </div>
                </div>

                {/* Error / next action */}
                {Object.keys(c.stage_errors).length > 0 && (
                  <div className="mt-3 flex items-start gap-2 p-2 bg-red-50 border border-red-200 rounded">
                    <AlertTriangle className="w-3.5 h-3.5 text-red-500 shrink-0 mt-0.5" />
                    <div>
                      {Object.entries(c.stage_errors).map(([stage, err]) => (
                        <p key={stage} className="text-xs text-red-700">
                          <span className="font-semibold">{stage.replace(/_/g, ' ')}:</span> {err.code} — {err.detail}
                        </p>
                      ))}
                    </div>
                  </div>
                )}

                <div className="mt-2 text-xs text-slate-500">
                  <span className="font-medium">Next action:</span> {c.next_action}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
