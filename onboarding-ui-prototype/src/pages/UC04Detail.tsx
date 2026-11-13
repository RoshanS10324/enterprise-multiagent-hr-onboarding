import { useNavigate } from 'react-router-dom'
import { ArrowLeft, AlertTriangle, Package, Shield } from 'lucide-react'
import { mockAgentExecutions, mockCases } from '../data/mockData'
import StatusBadge from '../components/StatusBadge'
import StepTimeline from '../components/StepTimeline'
import ExpandableJSON from '../components/ExpandableJSON'

export default function UC04Detail() {
  const navigate = useNavigate()
  const exec = mockAgentExecutions['uc04']
  const case001 = mockCases[0]
  const case005 = mockCases[4]

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
              <span className="text-sm font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">UC04</span>
              <StatusBadge status={exec.status} size="md" />
              <span className="text-xs bg-teal-100 text-teal-700 px-2 py-0.5 rounded font-medium">parallel branch</span>
            </div>
            <h1 className="text-xl font-bold text-slate-900">UC04 — Equipment & Access Provisioning</h1>
            <p className="text-sm text-slate-500 mt-1">{exec.description}</p>
          </div>
          <div className="text-xs text-amber-600 bg-amber-50 border border-amber-200 px-3 py-1 rounded-full font-medium shrink-0">
            ⚠️ Synthetic data
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4 pt-4 border-t border-slate-100">
          <div>
            <p className="text-xs text-slate-400">Items Provisioned</p>
            <p className="text-2xl font-bold text-green-600">3</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">System Access</p>
            <p className="text-2xl font-bold text-green-600">5</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Duration</p>
            <p className="text-lg font-bold text-slate-700">{exec.duration}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Case</p>
            <p className="text-lg font-bold text-slate-700">{exec.case_id}</p>
          </div>
        </div>
      </div>

      {/* CASE-005 failure alert */}
      <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3">
        <AlertTriangle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-semibold text-red-800">
            CASE-005 ({case005.employee_name}) — Equipment Provisioning Failed
          </p>
          <p className="text-sm text-red-700 mt-1">
            {case005.stage_errors.equipment_access?.detail}
          </p>
          <p className="text-xs text-red-500 mt-1">
            Error: {case005.stage_errors.equipment_access?.code} · Retryable: {case005.stage_errors.equipment_access?.retryable ? 'Yes' : 'No — manual IT intervention required'}
          </p>
          <div className="mt-2 flex gap-2 flex-wrap">
            {case005.equipment?.items.map((item) => (
              <span key={item.name} className="text-xs bg-red-100 text-red-700 px-2 py-0.5 rounded">
                {item.name}: {item.status}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        {/* Equipment list (CASE-001) */}
        <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
          <div className="flex items-center gap-2 mb-4">
            <Package className="w-4 h-4 text-indigo-600" />
            <h2 className="text-base font-semibold text-slate-900">Physical Equipment — CASE-001</h2>
          </div>
          <div className="space-y-2">
            {case001.equipment?.items.map((item) => (
              <div key={item.name} className="flex items-center justify-between py-2 border-b border-slate-50 last:border-0">
                <span className="text-sm text-slate-700">{item.name}</span>
                <StatusBadge status={item.status === 'provisioned' ? 'complete' : item.status} size="xs" />
              </div>
            ))}
          </div>
          <div className="mt-4 pt-4 border-t border-slate-100">
            <p className="text-xs text-slate-500 font-semibold uppercase tracking-wider mb-2">Provisioning Timeline</p>
            <div className="text-xs space-y-1 text-slate-600">
              <div className="flex justify-between">
                <span>MacBook Pro ordered</span><span className="text-slate-400">2026-10-25</span>
              </div>
              <div className="flex justify-between">
                <span>MacBook Pro received & configured</span><span className="text-slate-400">2026-11-05</span>
              </div>
              <div className="flex justify-between">
                <span>All equipment provisioned</span><span className="text-slate-400">2026-11-10</span>
              </div>
            </div>
          </div>
        </div>

        {/* System access */}
        <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
          <div className="flex items-center gap-2 mb-4">
            <Shield className="w-4 h-4 text-indigo-600" />
            <h2 className="text-base font-semibold text-slate-900">System Access — CASE-001</h2>
          </div>
          <div className="space-y-2">
            {case001.equipment?.access.map((sys) => (
              <div key={sys.system} className="flex items-center justify-between py-2 border-b border-slate-50 last:border-0">
                <span className="text-sm text-slate-700">{sys.system}</span>
                <StatusBadge status={sys.status === 'active' ? 'complete' : sys.status} size="xs" />
              </div>
            ))}
          </div>
          <div className="mt-4 pt-4 border-t border-slate-100 text-xs text-slate-500">
            <p className="font-semibold mb-1">Access Profile: engineer_standard</p>
            <p>Employee email: s.chen@enterprise.example.com</p>
            <p>All accounts active as of 2026-11-10</p>
          </div>
        </div>
      </div>

      {/* Processing steps */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        <h2 className="text-base font-semibold text-slate-900 mb-4">Processing Steps</h2>
        <StepTimeline steps={exec.steps} />
      </div>

      {/* History */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        <h2 className="text-base font-semibold text-slate-900 mb-4">Execution History</h2>
        <div className="space-y-2">
          {exec.history.map((h, i) => (
            <div key={i} className="flex items-start gap-4 py-2 border-b border-slate-50 last:border-0">
              <span className="text-xs text-slate-400 whitespace-nowrap font-mono">
                {new Date(h.timestamp).toLocaleDateString()}
              </span>
              <StatusBadge status={h.status} size="xs" />
              <span className="text-sm text-slate-700">{h.note}</span>
            </div>
          ))}
        </div>
        <ExpandableJSON data={exec} label="View full UC04 execution JSON" />
      </div>
    </div>
  )
}
