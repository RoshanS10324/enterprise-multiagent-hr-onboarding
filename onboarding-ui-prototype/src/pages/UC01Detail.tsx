import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { mockAgentExecutions } from '../data/mockData'
import StatusBadge from '../components/StatusBadge'
import StepTimeline from '../components/StepTimeline'
import InputPanel from '../components/InputPanel'
import OutputPanel from '../components/OutputPanel'
import ExpandableJSON from '../components/ExpandableJSON'

type Tab = 'steps' | 'input' | 'output' | 'history'

export default function UC01Detail() {
  const navigate = useNavigate()
  const exec = mockAgentExecutions['uc01']
  const [tab, setTab] = useState<Tab>('steps')

  const tabs: { id: Tab; label: string }[] = [
    { id: 'steps', label: 'Processing Steps' },
    { id: 'input', label: 'Input' },
    { id: 'output', label: 'Output' },
    { id: 'history', label: 'History' },
  ]

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
              <span className="text-sm font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">UC01</span>
              <StatusBadge status={exec.status} size="md" />
            </div>
            <h1 className="text-xl font-bold text-slate-900">UC01 — Employee Record Creation</h1>
            <p className="text-sm text-slate-500 mt-1">{exec.description}</p>
          </div>
          <div className="text-xs text-amber-600 bg-amber-50 border border-amber-200 px-3 py-1 rounded-full font-medium shrink-0">
            ⚠️ Synthetic data — CASE-001 (Sarah Chen)
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4 pt-4 border-t border-slate-100">
          <div>
            <p className="text-xs text-slate-400">Execution ID</p>
            <p className="text-sm font-mono text-slate-700">{exec.execution_id}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Case</p>
            <p className="text-sm font-medium text-slate-700">{exec.case_id}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Duration</p>
            <p className="text-sm font-medium text-slate-700">{exec.duration}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Completed</p>
            <p className="text-sm font-medium text-slate-700">{exec.end_time ? new Date(exec.end_time).toLocaleString() : '—'}</p>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-slate-100 p-1 rounded-lg w-fit">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-sm font-medium rounded-md transition-colors ${
              tab === t.id ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        {tab === 'steps' && (
          <div>
            <h2 className="text-base font-semibold text-slate-900 mb-4">Processing Steps ({exec.steps.length})</h2>
            <StepTimeline steps={exec.steps} />
          </div>
        )}

        {tab === 'input' && (
          <div>
            <h2 className="text-base font-semibold text-slate-900 mb-4">Agent Input</h2>
            <InputPanel
              groups={[
                {
                  title: 'Trigger',
                  fields: [
                    { label: 'Trigger Event', value: 'new_hire_offer_accepted' },
                    { label: 'Offer Reference', value: 'OFFER-2026-ENG-0041' },
                  ],
                },
                {
                  title: 'Employee Details',
                  fields: [
                    { label: 'Name', value: 'Sarah Chen' },
                    { label: 'Job Title', value: 'Software Engineer' },
                    { label: 'Department', value: 'Engineering' },
                    { label: 'Location', value: 'Dubai' },
                    { label: 'Joining Date', value: '2026-11-15' },
                    { label: 'Contract Type', value: 'full_time' },
                  ],
                },
                {
                  title: 'Organisational',
                  fields: [
                    { label: 'Hiring Manager', value: 'David Park' },
                    { label: 'Client ID', value: 'CLIENT-ACME-CORP' },
                  ],
                },
              ]}
              rawData={exec.input_summary}
            />
          </div>
        )}

        {tab === 'output' && (
          <div>
            <h2 className="text-base font-semibold text-slate-900 mb-4">Agent Output</h2>
            <OutputPanel
              fields={[
                { label: 'Case ID Created', value: 'CASE-001' },
                { label: 'Employee ID', value: 'EMP-001' },
                { label: 'HRMS Reference', value: 'HRMS-2026-11234' },
                { label: 'Record Created', value: true },
                { label: 'Documents Required', value: '5 documents' },
                { label: 'Next Stage', value: 'document_collection' },
              ]}
              downstream="UC02 Document Collection triggered automatically"
              rawData={exec.output_summary}
            />
          </div>
        )}

        {tab === 'history' && (
          <div>
            <h2 className="text-base font-semibold text-slate-900 mb-4">Execution History</h2>
            <div className="space-y-2">
              {exec.history.map((h, i) => (
                <div key={i} className="flex items-start gap-4 py-2 border-b border-slate-50 last:border-0">
                  <span className="text-xs text-slate-400 whitespace-nowrap font-mono">
                    {new Date(h.timestamp).toLocaleTimeString()}
                  </span>
                  <span className="text-xs text-slate-400 whitespace-nowrap">
                    {new Date(h.timestamp).toLocaleDateString()}
                  </span>
                  <StatusBadge status={h.status} size="xs" />
                  <span className="text-sm text-slate-700">{h.note}</span>
                </div>
              ))}
            </div>
            <ExpandableJSON data={exec.history} label="View history JSON" />
          </div>
        )}
      </div>
    </div>
  )
}
