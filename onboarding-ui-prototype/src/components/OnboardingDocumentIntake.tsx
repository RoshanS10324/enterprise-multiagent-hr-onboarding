/**
 * The document part of the Internal Onboarding Form (StartOnboardingModal): upload the completed onboarding form,
 * UC01 extracts and validates it (POST /onboarding-documents/extract, through the orchestrator), HR sees every field's
 * status, what is missing / invalid / uncertain and why a review is needed, and the form is prefilled with the values
 * that map exactly. HR corrects the form, confirms the review, and submits it as before: the form still goes to the
 * HR approval gate, and UC01 creates the record only after that approval.
 *
 * Nothing here decides anything: statuses, issues and the prefill are UC01's, shown as returned. Sensitive values
 * (salary, mobile, parents' names) never reach the browser -- only their status.
 */
import { useRef, useState } from 'react'
import { AlertCircle, CheckCircle, ChevronDown, ChevronRight, FileText, Loader, Upload, X } from 'lucide-react'
import { apiErrorMessage } from '../api/apiClient'
import { extractionFailure, extractOnboardingDocument } from '../api/onboardingApi'
import type { ExtractedFieldView, ExtractionFailure, OnboardingDocumentExtraction } from '../api/onboardingApi'
import { extractionFieldLabel, extractionIssueLabel, extractionStatusLabel } from '../lib/labels'

const MAX_BYTES = 10 * 1024 * 1024  // UC01's limit; the backend enforces it (and may set a lower one)
const ACCEPT = '.docx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document'

type Phase = 'idle' | 'UPLOADED' | 'EXTRACTING' | 'FAILED' | 'DONE'

interface Props {
  extraction: OnboardingDocumentExtraction | null
  onExtracted: (view: OnboardingDocumentExtraction) => void
  onCleared: () => void
  reviewed: boolean
  onReviewedChange: (value: boolean) => void
  showReviewError: boolean
  disabled?: boolean
}

function size(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

const STATUS_STYLE: Record<string, string> = {
  PRESENT: 'bg-emerald-50 text-emerald-700', MISSING: 'bg-slate-100 text-slate-600',
  INVALID: 'bg-red-50 text-red-700', UNCERTAIN: 'bg-amber-50 text-amber-700',
}

function shown(f: ExtractedFieldView): string {
  if (f.value !== undefined && f.value !== null && f.value !== '') return String(f.value)
  return f.status === 'PRESENT' ? 'Present — hidden (sensitive)' : '—'
}

export default function OnboardingDocumentIntake({ extraction, onExtracted, onCleared, reviewed, onReviewedChange,
  showReviewError, disabled }: Props) {
  const [phase, setPhase] = useState<Phase>(extraction ? 'DONE' : 'idle')
  const [file, setFile] = useState<{ name: string; size: number } | null>(null)
  const [failure, setFailure] = useState<ExtractionFailure['error'] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showFields, setShowFields] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  async function choose(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = e.target.files?.[0]
    e.target.value = ''  // the same file can be chosen again after a correction
    if (!picked) return
    setFailure(null); setError(null); onCleared()
    setFile({ name: picked.name, size: picked.size })
    const ext = picked.name.toLowerCase().slice(picked.name.lastIndexOf('.'))
    // Quick feedback only; the backend checks the same and more (signature, UC01's reader limits).
    if (!['.docx', '.pdf'].includes(ext)) {
      setPhase('FAILED')
      setFailure({ code: 'UNSUPPORTED_FORMAT', reason: `'${ext || 'no extension'}' is not supported`,
        action: 'Upload the onboarding form as a .docx or a text-based .pdf.' })
      return
    }
    if (picked.size === 0 || picked.size > MAX_BYTES) {
      setPhase('FAILED')
      setFailure(picked.size === 0
        ? { code: 'EMPTY_FILE', reason: 'the file is empty', action: 'Upload the completed onboarding form.' }
        : { code: 'FILE_TOO_LARGE', reason: `the file is ${size(picked.size)}`,
            action: 'Upload only the onboarding form (at most 10 MB).' })
      return
    }
    setPhase('EXTRACTING')  // the file is sent with the request: uploaded and extracting are one step here
    try {
      const view = await extractOnboardingDocument(picked)
      setPhase('DONE')
      onExtracted(view)
    } catch (err) {
      setPhase('FAILED')
      const refused = extractionFailure(err)
      if (refused) setFailure(refused.error)
      else setError(apiErrorMessage(err))
    }
  }

  function clear() {
    setPhase('idle'); setFile(null); setFailure(null); setError(null); onCleared()
  }

  const steps: { key: string; label: string; done: boolean; active: boolean }[] = [
    { key: 'UPLOADED', label: extractionStatusLabel('UPLOADED'), done: phase === 'EXTRACTING' || phase === 'DONE', active: phase === 'UPLOADED' },
    { key: 'EXTRACTING', label: extractionStatusLabel('EXTRACTING'), done: phase === 'DONE', active: phase === 'EXTRACTING' },
    { key: 'EXTRACTION_COMPLETED', label: extractionStatusLabel('EXTRACTION_COMPLETED'), done: phase === 'DONE', active: false },
    { key: 'RESULT', label: extraction ? extractionStatusLabel(extraction.status) : 'Review', done: false, active: phase === 'DONE' },
  ]

  const fieldRows = extraction
    ? Object.entries({ ...extraction.extraction.fields, ...extraction.extraction.supplementary })
        .filter(([key]) => !key.startsWith('documents.'))
    : []

  return (
    <fieldset className="border border-indigo-100 bg-indigo-50/40 rounded-lg px-4 py-3">
      <legend className="text-xs font-bold text-slate-500 uppercase tracking-wide px-1">
        Start from the onboarding form document (optional)
      </legend>
      <p className="text-[11px] text-slate-500 mb-2">
        UC01 reads the 2026 onboarding form (.docx or text-based .pdf, up to 10 MB) and fills in the fields below that
        map exactly. Nothing is stored and no case is opened until you submit; the form still needs HR approval.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <input ref={input} type="file" accept={ACCEPT} onChange={choose} className="hidden" disabled={disabled} />
        <button type="button" onClick={() => input.current?.click()} disabled={disabled || phase === 'EXTRACTING'}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-indigo-200 bg-white text-indigo-700 hover:bg-indigo-50 disabled:opacity-50">
          <Upload className="w-3.5 h-3.5" /> {file ? 'Upload another document' : 'Upload onboarding form'}
        </button>
        {file && (
          <span className="inline-flex items-center gap-1 text-xs text-slate-600">
            <FileText className="w-3.5 h-3.5" /> {file.name} · {size(file.size)}
            {phase !== 'EXTRACTING' && (
              <button type="button" onClick={clear} title="Remove the document (the form keeps what you entered)"
                className="ml-1 text-slate-400 hover:text-slate-600"><X className="w-3.5 h-3.5" /></button>
            )}
          </span>
        )}
      </div>

      {phase !== 'idle' && phase !== 'FAILED' && (
        <ol className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-[11px]">
          {steps.map(s => (
            <li key={s.key} className={`inline-flex items-center gap-1 ${s.done ? 'text-emerald-700' : s.active ? 'text-indigo-700 font-medium' : 'text-slate-400'}`}>
              {s.active && phase === 'EXTRACTING' && s.key === 'EXTRACTING' ? <Loader className="w-3 h-3 animate-spin" />
                : s.done ? <CheckCircle className="w-3 h-3" /> : <span className="w-3 h-3 rounded-full border border-current inline-block" />}
              {s.label}
            </li>
          ))}
        </ol>
      )}

      {phase === 'FAILED' && (failure || error) && (
        <div className="mt-2 flex items-start gap-2 bg-red-50 border border-red-200 rounded-md px-3 py-2 text-xs text-red-700">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <div>
            <p className="font-medium">{extractionStatusLabel('FAILED')}{failure ? ` — ${failure.code}` : ''}</p>
            <p>{failure ? failure.reason : error}</p>
            {failure?.action && <p className="mt-0.5 text-red-800">{failure.action}</p>}
            <p className="mt-0.5 text-slate-500">You can still fill in the form manually.</p>
          </div>
        </div>
      )}

      {extraction && phase === 'DONE' && (
        <div className="mt-3 space-y-2 text-xs">
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-slate-600">
            <span>Status: <span className={`font-semibold ${extraction.status === 'READY_FOR_REVIEW' ? 'text-emerald-700' : 'text-amber-700'}`}>
              {extractionStatusLabel(extraction.status)}</span></span>
            <span>Template: {extraction.source_document.template_recognised ? extraction.source_document.template_id : 'not recognised'}</span>
            <span>Completeness: {Math.round((extraction.completeness ?? 0) * 100)}%</span>
            <span>Prefilled: {Object.keys(extraction.prefill).length} field(s)</span>
            {extraction.extraction.synthetic_document && <span className="text-slate-500">Synthetic test document</span>}
          </div>

          {extraction.validation_errors.length > 0 && (
            <div className="bg-red-50 border border-red-200 rounded-md px-3 py-2 text-red-700">
              <p className="font-medium">The document lacks required data — enter or correct it on the form:</p>
              <ul className="list-disc ml-4">{extraction.validation_errors.map(e => <li key={e}>{extractionIssueLabel(e)}</li>)}</ul>
              {extraction.validation_errors.some(e => e.endsWith('employee.final_salary')) && (
                <p className="mt-1 text-[11px] text-red-800">Salary is not carried by this form; the approver sees that the
                  source document lacks it.</p>
              )}
            </div>
          )}
          {extraction.review_reasons.length > 0 && (
            <div className="bg-amber-50 border border-amber-200 rounded-md px-3 py-2 text-amber-800">
              <p className="font-medium">Check before submitting:</p>
              <ul className="list-disc ml-4">{extraction.review_reasons.map(r => <li key={r}>{extractionIssueLabel(r)}</li>)}</ul>
            </div>
          )}
          {extraction.form_inputs_required.length > 0 && (
            <p className="text-slate-500">Not in the document (enter on the form):{' '}
              {extraction.form_inputs_required.map(extractionIssueLabel).map(s => s.replace(/^Missing: /, '')).join('; ')}</p>
          )}
          {extraction.outstanding_documents.length > 0 && (
            <p className="text-slate-500">Documents marked as not yet received (collected later by UC03):{' '}
              {extraction.outstanding_documents.map(d => d.replace(/_/g, ' ')).join(', ')}</p>
          )}

          <button type="button" onClick={() => setShowFields(v => !v)}
            className="inline-flex items-center gap-1 text-indigo-700 hover:text-indigo-900">
            {showFields ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
            Extracted fields ({fieldRows.length})
          </button>
          {showFields && (
            <div className="max-h-56 overflow-y-auto border border-slate-200 rounded-md bg-white">
              <table className="w-full text-[11px]">
                <thead className="bg-slate-50 text-slate-500 sticky top-0">
                  <tr><th className="text-left px-2 py-1">Field</th><th className="text-left px-2 py-1">Status</th>
                    <th className="text-left px-2 py-1">Value</th><th className="text-left px-2 py-1">Used for</th></tr>
                </thead>
                <tbody>
                  {fieldRows.map(([key, f]) => {
                    const used = Object.entries(extraction.prefill_sources).filter(([, src]) => src === key).map(([k]) => k)
                    return (
                      <tr key={key} className="border-t border-slate-100 align-top">
                        <td className="px-2 py-1 text-slate-700">{extractionFieldLabel(key)}</td>
                        <td className="px-2 py-1"><span className={`px-1.5 py-0.5 rounded ${STATUS_STYLE[f.status] ?? ''}`}>{f.status}</span></td>
                        <td className="px-2 py-1 text-slate-700 break-words">{shown(f)}{f.note && <span className="block text-slate-400">{f.note}</span>}</td>
                        <td className="px-2 py-1 text-slate-500">{used.length ? `form: ${used.join(', ')}` : 'reference only'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          {extraction.review_required && (
            <label className={`flex items-start gap-2 rounded-md px-3 py-2 border ${showReviewError && !reviewed ? 'border-red-300 bg-red-50' : 'border-slate-200 bg-white'}`}>
              <input type="checkbox" checked={reviewed} onChange={e => onReviewedChange(e.target.checked)} className="mt-0.5" />
              <span className="text-slate-700">I have checked the flagged fields against the document and corrected the form
                where needed. <span className="text-red-500">*</span>
                {showReviewError && !reviewed && <span className="block text-red-600">Confirm the review to continue</span>}
              </span>
            </label>
          )}
        </div>
      )}
    </fieldset>
  )
}
