/**
 * Approve / Reject for one pending approval, through the real decision API
 * (POST /v1/orchestration/cases/{case}/approvals/{id}/decision). The backend's inbox decides whether the
 * current user may: the approver role, the tenant, and separation of duties for the form approval. When the
 * caller already knows it may not, the buttons are disabled with the reason.
 */
import { useState } from 'react'
import { CheckCircle, Loader, XCircle } from 'lucide-react'
import { apiErrorMessage } from '../api/apiClient'
import { decideApproval } from '../api/onboardingApi'
import type { CaseView } from '../api/onboardingApi'

interface Props {
  caseId: string
  approvalId: string
  canDecide: boolean
  reason?: string | null
  /** What is being approved, for the confirmation text. */
  subject: string
  /** What approving does next, for the confirmation text. */
  effect?: string
  onDecided: (view: CaseView) => void
  compact?: boolean
}

export default function ApprovalActions({ caseId, approvalId, canDecide, reason, subject, effect, onDecided, compact }: Props) {
  const [mode, setMode] = useState<'idle' | 'confirm' | 'reject'>('idle')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function send(decision: 'approved' | 'rejected') {
    if (busy) return
    if (decision === 'rejected' && !note.trim()) { setError('Give a reason for the rejection; it is recorded with the decision.'); return }
    setBusy(true); setError(null)
    try {
      onDecided(await decideApproval(caseId, approvalId, decision, note.trim() || undefined))
      setMode('idle'); setNote('')
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  if (!canDecide) {
    return <p className="text-[11px] text-slate-500">{reason ? `You cannot decide this: ${reason}.` : 'You cannot decide this approval.'}</p>
  }

  return (
    <div className={compact ? '' : 'mt-1'}>
      {mode === 'idle' && (
        <div className="flex gap-2">
          <button onClick={() => { setMode('confirm'); setError(null) }}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded bg-green-600 text-white hover:bg-green-700">
            <CheckCircle className="w-3.5 h-3.5" /> Approve
          </button>
          <button onClick={() => { setMode('reject'); setError(null) }}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded bg-red-600 text-white hover:bg-red-700">
            <XCircle className="w-3.5 h-3.5" /> Reject
          </button>
        </div>
      )}
      {mode === 'confirm' && (
        <div className="rounded border border-green-200 bg-green-50 p-2 space-y-1.5">
          <p className="text-[11px] text-green-800">Approve {subject}?{effect ? ` ${effect}` : ''} Your decision is recorded and audited.</p>
          <input value={note} onChange={e => setNote(e.target.value)} maxLength={1000} placeholder="Note (optional)"
            className="w-full rounded border border-slate-300 px-2 py-1 text-xs" />
          <div className="flex gap-2">
            <button disabled={busy} onClick={() => send('approved')}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-50">
              {busy && <Loader className="w-3 h-3 animate-spin" />} Confirm approval
            </button>
            <button disabled={busy} onClick={() => setMode('idle')} className="px-2.5 py-1 text-xs text-slate-600 hover:text-slate-800">Cancel</button>
          </div>
        </div>
      )}
      {mode === 'reject' && (
        <div className="rounded border border-red-200 bg-red-50 p-2 space-y-1.5">
          <input value={note} onChange={e => setNote(e.target.value)} maxLength={1000} placeholder="Reason for rejection (required)"
            className="w-full rounded border border-slate-300 px-2 py-1 text-xs" />
          <div className="flex gap-2">
            <button disabled={busy} onClick={() => send('rejected')}
              className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50">
              {busy && <Loader className="w-3 h-3 animate-spin" />} Confirm rejection
            </button>
            <button disabled={busy} onClick={() => setMode('idle')} className="px-2.5 py-1 text-xs text-slate-600 hover:text-slate-800">Cancel</button>
          </div>
        </div>
      )}
      {error && <p className="text-[11px] text-red-600 mt-1">{error}</p>}
    </div>
  )
}
