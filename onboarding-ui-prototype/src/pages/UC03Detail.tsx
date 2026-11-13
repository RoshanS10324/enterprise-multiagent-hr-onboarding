import { useNavigate } from 'react-router-dom'
import { ArrowLeft, CheckCircle, XCircle, AlertTriangle, ArrowRight } from 'lucide-react'
import { mockAgentExecutions } from '../data/mockData'
import StatusBadge from '../components/StatusBadge'
import StepTimeline from '../components/StepTimeline'
import ExpandableJSON from '../components/ExpandableJSON'

const docResults = [
  { name: 'Passport', status: 'VERIFIED', note: 'Valid until 2029-05-12 · Singapore · MRZ OK', case: 'CASE-001' },
  { name: 'Emirates ID', status: 'VERIFIED', note: 'ICA verified · Valid until 2027-08-31', case: 'CASE-001' },
  { name: 'Offer Letter', status: 'VERIFIED', note: 'Name/date/signatory all matched', case: 'CASE-001' },
  { name: 'Educational Certificate', status: 'VERIFIED', note: 'NUS · MOHESR attestation valid', case: 'CASE-001' },
  { name: 'NOC Letter', status: 'VERIFIED', note: 'TechCorp Singapore · NOC date 2026-10-10', case: 'CASE-001' },
]

const outcomeRoutes = [
  { outcome: 'All verified', action: 'Proceed to UC02 Task Planning (sequential barrier)', color: 'text-green-700', bg: 'bg-green-50', border: 'border-green-200' },
  { outcome: 'Some missing / invalid', action: 'Route back to UC02 Document Collection — notify employee to resubmit', color: 'text-amber-700', bg: 'bg-amber-50', border: 'border-amber-200' },
  { outcome: 'Critical failure', action: 'Block workflow — alert HR admin for manual intervention', color: 'text-red-700', bg: 'bg-red-50', border: 'border-red-200' },
]

export default function UC03Detail() {
  const navigate = useNavigate()
  const exec = mockAgentExecutions['uc03']

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
              <span className="text-sm font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">UC03</span>
              <StatusBadge status={exec.status} size="md" />
            </div>
            <h1 className="text-xl font-bold text-slate-900">UC03 — Document Validation</h1>
            <p className="text-sm text-slate-500 mt-1">{exec.description}</p>
          </div>
          <div className="text-xs text-amber-600 bg-amber-50 border border-amber-200 px-3 py-1 rounded-full font-medium shrink-0">
            ⚠️ Synthetic data — CASE-001 (Sarah Chen)
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4 pt-4 border-t border-slate-100">
          <div>
            <p className="text-xs text-slate-400">Documents Checked</p>
            <p className="text-2xl font-bold text-green-600">5</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Verified</p>
            <p className="text-2xl font-bold text-green-600">5</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Rejected</p>
            <p className="text-2xl font-bold text-red-500">0</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Duration</p>
            <p className="text-2xl font-bold text-slate-700">{exec.duration}</p>
          </div>
        </div>
      </div>

      {/* Routing decision */}
      <div className="bg-green-50 border border-green-200 rounded-lg p-4 flex items-center gap-3">
        <CheckCircle className="w-5 h-5 text-green-600 shrink-0" />
        <div>
          <p className="text-sm font-semibold text-green-800">Routing Decision: All Documents Verified</p>
          <p className="text-sm text-green-700 mt-0.5">
            Proceeding to UC02 Task Planning (sequential barrier). Parallel fan-out to UC04 and UC05 will follow.
          </p>
        </div>
        <ArrowRight className="w-5 h-5 text-green-600 ml-auto shrink-0" />
      </div>

      {/* CASE-002 context */}
      <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3">
        <XCircle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-semibold text-red-800">CASE-002 (James Okafor) — Validation Blocked</p>
          <p className="text-sm text-red-700 mt-0.5">
            In a different scenario: passport not submitted; Emirates ID address page unreadable. UC03 routed back to UC02 Document Collection. Employee notified to resubmit.
          </p>
          <p className="text-xs text-red-500 mt-1">Error: MISSING_REQUIRED_DOCUMENT · Retryable: true</p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        {/* Validation results */}
        <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
          <h2 className="text-base font-semibold text-slate-900 mb-4">Validation Results (CASE-001)</h2>
          <div className="space-y-3">
            {docResults.map((doc) => (
              <div key={doc.name} className="flex items-start gap-3 p-3 bg-green-50 border border-green-100 rounded-lg">
                <CheckCircle className="w-4 h-4 text-green-600 shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-semibold text-slate-800">{doc.name}</p>
                  <p className="text-xs text-slate-500 mt-0.5">{doc.note}</p>
                </div>
                <StatusBadge status="complete" size="xs" />
              </div>
            ))}
          </div>
        </div>

        {/* Processing steps */}
        <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
          <h2 className="text-base font-semibold text-slate-900 mb-4">Processing Steps</h2>
          <StepTimeline steps={exec.steps} />
        </div>
      </div>

      {/* Routing outcomes legend */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        <h2 className="text-base font-semibold text-slate-900 mb-4">Routing Outcomes</h2>
        <div className="flex items-center gap-2 mb-3 text-xs text-slate-500">
          <AlertTriangle className="w-4 h-4" />
          UC03 routes the workflow to one of three outcomes based on validation results:
        </div>
        <div className="space-y-2">
          {outcomeRoutes.map((r) => (
            <div key={r.outcome} className={`flex items-start gap-3 p-3 rounded-lg border ${r.bg} ${r.border}`}>
              <ArrowRight className={`w-4 h-4 shrink-0 mt-0.5 ${r.color}`} />
              <div>
                <p className={`text-sm font-semibold ${r.color}`}>{r.outcome}</p>
                <p className={`text-xs mt-0.5 ${r.color} opacity-80`}>{r.action}</p>
              </div>
            </div>
          ))}
        </div>
        <ExpandableJSON data={exec.output_summary} label="View full validation output JSON" />
      </div>

      {/* History */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        <h2 className="text-base font-semibold text-slate-900 mb-4">Execution History</h2>
        <div className="space-y-2">
          {exec.history.map((h, i) => (
            <div key={i} className="flex items-start gap-4 py-2 border-b border-slate-50 last:border-0">
              <span className="text-xs text-slate-400 whitespace-nowrap font-mono">
                {new Date(h.timestamp).toLocaleTimeString()}
              </span>
              <StatusBadge status={h.status} size="xs" />
              <span className="text-sm text-slate-700">{h.note}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
