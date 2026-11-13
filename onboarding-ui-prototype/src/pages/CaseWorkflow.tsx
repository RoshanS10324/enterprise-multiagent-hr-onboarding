import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { ArrowLeft, AlertTriangle, CheckCircle, Clock, XCircle, User, MapPin, Briefcase, Calendar } from 'lucide-react'
import { getMockCase, mockAgentExecutions } from '../data/mockData'
import StatusBadge from '../components/StatusBadge'

import StepTimeline from '../components/StepTimeline'
import ExpandableJSON from '../components/ExpandableJSON'
import type { StageStatus } from '../types'

const stageToExec: Record<string, string> = {
  uc01: 'uc01',
  document_collection: 'uc02',
  document_validation: 'uc03',
  task_planning: 'uc02',
  tasks_complete: 'uc02',
  equipment_access: 'uc04',
  orientation_scheduling: 'uc05',
  buddy_assignment: 'uc05',
  team_introduction: 'uc05',
  welcome_message: 'uc05',
  day1_readiness: 'uc05',
  evaluate: 'uc05',
}

export default function CaseWorkflow() {
  const { caseId } = useParams<{ caseId: string }>()
  const c = getMockCase(caseId ?? '')
  const [selectedNode, setSelectedNode] = useState<string | null>(null)

  if (!c) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-slate-400">
        <XCircle className="w-10 h-10 mb-4 text-red-400" />
        <h2 className="text-xl font-semibold text-slate-600">Case not found</h2>
        <p className="text-sm mt-2">Case ID "{caseId}" does not exist in the system.</p>
        <Link to="/demo/cases" className="mt-4 text-indigo-600 hover:underline text-sm font-medium">
          ← Back to Onboarding Cases
        </Link>
      </div>
    )
  }

  const execKey = selectedNode ? stageToExec[selectedNode] : null
  const exec = execKey ? mockAgentExecutions[execKey] : null

  const statusIconMap: Record<string, React.ReactNode> = {
    complete: <CheckCircle className="w-4 h-4 text-green-500" />,
    failed: <XCircle className="w-4 h-4 text-red-500" />,
    blocked: <XCircle className="w-4 h-4 text-red-400" />,
    awaiting_approval: <Clock className="w-4 h-4 text-amber-500" />,
    waiting: <Clock className="w-4 h-4 text-amber-500" />,
    in_progress: <Clock className="w-4 h-4 text-blue-500" />,
  }

  return (
    <div className="space-y-5">
      {/* Back link */}
      <Link to="/demo/cases" className="inline-flex items-center gap-1 text-sm text-indigo-600 hover:text-indigo-800 font-medium">
        <ArrowLeft className="w-4 h-4" /> Back to Onboarding Cases
      </Link>

      {/* Case header */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 bg-indigo-100 rounded-full flex items-center justify-center text-lg font-bold text-indigo-700">
              {c.employee_name.split(' ').map((n) => n[0]).join('')}
            </div>
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <h1 className="text-xl font-bold text-slate-900">{c.employee_name}</h1>
                <StatusBadge status={c.overall_status} size="md" />
              </div>
              <p className="text-sm text-slate-500 mt-0.5 font-mono">{c.case_id}</p>
            </div>
          </div>
          <div className="text-xs text-amber-600 bg-amber-50 border border-amber-200 px-3 py-1 rounded-full font-medium">
            ⚠️ Synthetic demonstration data
          </div>
        </div>

        {/* Case meta */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-5 pt-5 border-t border-slate-100">
          <div className="flex items-center gap-2">
            <Briefcase className="w-4 h-4 text-slate-400" />
            <div>
              <p className="text-xs text-slate-400">Job Title</p>
              <p className="text-sm font-medium text-slate-800">{c.job_title}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <User className="w-4 h-4 text-slate-400" />
            <div>
              <p className="text-xs text-slate-400">Department</p>
              <p className="text-sm font-medium text-slate-800">{c.department}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <MapPin className="w-4 h-4 text-slate-400" />
            <div>
              <p className="text-xs text-slate-400">Location</p>
              <p className="text-sm font-medium text-slate-800">{c.location}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Calendar className="w-4 h-4 text-slate-400" />
            <div>
              <p className="text-xs text-slate-400">Joining Date</p>
              <p className="text-sm font-medium text-slate-800">{c.joining_date}</p>
            </div>
          </div>
        </div>

        {/* Stage statuses mini grid */}
        <div className="mt-4 flex flex-wrap gap-2">
          {Object.entries(c.stage_statuses).map(([stage, status]) => (
            <div key={stage} className="flex items-center gap-1.5 text-xs">
              {statusIconMap[status] ?? <div className="w-4 h-4 rounded-full bg-slate-200" />}
              <span className="text-slate-600 font-mono">{stage.replace(/_/g, ' ')}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Errors */}
      {Object.entries(c.stage_errors).map(([stage, err]) => (
        <div key={stage} className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-red-800">
              Error in {stage.replace(/_/g, ' ')}: {err.code}
            </p>
            <p className="text-sm text-red-700 mt-1">{err.detail}</p>
            <p className="text-xs text-red-500 mt-1">Retryable: {err.retryable ? 'Yes' : 'No (manual intervention required)'}</p>
          </div>
        </div>
      ))}

      {/* Pending approvals */}
      {c.pending_approvals.length > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
          <p className="text-sm font-semibold text-amber-800 mb-2">
            {c.pending_approvals.length} Pending Approval{c.pending_approvals.length > 1 ? 's' : ''}
          </p>
          {c.pending_approvals.map((ap) => (
            <div key={ap.id} className="flex items-start gap-3 mt-2">
              <Clock className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-amber-800">{ap.description}</p>
                <p className="text-xs text-amber-600 mt-0.5">
                  Stage: {ap.stage} · Gate: {ap.gate} · Role: {ap.role} · Ref: {ap.id}
                </p>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Two-column: diagram + detail panel */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-5">
        {/* Workflow diagram */}
        <div className="lg:col-span-3 bg-white rounded-lg border border-slate-200 shadow-sm p-5">
          <h2 className="text-base font-semibold text-slate-900 mb-4">Workflow Progress</h2>
          <p className="text-xs text-slate-400 mb-4">Click a node to see agent execution detail →</p>
          {/* Demo page: the live workflow diagram is on the dashboard (it needs a real CaseView). */}
          <div className="space-y-2">
            {Object.entries(c.stage_statuses as Record<string, StageStatus>).map(([id, status]) => (
              <button key={id} onClick={() => setSelectedNode(selectedNode === id ? null : id)}
                className={`w-full flex justify-between rounded border px-3 py-2 text-xs ${selectedNode === id ? 'border-indigo-400 bg-indigo-50' : 'border-slate-200'}`}>
                <span className="font-medium text-slate-700">{id}</span><span className="text-slate-500">{status}</span>
              </button>
            ))}
          </div>
        </div>

        {/* Detail panel */}
        <div className="lg:col-span-2">
          {selectedNode && exec ? (
            <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5 space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <span className="text-xs font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">
                    {exec.uc_number}
                  </span>
                  <h3 className="text-sm font-semibold text-slate-900 mt-1">{exec.agent_name}</h3>
                </div>
                <StatusBadge status={exec.status} size="xs" />
              </div>
              <p className="text-xs text-slate-500">{exec.description}</p>

              <div className="grid grid-cols-2 gap-3 text-xs">
                <div>
                  <span className="text-slate-400">Duration</span>
                  <p className="font-semibold text-slate-700">{exec.duration ?? '—'}</p>
                </div>
                <div>
                  <span className="text-slate-400">Steps</span>
                  <p className="font-semibold text-slate-700">{exec.steps.length}</p>
                </div>
              </div>

              <div>
                <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">Steps</p>
                <StepTimeline steps={exec.steps.slice(0, 3)} />
              </div>

              <div className="border-t border-slate-100 pt-3">
                <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">Output Summary</p>
                <div className="space-y-1">
                  {Object.entries(exec.output_summary).slice(0, 4).map(([k, v]) => (
                    <div key={k} className="flex justify-between text-xs gap-3">
                      <span className="text-slate-400">{k}</span>
                      <span className="text-slate-800 font-medium text-right">{String(v)}</span>
                    </div>
                  ))}
                </div>
                <ExpandableJSON data={exec.output_summary} label="View full output" />
              </div>

              <Link
                to={`/demo/agents/${exec.uc_number.toLowerCase()}`}
                className="block text-center text-xs font-medium text-indigo-600 hover:text-indigo-800 border border-indigo-200 rounded-lg py-2 hover:bg-indigo-50 transition-colors"
              >
                Open full {exec.uc_number} detail page →
              </Link>
            </div>
          ) : (
            <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5 h-64 flex items-center justify-center">
              <div className="text-center text-slate-400">
                <div className="text-3xl mb-2">←</div>
                <p className="text-sm">Click a workflow node to see agent execution details</p>
              </div>
            </div>
          )}

          {/* Case summary card */}
          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5 mt-4">
            <h3 className="text-sm font-semibold text-slate-900 mb-3">Next Action</h3>
            <p className="text-sm text-slate-600">{c.next_action}</p>
            {c.documents && (
              <div className="mt-3 pt-3 border-t border-slate-100">
                <p className="text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1">Documents</p>
                <div className="text-xs space-y-0.5">
                  <div className="flex justify-between">
                    <span className="text-slate-500">Submitted</span>
                    <span className="text-green-600 font-medium">{c.documents.submitted.length}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">Pending</span>
                    <span className={c.documents.pending.length > 0 ? 'text-amber-600 font-medium' : 'text-slate-400'}>{c.documents.pending.length}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-500">Invalid</span>
                    <span className={c.documents.invalid.length > 0 ? 'text-red-600 font-medium' : 'text-slate-400'}>{c.documents.invalid.length}</span>
                  </div>
                </div>
              </div>
            )}
            {c.tasks && (
              <div className="mt-3 pt-3 border-t border-slate-100">
                <p className="text-xs text-slate-500 font-semibold uppercase tracking-wider mb-1">Tasks</p>
                <div className="flex items-center gap-2">
                  <div className="flex-1 bg-slate-100 rounded-full h-2 overflow-hidden">
                    <div
                      className="bg-green-500 h-2 rounded-full"
                      style={{ width: `${(c.tasks.completed / c.tasks.total) * 100}%` }}
                    />
                  </div>
                  <span className="text-xs font-medium text-slate-600">
                    {c.tasks.completed}/{c.tasks.total}
                  </span>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
