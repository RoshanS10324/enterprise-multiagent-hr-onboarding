import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import type { AgentStep } from '../types'
import StatusBadge, { statusDotColor } from './StatusBadge'
import ExpandableJSON from './ExpandableJSON'

interface StepTimelineProps {
  steps: AgentStep[];
}

export default function StepTimeline({ steps }: StepTimelineProps) {
  const [expanded, setExpanded] = useState<string | null>(null)

  return (
    <div className="space-y-2">
      {steps.map((step, idx) => {
        const isOpen = expanded === step.id
        const isLast = idx === steps.length - 1
        return (
          <div key={step.id} className="relative">
            {/* Connector line */}
            {!isLast && (
              <div className="absolute left-4 top-10 bottom-0 w-0.5 bg-slate-200 z-0" />
            )}
            <div className="relative z-10">
              {/* Step header */}
              <button
                onClick={() => setExpanded(isOpen ? null : step.id)}
                className="w-full flex items-start gap-3 p-3 rounded-lg hover:bg-slate-50 transition-colors text-left"
              >
                <div className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${statusDotColor(step.status)} text-white text-xs font-bold`}>
                  {idx + 1}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-semibold text-slate-900">{step.name}</span>
                    <StatusBadge status={step.status} size="xs" />
                    {step.duration && (
                      <span className="text-xs text-slate-400">{step.duration}</span>
                    )}
                  </div>
                  <p className="text-xs text-slate-500 mt-0.5">{step.purpose}</p>
                  {step.error && (
                    <p className="text-xs text-red-600 mt-1 font-medium">Error: {step.error}</p>
                  )}
                  {step.warning && (
                    <p className="text-xs text-amber-600 mt-1 font-medium">Warning: {step.warning}</p>
                  )}
                </div>
                {isOpen ? (
                  <ChevronDown className="w-4 h-4 text-slate-400 shrink-0 mt-1" />
                ) : (
                  <ChevronRight className="w-4 h-4 text-slate-400 shrink-0 mt-1" />
                )}
              </button>

              {/* Expanded detail */}
              {isOpen && (
                <div className="ml-11 mb-3 space-y-3">
                  {/* Input */}
                  <div className="bg-indigo-50 border border-indigo-100 rounded-lg p-3">
                    <p className="text-xs font-semibold text-indigo-700 uppercase tracking-wider mb-2">Input</p>
                    <div className="space-y-1">
                      {Object.entries(step.input).map(([k, v]) => (
                        <div key={k} className="flex justify-between gap-4 text-xs">
                          <span className="text-indigo-600">{k}</span>
                          <span className="text-indigo-900 font-medium text-right">{String(v)}</span>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Processing */}
                  <div className="bg-slate-50 border border-slate-200 rounded-lg p-3">
                    <p className="text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">Processing</p>
                    <ul className="space-y-1">
                      {step.processing.map((p, i) => (
                        <li key={i} className="text-xs text-slate-700 flex items-start gap-2">
                          <span className="text-slate-400 shrink-0">→</span>
                          {p}
                        </li>
                      ))}
                    </ul>
                  </div>

                  {/* Output */}
                  <div className="bg-green-50 border border-green-100 rounded-lg p-3">
                    <p className="text-xs font-semibold text-green-700 uppercase tracking-wider mb-2">Output</p>
                    <div className="space-y-1">
                      {Object.entries(step.output).map(([k, v]) => (
                        <div key={k} className="flex justify-between gap-4 text-xs">
                          <span className="text-green-600">{k}</span>
                          <span className="text-green-900 font-medium text-right">{String(v)}</span>
                        </div>
                      ))}
                    </div>
                  </div>

                  <ExpandableJSON data={{ input: step.input, processing: step.processing, output: step.output }} label="View step JSON" />
                </div>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
