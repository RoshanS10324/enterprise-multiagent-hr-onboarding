import { ArrowRight } from 'lucide-react'
import ExpandableJSON from './ExpandableJSON'

interface OutputField {
  label: string;
  value: string | number | boolean | null | undefined;
}

interface OutputPanelProps {
  fields: OutputField[];
  downstream?: string;
  rawData?: unknown;
  title?: string;
}

export default function OutputPanel({ fields, downstream, rawData, title = 'Output Summary' }: OutputPanelProps) {
  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg border border-slate-200 p-4">
        <h4 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">{title}</h4>
        <div className="space-y-2">
          {fields.map((field) => (
            <div key={field.label} className="flex justify-between items-start gap-4">
              <span className="text-sm text-slate-500 shrink-0">{field.label}</span>
              <span className="text-sm font-medium text-slate-900 text-right">
                {field.value === null || field.value === undefined
                  ? '—'
                  : typeof field.value === 'boolean'
                  ? field.value ? 'Yes' : 'No'
                  : String(field.value)}
              </span>
            </div>
          ))}
        </div>
      </div>
      {downstream && (
        <div className="flex items-center gap-2 px-4 py-3 bg-teal-50 border border-teal-200 rounded-lg">
          <ArrowRight className="w-4 h-4 text-teal-600 shrink-0" />
          <span className="text-sm text-teal-700">
            <span className="font-medium">Downstream:</span> {downstream}
          </span>
        </div>
      )}
      {rawData !== undefined && (
        <ExpandableJSON data={rawData} label="View full output JSON" />
      )}
    </div>
  )
}
