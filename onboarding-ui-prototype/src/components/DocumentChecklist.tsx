/**
 * UC03 documents of one case: the current plan revision's required documents, real uploads (backend ->
 * Supabase Storage -> UC03), each document's validation result, a signed-URL view, and the HR review
 * decision. Every status shown is read from the case state; nothing is simulated.
 */
import { useRef, useState } from 'react'
import { AlertCircle, CheckCircle, Clock, Eye, FileText, Loader, ShieldCheck, Upload, XCircle } from 'lucide-react'
import { apiErrorMessage } from '../api/apiClient'
import { changeDocumentRequirements, getDocumentUrl, reviewDocuments, uploadDocument } from '../api/onboardingApi'
import type { CaseView, HealthView, UploadReceipt } from '../api/onboardingApi'
import { allowed, usePermissions } from '../api/permissions'
import { fmtTime, humanize } from '../lib/stages'

interface Props {
  view: CaseView
  health: HealthView | null
  onApply: (view: CaseView) => void
}

const EXT: Record<string, string> = {
  'application/pdf': '.pdf', 'image/jpeg': '.jpg,.jpeg', 'image/png': '.png', 'image/tiff': '.tif,.tiff',
}

function validationBadge(v: CaseView['document_validation'][string] | undefined, hasDoc: boolean) {
  if (!hasDoc) return { label: 'Not uploaded', cls: 'text-slate-500', Icon: Clock }
  if (!v) return { label: 'Pending validation', cls: 'text-blue-600', Icon: Loader }
  const c = v.classification ?? ''
  if (c === 'verified' || v.document_status === 'VERIFIED') return { label: 'Validated', cls: 'text-green-700', Icon: CheckCircle }
  if (c === 'hr_review_required') return { label: 'HR review required', cls: 'text-amber-700', Icon: ShieldCheck }
  if (c === 'rejected' || v.review_decision === 'rejected') return { label: 'Rejected', cls: 'text-red-700', Icon: XCircle }
  if (c.includes('correction') || c === 'invalid' || c === 'missing') return { label: 'Correction required', cls: 'text-red-700', Icon: AlertCircle }
  return { label: humanize(c || v.document_status || 'processing'), cls: 'text-slate-700', Icon: FileText }
}

export default function DocumentChecklist({ view, health, onApply }: Props) {
  const [busy, setBusy] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [receipts, setReceipts] = useState<Record<string, UploadReceipt>>({})
  const [notes, setNotes] = useState<Record<string, string>>({})
  const inputs = useRef<Record<string, HTMLInputElement | null>>({})
  const me = usePermissions()
  const mayUpload = allowed(me, 'submit_documents')
  const mayView = allowed(me, 'view_documents')
  const mayReview = allowed(me, 'review_documents')
  const mayChangeRequirements = allowed(me, 'change_document_requirements')

  const collection = view.document_collection
  const storage = health?.document_storage
  const accept = (storage?.allowed_types ?? Object.keys(EXT)).map(t => EXT[t] ?? '').filter(Boolean).join(',')
  const planVersion = (view.onboarding_plan as { plan_version?: number } | null)?.plan_version
  const verifying = view.stage_statuses.validate_documents === 'complete'

  if (!collection) {
    return <p className="text-xs text-slate-500">Document collection has not been opened yet: UC02 must complete planning first.</p>
  }

  const required = collection.required_documents
  const mandatoryDocs = required.filter(d => d.required !== false)
  const validated = mandatoryDocs.filter(d => {
    const v = view.document_validation[d.document_type]
    return v && (v.classification === 'verified' || v.document_status === 'VERIFIED')
  }).length

  async function run(key: string, fn: () => Promise<void>) {
    setBusy(key); setErrors(e => ({ ...e, [key]: '' }))
    try { await fn() } catch (err) { setErrors(e => ({ ...e, [key]: apiErrorMessage(err) })) } finally { setBusy(null) }
  }

  const upload = (docType: string, file: File) => run(docType, async () => {
    if (storage?.max_bytes && file.size > storage.max_bytes) {
      throw Object.assign(new Error(), { response: { status: 413, data: {
        detail: `FILE_TOO_LARGE: the file exceeds ${Math.round(storage.max_bytes / 1024 / 1024)} MB` } } })
    }
    const out = await uploadDocument(view.case_id, docType, file)
    setReceipts(r => ({ ...r, [docType]: out.upload }))
    onApply(out.case)
  })

  const openDoc = (docType: string) => run(`view:${docType}`, async () => {
    const url = await getDocumentUrl(view.case_id, docType)
    window.open(url, '_blank', 'noopener,noreferrer')
  })

  const dropRequirement = (docType: string) => run(`drop:${docType}`, async () => {
    const reason = notes[`drop:${docType}`]?.trim()
    if (!reason) throw Object.assign(new Error(), { response: { status: 422,
      data: { detail: 'Give the reason this document is not required for this case (it is recorded)' } } })
    onApply(await changeDocumentRequirements(view.case_id, { remove_documents: [docType], reason }))
  })

  const review = (docType: string, decision: 'approved' | 'rejected') => run(`review:${docType}`, async () => {
    const note = notes[docType]?.trim()
    if (decision === 'rejected' && !note) throw Object.assign(new Error(), { response: { status: 422,
      data: { detail: 'Give a reason for the rejection (it is recorded with the decision)' } } })
    onApply(await reviewDocuments(view.case_id, [{ document_type: docType, decision, ...(note ? { note } : {}) }]))
  })

  return (
    <div className="space-y-3">
      <div>
        <div className="flex justify-between text-xs text-slate-500 mb-1">
          <span>Plan revision v{collection.plan_version ?? '—'}{planVersion && planVersion !== collection.plan_version
            ? ` (latest plan v${planVersion})` : ''}</span>
          <span className="font-medium">{validated} / {mandatoryDocs.length} mandatory validated</span>
        </div>
        <div className="w-full bg-slate-100 rounded-full h-1.5">
          <div className="bg-green-500 h-1.5 rounded-full transition-all" style={{ width: `${mandatoryDocs.length ? (validated / mandatoryDocs.length) * 100 : 0}%` }} />
        </div>
      </div>

      {!storage?.configured && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5">
          Document storage is not configured on the backend (DOCUMENT_STORAGE_BUCKET), so files cannot be uploaded.
        </p>
      )}
      {storage?.configured && storage.extraction && !storage.extraction.available && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5">
          UC03 text extraction is unavailable on the backend: {storage.extraction.reason}. Uploaded files are stored,
          but UC03 will report them unreadable until this is fixed.
        </p>
      )}

      {required.map(doc => {
        const t = doc.document_type
        const submitted = view.documents[t]
        const validation = view.document_validation[t]
        const optional = doc.required === false
        const rawBadge = validationBadge(validation, !!submitted)
        const badge = optional && rawBadge.cls.includes('red')
          ? { ...rawBadge, label: `${rawBadge.label} (optional)`, cls: 'text-amber-700' } : rawBadge
        const corrections = view.correction_requirements.filter(c => c.document_type === t && c.action !== 'hr_review')
        // The backend's own rule (orchestrator review_documents): the gate is still open, the document is submitted,
        // UC03 asked for HR review, and that validation is of the CURRENT revision. Otherwise a decision is refused (409).
        const needsReview = !verifying && !!submitted && validation?.classification === 'hr_review_required'
          && !submitted.hr_review && (validation.revision === undefined || submitted.revision === undefined
            || validation.revision === submitted.revision)
        const stale = validation?.plan_version && collection.plan_version && validation.plan_version < collection.plan_version
        const receipt = receipts[t]
        const canUpload = storage?.configured && !verifying && mayUpload.ok
        return (
          <div key={t} className={`rounded-lg border p-3 ${
            badge.label === 'Validated' ? 'border-green-100 bg-green-50'
              : badge.cls.includes('red') ? 'border-red-100 bg-red-50'
              : badge.cls.includes('amber') ? 'border-amber-100 bg-amber-50' : 'border-slate-100 bg-slate-50'}`}>
            <div className="flex items-start gap-2">
              <badge.Icon className={`w-4 h-4 mt-0.5 shrink-0 ${badge.cls}`} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between gap-1">
                  <span className="text-xs font-semibold text-slate-800">{doc.label ?? humanize(t)}</span>
                  <span className={`text-xs font-medium ${badge.cls}`}>{badge.label}</span>
                </div>
                <p className="text-[11px] text-slate-500 mt-0.5">
                  {doc.required === false ? 'Optional' : 'Mandatory'}{doc.due_date ? ` · due ${doc.due_date}` : ''}{doc.reason ? ` · ${doc.reason}` : ''}
                </p>
                {submitted && (
                  <p className="text-[11px] text-slate-500 mt-1">
                    Revision {submitted.revision} · {submitted.source} · {fmtTime(submitted.submitted_at)}
                    {submitted.hr_review?.decision && ` · HR ${submitted.hr_review.decision}`}
                  </p>
                )}
                {stale && <p className="text-[11px] text-orange-600 mt-1">Validated against plan v{validation?.plan_version}: superseded by v{collection.plan_version}.</p>}
                {validation?.findings && validation.findings.length > 0 && (
                  <p className="text-[11px] text-slate-600 mt-1">Findings: {validation.findings.join(', ')}
                    {validation.required_next_action && ` · next: ${humanize(validation.required_next_action.toLowerCase())}`}</p>
                )}
                {corrections.map((c, i) => (
                  <p key={i} className="text-[11px] text-red-600 mt-1">⚠ {humanize(c.action)}: {c.reason_codes.join(', ')}</p>
                ))}
                {receipt && (
                  <div className="text-[11px] text-slate-500 mt-1 space-y-0.5">
                    <p>Stored: {receipt.bucket}/{receipt.storage_path.split('/').slice(-1)[0]} ({receipt.storage_status},
                      {' '}{Math.max(1, Math.round(receipt.size_bytes / 1024))} KB)</p>
                    <p>Metadata row: {receipt.metadata_persisted ? 'candidate_documents ✓' : `not written (${receipt.metadata_reason})`}</p>
                    <p>Extraction: {receipt.extraction.readable ? 'readable' : 'unreadable'} · confidence {receipt.extraction.confidence.toFixed(2)}
                      {receipt.extraction.expiry_date && ` · expiry ${receipt.extraction.expiry_date}`}
                      {receipt.extraction.engine_issue && ` · ${receipt.extraction.engine_issue}`}</p>
                  </div>
                )}
                {errors[t] && <p className="text-[11px] text-red-600 mt-1">{errors[t]}</p>}
                {errors[`view:${t}`] && <p className="text-[11px] text-red-600 mt-1">{errors[`view:${t}`]}</p>}
                {errors[`review:${t}`] && <p className="text-[11px] text-red-600 mt-1">{errors[`review:${t}`]}</p>}
                {errors[`drop:${t}`] && <p className="text-[11px] text-red-600 mt-1">{errors[`drop:${t}`]}</p>}
                {optional && <p className="text-[11px] text-slate-500 mt-1">Optional — validated when supplied; never blocks the document gate.</p>}

                <div className="flex flex-wrap items-center gap-3 mt-2">
                  {canUpload && (
                    <button disabled={busy !== null} onClick={() => inputs.current[t]?.click()}
                      className="flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-800 disabled:opacity-40">
                      {busy === t ? <Loader className="w-3 h-3 animate-spin" /> : <Upload className="w-3 h-3" />}
                      {busy === t ? 'Uploading & validating…' : submitted ? 'Replace' : 'Upload'}
                    </button>
                  )}
                  <input ref={el => { inputs.current[t] = el }} type="file" accept={accept} className="hidden"
                    onChange={e => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void upload(t, f) }} />
                  {submitted?.document_ref?.startsWith('storage:') && mayView.ok && (
                    <button disabled={busy !== null} onClick={() => openDoc(t)}
                      className="flex items-center gap-1 text-xs font-medium text-slate-600 hover:text-slate-800 disabled:opacity-40">
                      <Eye className="w-3 h-3" /> View
                    </button>
                  )}
                </div>

                {!optional && !verifying && mayChangeRequirements.ok && (
                  <details className="mt-2 text-[11px]">
                    <summary className="cursor-pointer text-slate-500 hover:text-slate-700">Not required for this case…</summary>
                    <div className="mt-1.5 space-y-1.5">
                      <input value={notes[`drop:${t}`] ?? ''} onChange={e => setNotes(n => ({ ...n, [`drop:${t}`]: e.target.value }))}
                        placeholder="Reason (recorded with the change)" maxLength={200}
                        className="w-full rounded border border-slate-300 px-2 py-1 text-xs" />
                      <p className="text-slate-500">UC02 replans the case without this document (a new plan version); UC03 then collects against it.</p>
                      <button disabled={busy !== null} onClick={() => dropRequirement(t)}
                        className="px-2 py-1 text-xs font-medium rounded border border-slate-300 hover:bg-white disabled:opacity-40">
                        {busy === `drop:${t}` ? 'Replanning…' : 'Remove requirement'}
                      </button>
                    </div>
                  </details>
                )}
                {needsReview && !mayReview.ok && <p className="text-[11px] text-slate-500 mt-2">HR review {mayReview.reason}.</p>}
                {needsReview && mayReview.ok && (
                  <div className="mt-2 border-t border-amber-200 pt-2 space-y-1.5">
                    <input value={notes[t] ?? ''} onChange={e => setNotes(n => ({ ...n, [t]: e.target.value }))}
                      placeholder="Review note (required to reject)" maxLength={500}
                      className="w-full rounded border border-slate-300 px-2 py-1 text-xs" />
                    <div className="flex gap-2">
                      <button disabled={busy !== null} onClick={() => review(t, 'approved')}
                        className="px-2 py-1 text-xs font-medium rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-40">Approve</button>
                      <button disabled={busy !== null} onClick={() => review(t, 'rejected')}
                        className="px-2 py-1 text-xs font-medium rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-40">Reject</button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
