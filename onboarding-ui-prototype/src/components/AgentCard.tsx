import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import StatusBadge from './StatusBadge'
import type { AgentExecution } from '../types'

interface AgentCardProps {
  execution: AgentExecution;
  linkTo?: string;
}

export default function AgentCard({ execution, linkTo }: AgentCardProps) {
  return (
    <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5 hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">
              {execution.uc_number}
            </span>
            <StatusBadge status={execution.status} size="xs" />
          </div>
          <h3 className="text-sm font-semibold text-slate-900">{execution.agent_name}</h3>
        </div>
        {linkTo && (
          <Link
            to={linkTo}
            className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 font-medium shrink-0"
          >
            Detail <ArrowRight className="w-3 h-3" />
          </Link>
        )}
      </div>
      <p className="text-xs text-slate-500 mb-3 line-clamp-2">{execution.description}</p>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div>
          <span className="text-slate-400">Steps</span>
          <p className="font-semibold text-slate-700">{execution.steps.length}</p>
        </div>
        <div>
          <span className="text-slate-400">Duration</span>
          <p className="font-semibold text-slate-700">{execution.duration ?? '—'}</p>
        </div>
        <div>
          <span className="text-slate-400">Started</span>
          <p className="font-semibold text-slate-700">{new Date(execution.start_time).toLocaleDateString()}</p>
        </div>
        <div>
          <span className="text-slate-400">Case</span>
          <p className="font-semibold text-slate-700">{execution.case_id}</p>
        </div>
      </div>
    </div>
  )
}
