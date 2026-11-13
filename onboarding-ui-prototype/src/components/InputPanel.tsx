import ExpandableJSON from './ExpandableJSON'

interface InputField {
  label: string;
  value: string | number | boolean | null | undefined;
}

interface InputGroup {
  title: string;
  fields: InputField[];
}

interface InputPanelProps {
  groups: InputGroup[];
  rawData?: unknown;
}

export default function InputPanel({ groups, rawData }: InputPanelProps) {
  return (
    <div className="space-y-4">
      {groups.map((group) => (
        <div key={group.title} className="bg-white rounded-lg border border-slate-200 p-4">
          <h4 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">{group.title}</h4>
          <div className="space-y-2">
            {group.fields.map((field) => (
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
      ))}
      {rawData !== undefined && (
        <ExpandableJSON data={rawData} label="View full input JSON" />
      )}
    </div>
  )
}
