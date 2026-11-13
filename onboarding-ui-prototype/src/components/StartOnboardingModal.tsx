/**
 * Internal Onboarding Form -> POST /v1/orchestration/cases (the orchestrator runs UC01, then UC02 and
 * UC03's collection). Fields are exactly api.py StartCaseRequest; which are mandatory follows UC01
 * (name, email, designation, joining date, location) and UC02's start condition (role, department,
 * manager, project, offer acceptance and form approval facts). Client and HR officer come from the
 * verified identity, never from the form. Starting is idempotent per offer reference on the backend.
 *
 * Optionally HR starts from the onboarding form document (OnboardingDocumentIntake): UC01 extracts it, the fields
 * that map exactly are prefilled and marked, HR corrects and confirms the review, and the request carries the
 * extraction id so the case records where its data came from. The submission path is unchanged.
 */
import { useMemo, useRef, useState } from 'react'
import { AlertCircle, ArrowLeft, CheckCircle, Info, Loader, X } from 'lucide-react'
import { apiErrorMessage, currentIdentity } from '../api/apiClient'
import { startCase } from '../api/onboardingApi'
import type { CaseView, OnboardingDocumentExtraction, StartCaseRequest } from '../api/onboardingApi'
import OnboardingDocumentIntake from './OnboardingDocumentIntake'

interface Props {
  onClose: () => void
  onStarted: (view: CaseView) => void
}

type Values = Record<keyof typeof FIELDS, string>

const FIELDS = {
  candidate_name: { label: 'Employee full name', required: true, section: 'A' },
  candidate_email: { label: 'Personal email', required: false, section: 'A', type: 'email' },
  id_number: { label: 'ID / passport number', required: false, section: 'A',
    hint: 'Used by UC01 for duplicate detection only; never shown again.' },
  employee_id: { label: 'HRMS employee number', required: false, section: 'A',
    hint: 'Required for clients that identify employees by their HRMS number (e.g. CLIENT-ALPHA); otherwise UC01 allocates it.' },
  nationality_group: { label: 'Nationality group code', required: false, section: 'A',
    hint: 'The client’s opaque group code (e.g. NG-1), never a nationality.' },
  designation: { label: 'Job title / designation', required: true, section: 'B' },
  role_id: { label: 'Position (role code)', required: false, section: 'B', hint: 'e.g. software_engineer' },
  department: { label: 'Department', required: true, section: 'B' },
  grade: { label: 'Grade', required: false, section: 'B' },
  manager_id: { label: 'Reporting manager ID', required: false, section: 'B', hint: 'e.g. TL-TEST-001' },
  project: { label: 'Project', required: false, section: 'B', hint: 'Required by UC02 task planning.' },
  location: { label: 'Work location', required: true, section: 'B' },
  work_location_type: { label: 'Work location type', required: true, section: 'B' },
  attributes: { label: 'Checklist attributes', required: false, section: 'B',
    hint: 'Comma-separated codes the client’s configuration asks HR to state, e.g. employment_type:full_time, visa_route:in_country_transfer. UC02 never guesses them.' },
  joining_date: { label: 'Joining date', required: true, section: 'C', type: 'date' },
  offer_reference: { label: 'Offer reference', required: false, section: 'C',
    hint: 'Identifies the case: resubmitting the same reference never creates a second case.' },
  offer_accepted_at: { label: 'Offer accepted at', required: false, section: 'C', type: 'datetime-local' },
  offer_acceptance_event_id: { label: 'Offer acceptance event ID', required: false, section: 'D',
    hint: 'Only when UC05 reads offers from a mailbox. With the case source (UC05_OFFER_EVENT_SOURCE=orchestrator_case) the case itself is the event and this is not used.' },
  timezone: { label: 'Employee timezone (IANA)', required: false, section: 'D', hint: 'e.g. Asia/Dubai' },
} as const

const SECTIONS: Record<string, string> = {
  A: 'A. Employee Information', B: 'B. Employment Information', C: 'C. Joining Information', D: 'D. Additional Information',
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]*$/

function today(): string {
  const d = new Date()
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 10)
}

function splitAttributes(raw: string): string[] {
  return raw.split(',').map(a => a.trim()).filter(Boolean)
}

function validate(v: Values): Partial<Record<keyof Values, string>> {
  const e: Partial<Record<keyof Values, string>> = {}
  for (const [key, f] of Object.entries(FIELDS) as [keyof Values, (typeof FIELDS)[keyof Values]][]) {
    if (f.required && !v[key].trim()) e[key] = `${f.label} is required`
  }
  if (v.candidate_email && !EMAIL.test(v.candidate_email.trim())) e.candidate_email = 'Enter a valid email address'
  if (v.joining_date && v.joining_date < today()) e.joining_date = 'The joining date cannot be in the past'
  if (v.offer_accepted_at && new Date(v.offer_accepted_at).getTime() > Date.now() + 60_000) {
    e.offer_accepted_at = 'The offer cannot be accepted in the future'
  }
  if (v.offer_reference && !SAFE_ID.test(v.offer_reference.trim())) {
    e.offer_reference = 'Use letters, digits and . _ : / - only'
  }
  if (v.timezone && !/^[A-Za-z_]+(\/[A-Za-z0-9_+-]+)+$|^UTC$/.test(v.timezone.trim())) {
    e.timezone = 'Use an IANA timezone such as Asia/Dubai'
  }
  if (splitAttributes(v.attributes).some(a => !/^[A-Za-z0-9_:.-]{1,64}$/.test(a))) {
    e.attributes = 'Codes only: letters, digits and _ : . - (up to 64 characters each), separated by commas'
  }
  for (const k of ['candidate_name', 'designation', 'department', 'location', 'project'] as const) {
    if (v[k].length > 200) e[k] = 'Too long'
  }
  return e
}

const EMPTY = Object.fromEntries(Object.keys(FIELDS).map(k => [k, ''])) as Values

export default function StartOnboardingModal({ onClose, onStarted }: Props) {
  const [values, setValues] = useState<Values>({ ...EMPTY, work_location_type: 'OFFICE' })
  const [offerAccepted, setOfferAccepted] = useState(false)
  const [touched, setTouched] = useState<Partial<Record<keyof Values, boolean>>>({})
  const [step, setStep] = useState<'form' | 'confirm'>('form')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [extraction, setExtraction] = useState<OnboardingDocumentExtraction | null>(null)
  const [reviewed, setReviewed] = useState(false)
  const [reviewTouched, setReviewTouched] = useState(false)
  const inFlight = useRef(false)

  const errors = useMemo(() => validate(values), [values])
  const reviewMissing = !!extraction?.review_required && !reviewed
  const formValid = Object.keys(errors).length === 0 && offerAccepted && !reviewMissing

  const set = (k: keyof Values) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setValues(prev => ({ ...prev, [k]: e.target.value }))

  function applyExtraction(view: OnboardingDocumentExtraction) {
    // Only the values UC01 read as PRESENT and that mean exactly the form field (the backend's prefill).
    setValues(prev => ({ ...prev, ...Object.fromEntries(
      Object.entries(view.prefill).filter(([k, v]) => k in FIELDS && typeof v === 'string')) }))
    setExtraction(view); setReviewed(false); setReviewTouched(false)
  }

  function clearExtraction() {
    setExtraction(null); setReviewed(false); setReviewTouched(false)  // the form keeps what was entered
  }

  /** 'document' while a field still holds the prefilled value, 'edited' once HR changed it. */
  function origin(key: keyof Values): 'document' | 'edited' | null {
    const value = extraction?.prefill[key as keyof OnboardingDocumentExtraction['prefill']]
    if (value === undefined) return null
    return values[key].trim() === value ? 'document' : 'edited'
  }

  function request(): StartCaseRequest {
    const v = Object.fromEntries(Object.entries(values).map(([k, x]) => [k, x.trim()])) as Values
    const opt = (x: string) => (x ? x : undefined)
    return {
      offer_accepted: offerAccepted,
      offer_reference: v.offer_reference,
      candidate_name: v.candidate_name, candidate_email: v.candidate_email, designation: v.designation,
      joining_date: v.joining_date, location: v.location,
      work_location_type: v.work_location_type as 'OFFICE' | 'CLIENT_SITE',
      role_id: v.role_id, department: v.department, manager_id: v.manager_id, project: v.project,
      grade: opt(v.grade), nationality_group: opt(v.nationality_group), id_number: opt(v.id_number),
      attributes: splitAttributes(v.attributes).length ? splitAttributes(v.attributes) : undefined,
      employee_id: opt(v.employee_id), offer_acceptance_event_id: opt(v.offer_acceptance_event_id),
      timezone: opt(v.timezone),
      ...(v.offer_accepted_at ? { offer_accepted_at: new Date(v.offer_accepted_at).toISOString() } : {}),
      // No approval facts: submitting raises an approval request; only an authorized approver's decision
      // (recorded by the backend) approves the form.
      ...(extraction ? { source_extraction_id: extraction.extraction_id, extraction_reviewed: reviewed } : {}),
    }
  }

  function toConfirm(e: React.FormEvent) {
    e.preventDefault()
    setTouched(Object.fromEntries(Object.keys(FIELDS).map(k => [k, true])))
    setReviewTouched(true)
    if (formValid) { setError(null); setStep('confirm') }
  }

  async function submit() {
    if (inFlight.current) return  // no duplicate submissions
    inFlight.current = true
    setSubmitting(true); setError(null)
    try {
      const view = await startCase(request())
      onStarted(view)
    } catch (err) {
      setError(apiErrorMessage(err))
      setStep('form')
    } finally {
      inFlight.current = false
      setSubmitting(false)
    }
  }

  function field(key: keyof Values) {
    const f = FIELDS[key] as { label: string; required: boolean; type?: string; hint?: string }
    const err = touched[key] ? errors[key] : undefined
    const cls = `w-full rounded-md border px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300 ${
      err ? 'border-red-300 bg-red-50' : 'border-slate-300'}`
    const from = origin(key)
    return (
      <label key={key} className="block">
        <span className="text-xs font-medium text-slate-600">{f.label}{f.required && <span className="text-red-500"> *</span>}
          {from && <span className={`ml-1.5 px-1 rounded text-[10px] ${from === 'document' ? 'bg-indigo-50 text-indigo-600' : 'bg-amber-50 text-amber-700'}`}>
            {from === 'document' ? 'from document' : 'edited'}</span>}</span>
        {key === 'work_location_type' ? (
          <select value={values[key]} onChange={set(key)} onBlur={() => setTouched(t => ({ ...t, [key]: true }))} className={cls}>
            <option value="OFFICE">Office</option>
            <option value="CLIENT_SITE">Client site</option>
          </select>

        ) : (
          <input type={f.type ?? 'text'} value={values[key]} onChange={set(key)}
            onBlur={() => setTouched(t => ({ ...t, [key]: true }))} className={cls}
            min={key === 'joining_date' ? today() : undefined} autoComplete="off" />
        )}
        {err ? <span className="text-xs text-red-600">{err}</span>
          : f.hint ? <span className="text-[11px] text-slate-400">{f.hint}</span> : null}
      </label>
    )
  }

  const req = step === 'confirm' ? request() : null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-3xl max-h-[92vh] flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">Internal Onboarding Form</h2>
            <p className="text-xs text-slate-500">Submitting starts UC01 Employee Record Creation through the central orchestrator.</p>
          </div>
          <button onClick={onClose} disabled={submitting} className="text-slate-400 hover:text-slate-600 disabled:opacity-40">
            <X className="w-5 h-5" />
          </button>
        </div>

        {error && (
          <div className="mx-6 mt-4 flex items-start gap-2 bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" /><span>Onboarding was not started: {error}</span>
          </div>
        )}

        {step === 'form' ? (
          <form onSubmit={toConfirm} className="flex-1 overflow-y-auto px-6 py-4 space-y-5" noValidate>
            <OnboardingDocumentIntake extraction={extraction} onExtracted={applyExtraction} onCleared={clearExtraction}
              reviewed={reviewed} onReviewedChange={setReviewed} showReviewError={reviewTouched} disabled={submitting} />
            {(['A', 'B', 'C', 'D'] as const).map(section => (
              <fieldset key={section}>
                <legend className="text-xs font-bold text-slate-500 uppercase tracking-wide mb-2">{SECTIONS[section]}</legend>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {(Object.keys(FIELDS) as (keyof Values)[]).filter(k => FIELDS[k].section === section).map(field)}
                  {section === 'C' && (
                    <>
                      <label className="flex items-start gap-2 sm:col-span-2">
                        <input type="checkbox" checked={offerAccepted} onChange={e => setOfferAccepted(e.target.checked)} className="mt-1" />
                        <span className="text-xs text-slate-700">The candidate has accepted the offer <span className="text-red-500">*</span>
                          {!offerAccepted && touched.offer_reference && <span className="block text-red-600">UC01 opens a case only for an accepted offer</span>}
                        </span>
                      </label>
                      <div className="sm:col-span-2 text-xs text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
                        HR officer / requesting user: <span className="font-medium text-slate-700">{currentIdentity().userId}</span> ({currentIdentity().roles})
                        · client <span className="font-medium text-slate-700">{currentIdentity().clientId}</span> — taken from the verified identity, not from this form.
                      </div>
                    </>
                  )}
                </div>
              </fieldset>
            ))}
            <div className="flex items-start gap-2 text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-md px-3 py-2">
              <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>Submitting does not approve this form: it creates an approval request for an authorized HR approver.
                Contact number and nationality are not collected: no UC01 contract, orchestrator input or Supabase column
                holds them today. Employment type and visa route are stated as checklist attributes when the client’s
                configuration asks for them.</span>
            </div>
            <div className="flex justify-end gap-2 pt-2 border-t border-slate-100">
              <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800">Cancel</button>
              <button type="submit" className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700">
                Review &amp; confirm
              </button>
            </div>
          </form>
        ) : (
          <div className="flex-1 overflow-y-auto px-6 py-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">Confirm before starting onboarding</h3>
            <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm">
              {req && (Object.keys(FIELDS) as (keyof Values)[]).map(k => (
                <div key={k} className="flex flex-col">
                  <dt className="text-xs text-slate-400">{FIELDS[k].label}</dt>
                  <dd className="text-slate-800 break-words">{k === 'id_number' && values[k] ? '•••• (provided)' : values[k] || '—'}</dd>
                </div>
              ))}
              <div className="flex flex-col"><dt className="text-xs text-slate-400">Offer accepted</dt><dd>Yes</dd></div>
              {extraction && (
                <div className="flex flex-col sm:col-span-2"><dt className="text-xs text-slate-400">Source document</dt>
                  <dd>{extraction.source_document.filename} (UC01: {extraction.status === 'READY_FOR_REVIEW'
                    ? 'ready for review' : 'validation required'}) ·{' '}
                    {(Object.keys(FIELDS) as (keyof Values)[]).filter(k => origin(k) === 'document').length} field(s) from
                    the document,{' '}{(Object.keys(FIELDS) as (keyof Values)[]).filter(k => origin(k) === 'edited').length} edited by you
                    {extraction.review_required && (reviewed ? ' · review confirmed' : ' · review NOT confirmed')}.
                    The case records the document’s statuses and field names, not its values.</dd></div>
              )}
              <div className="flex flex-col sm:col-span-2"><dt className="text-xs text-slate-400">Form approval</dt>
                <dd>Not approved by submitting. An approval request is raised for an authorized HR approver (the
                  role the approval policy names; never you as the requester). UC01 creates the employee record
                  only after that approval.</dd></div>
            </dl>
            <p className="mt-4 text-xs text-slate-500">
              Next: the case waits at “HR Form Approval”. Once an authorized approver approves it, the orchestrator
              runs UC01 (record creation), then UC02 task planning and UC03 document collection.
            </p>
            <div className="flex justify-between gap-2 pt-4 mt-4 border-t border-slate-100">
              <button onClick={() => setStep('form')} disabled={submitting}
                className="inline-flex items-center gap-1 px-4 py-2 text-sm text-slate-600 hover:text-slate-800 disabled:opacity-40">
                <ArrowLeft className="w-4 h-4" /> Edit
              </button>
              <button onClick={submit} disabled={submitting}
                className="inline-flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-60">
                {submitting ? <Loader className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
                {submitting ? 'Starting onboarding…' : 'Start onboarding'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
