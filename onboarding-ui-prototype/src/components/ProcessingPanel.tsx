import { CheckCircle, Clock, AlertCircle } from 'lucide-react'

type ActionStatus = 'complete' | 'pending' | 'warning';

interface ProcessingAction {
  text: string;
  status?: ActionStatus;
}

interface ProcessingPanelProps {
  actions: ProcessingAction[];
  title?: string;
}

export default function ProcessingPanel({ actions, title = 'Processing Steps' }: ProcessingPanelProps) {
  return (
    <div className="bg-white rounded-lg border border-slate-200 p-4">
      <h4 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">{title}</h4>
      <ol className="space-y-2">
        {actions.map((action, i) => {
          const status = action.status ?? 'complete'
          const Icon = status === 'complete' ? CheckCircle : status === 'warning' ? AlertCircle : Clock
          const iconColor = status === 'complete' ? 'text-green-500' : status === 'warning' ? 'text-amber-500' : 'text-slate-400'
          return (
            <li key={i} className="flex items-start gap-3">
              <span className="text-xs text-slate-400 mt-0.5 w-4 shrink-0 text-right">{i + 1}.</span>
              <Icon className={`w-4 h-4 shrink-0 mt-0.5 ${iconColor}`} />
              <span className="text-sm text-slate-700">{action.text}</span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
