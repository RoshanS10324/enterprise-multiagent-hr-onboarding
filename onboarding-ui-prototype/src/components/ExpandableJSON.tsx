import { useState } from 'react'
import { ChevronDown, ChevronRight, Code } from 'lucide-react'

interface ExpandableJSONProps {
  data: unknown;
  label?: string;
}

export default function ExpandableJSON({ data, label = 'View Raw JSON' }: ExpandableJSONProps) {
  const [open, setOpen] = useState(false)

  return (
    <div className="mt-3">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-xs font-medium text-indigo-600 hover:text-indigo-800 transition-colors"
      >
        <Code className="w-3.5 h-3.5" />
        {label}
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
      </button>
      {open && (
        <div className="mt-2 rounded-lg bg-slate-900 p-4 overflow-x-auto">
          <pre className="text-xs text-slate-100 whitespace-pre-wrap font-mono leading-relaxed">
            {JSON.stringify(data, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
