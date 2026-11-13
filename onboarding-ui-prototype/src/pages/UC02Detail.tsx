import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { mockAgentExecutions } from '../data/mockData'
import StatusBadge from '../components/StatusBadge'
import StepTimeline from '../components/StepTimeline'
import InputPanel from '../components/InputPanel'
import OutputPanel from '../components/OutputPanel'
import ExpandableJSON from '../components/ExpandableJSON'

type Activity = 'doc_collection' | 'task_planning'

export default function UC02Detail() {
  const navigate = useNavigate()
  const exec = mockAgentExecutions['uc02']
  const [activity, setActivity] = useState<Activity>('doc_collection')

  return (
    <div className="space-y-5">
      <button onClick={() => navigate(-1)} className="inline-flex items-center gap-1 text-sm text-indigo-600 hover:text-indigo-800 font-medium">
        <ArrowLeft className="w-4 h-4" /> Back to Workflow
      </button>

      {/* Header */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-sm font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">UC02</span>
              <StatusBadge status={exec.status} size="md" />
            </div>
            <h1 className="text-xl font-bold text-slate-900">UC02 — Document Collection & Task Planning</h1>
            <p className="text-sm text-slate-500 mt-1">{exec.description}</p>
          </div>
          <div className="text-xs text-amber-600 bg-amber-50 border border-amber-200 px-3 py-1 rounded-full font-medium shrink-0">
            ⚠️ Synthetic data — CASE-001 (Sarah Chen)
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4 mt-4 pt-4 border-t border-slate-100">
          <div>
            <p className="text-xs text-slate-400">Execution ID</p>
            <p className="text-sm font-mono text-slate-700">{exec.execution_id}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Total Duration</p>
            <p className="text-sm font-medium text-slate-700">{exec.duration}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Activities</p>
            <p className="text-sm font-medium text-slate-700">2 (sequential)</p>
          </div>
        </div>
      </div>

      {/* Activity tabs */}
      <div className="flex gap-3">
        <button
          onClick={() => setActivity('doc_collection')}
          className={`flex-1 md:flex-none px-5 py-3 rounded-lg border text-sm font-medium transition-all ${
            activity === 'doc_collection'
              ? 'bg-indigo-600 text-white border-indigo-600 shadow-sm'
              : 'bg-white text-slate-600 border-slate-200 hover:border-indigo-300'
          }`}
        >
          Activity A: Document Collection
        </button>
        <button
          onClick={() => setActivity('task_planning')}
          className={`flex-1 md:flex-none px-5 py-3 rounded-lg border text-sm font-medium transition-all ${
            activity === 'task_planning'
              ? 'bg-indigo-600 text-white border-indigo-600 shadow-sm'
              : 'bg-white text-slate-600 border-slate-200 hover:border-indigo-300'
          }`}
        >
          Activity B: Task Planning
        </button>
      </div>

      {/* Activity A: Document Collection */}
      {activity === 'doc_collection' && (
        <div className="space-y-5">
          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs font-bold text-teal-600 bg-teal-50 px-2 py-0.5 rounded">Activity A</span>
              <StatusBadge status="complete" size="xs" />
            </div>
            <h2 className="text-base font-semibold text-slate-900">Document Collection</h2>
            <p className="text-sm text-slate-500 mt-1">
              Sends document submission portal link to employee, monitors upload progress, and triggers UC03 when all documents are received.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">Input</h3>
              <InputPanel
                groups={[
                  {
                    title: 'Case Context',
                    fields: [
                      { label: 'Case ID', value: 'CASE-001' },
                      { label: 'Employee', value: 'Sarah Chen' },
                      { label: 'Employee Email', value: 's.chen@example.com' },
                      { label: 'Document Count Required', value: 5 },
                      { label: 'Submission Deadline', value: '2026-11-01' },
                    ],
                  },
                ]}
              />
            </div>
            <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">Output</h3>
              <OutputPanel
                fields={[
                  { label: 'Documents Received', value: 5 },
                  { label: 'All Documents Submitted', value: true },
                  { label: 'Final Submission', value: '2026-10-24' },
                  { label: 'UC03 Triggered', value: true },
                ]}
                downstream="UC03 Document Validation triggered automatically"
              />
            </div>
          </div>

          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-slate-900 mb-4">Processing Steps</h3>
            <StepTimeline steps={exec.steps.slice(0, 2)} />
          </div>

          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-slate-900 mb-3">Documents Received</h3>
            <div className="space-y-2">
              {[
                { name: 'passport', received: '2026-10-22', status: 'verified' },
                { name: 'emirates_id', received: '2026-10-21', status: 'verified' },
                { name: 'offer_letter', received: '2026-10-21', status: 'verified' },
                { name: 'educational_certificate', received: '2026-10-23', status: 'verified' },
                { name: 'noc_letter', received: '2026-10-24', status: 'verified' },
              ].map((doc) => (
                <div key={doc.name} className="flex items-center justify-between py-2 border-b border-slate-50 last:border-0">
                  <span className="text-sm font-mono text-slate-700">{doc.name.replace(/_/g, ' ')}</span>
                  <div className="flex items-center gap-3">
                    <span className="text-xs text-slate-400">{doc.received}</span>
                    <StatusBadge status={doc.status} size="xs" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Activity B: Task Planning */}
      {activity === 'task_planning' && (
        <div className="space-y-5">
          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs font-bold text-orange-600 bg-orange-50 px-2 py-0.5 rounded">Activity B</span>
              <span className="text-xs bg-orange-100 text-orange-700 px-1.5 py-0.5 rounded font-medium">sequential barrier</span>
              <StatusBadge status="complete" size="xs" />
            </div>
            <h2 className="text-base font-semibold text-slate-900">Onboarding Task Planning</h2>
            <p className="text-sm text-slate-500 mt-1">
              Generates a structured task plan with assigned owners and due dates after UC03 validation clears. This is the sequential barrier — parallel fan-out begins after this step.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">Input</h3>
              <InputPanel
                groups={[
                  {
                    title: 'Validation Result',
                    fields: [
                      { label: 'UC03 Outcome', value: 'all_verified' },
                      { label: 'Employee Role', value: 'Software Engineer' },
                      { label: 'Department', value: 'Engineering' },
                      { label: 'Joining Date', value: '2026-11-15' },
                    ],
                  },
                ]}
              />
            </div>
            <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
              <h3 className="text-sm font-semibold text-slate-900 mb-3">Output</h3>
              <OutputPanel
                fields={[
                  { label: 'Total Tasks', value: 12 },
                  { label: 'HR Tasks', value: 4 },
                  { label: 'IT Tasks', value: 3 },
                  { label: 'Employee Tasks', value: 3 },
                  { label: 'Manager Tasks', value: 2 },
                  { label: 'All Owners Notified', value: true },
                  { label: 'UC04 & UC05 Triggered', value: true },
                ]}
                downstream="Parallel fan-out: UC04 Equipment & UC05 Orientation triggered simultaneously"
              />
            </div>
          </div>

          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-slate-900 mb-4">Processing Steps</h3>
            <StepTimeline steps={exec.steps.slice(2)} />
          </div>

          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-slate-900 mb-3">Generated Task Plan (12 tasks)</h3>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-slate-100">
                    {['#', 'Task', 'Owner', 'Due Date', 'Status'].map((h) => (
                      <th key={h} className="text-left text-xs font-semibold text-slate-500 px-3 py-2">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {[
                    { id: 1, title: 'Complete HR forms', owner: 'HR Admin', due: '2026-11-10', status: 'complete' },
                    { id: 2, title: 'Sign employment contract', owner: 'Sarah Chen', due: '2026-11-10', status: 'complete' },
                    { id: 3, title: 'Submit medical clearance', owner: 'Sarah Chen', due: '2026-11-12', status: 'complete' },
                    { id: 4, title: 'Setup IT accounts', owner: 'IT Department', due: '2026-11-13', status: 'complete' },
                    { id: 5, title: 'Collect laptop from IT', owner: 'Sarah Chen', due: '2026-11-15', status: 'complete' },
                    { id: 6, title: 'Complete security training', owner: 'Sarah Chen', due: '2026-11-14', status: 'complete' },
                    { id: 7, title: 'Team introduction meeting', owner: 'Team Lead', due: '2026-11-15', status: 'complete' },
                    { id: 8, title: 'Office orientation tour', owner: 'HR Admin', due: '2026-11-15', status: 'complete' },
                    { id: 9, title: 'Benefits enrollment', owner: 'Sarah Chen', due: '2026-11-14', status: 'complete' },
                    { id: 10, title: 'Emergency contact form', owner: 'Sarah Chen', due: '2026-11-11', status: 'complete' },
                    { id: 11, title: 'Probation plan review', owner: 'Manager', due: '2026-11-15', status: 'complete' },
                    { id: 12, title: 'First day check-in', owner: 'HR Admin', due: '2026-11-15', status: 'complete' },
                  ].map((t) => (
                    <tr key={t.id} className="border-b border-slate-50 hover:bg-slate-50">
                      <td className="px-3 py-2 text-slate-400">{t.id}</td>
                      <td className="px-3 py-2 text-slate-700">{t.title}</td>
                      <td className="px-3 py-2 text-slate-500">{t.owner}</td>
                      <td className="px-3 py-2 text-slate-500">{t.due}</td>
                      <td className="px-3 py-2">
                        <StatusBadge status={t.status} size="xs" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ExpandableJSON data={{ tasks: exec.output_summary }} label="View full task plan JSON" />
          </div>
        </div>
      )}
    </div>
  )
}
