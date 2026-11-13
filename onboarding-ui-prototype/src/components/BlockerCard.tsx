/**
 * One blocker as the orchestrator recorded it: what is missing, who must act, what to do, and whether the stage can
 * be retried afterwards. "Resolve" is offered only where an orchestrator route resolves it (equipment/access requests,
 * offer-acceptance event reference); everything else is shown as an external dependency, never faked.
 * For NO_ITEMS_REQUESTED, UC04's skill recommend_role_standard_items may recommend the role's standard package: it
 * pre-fills the form, labelled as UC04's recommendation, and HR reviews, edits and saves it. Nothing is sent until then.
 */
import { useState } from 'react'
import { AlertTriangle, Loader, Sparkles } from 'lucide-react'
import { apiErrorMessage } from '../api/apiClient'
import { recordEquipmentAccessRequests, recordOfferEvent, restartUc05AfterRecordsLost, retryApproval } from '../api/onboardingApi'
import type { Blocker, CaseView } from '../api/onboardingApi'
import { allowed, usePermissions } from '../api/permissions'

function split(text: string): string[] {
  return text.split(/[,\s]+/).map(t => t.trim()).filter(Boolean)
}

export default function BlockerCard({ blocker, view, onApply }: { blocker: Blocker; view: CaseView; onApply: (v: CaseView) => void }) {
  const me = usePermissions()
  const restart = blocker.resolution === 'restart_uc05_after_records_lost'
  const mayResolve = allowed(me, restart ? 'uc05_operations' : 'resume_case')
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const rec = blocker.recommendation
  const recorded = view.equipment_access_requests
  // HR's own earlier entry wins; otherwise start from UC04's recommendation for this role (if any).
  const [equipment, setEquipment] = useState((recorded?.requested_equipment ?? rec?.equipment ?? []).join(', '))
  const [access, setAccess] = useState((recorded?.requested_access ?? rec?.access ?? []).join(', '))
  const prefilled = !recorded && !!rec
  const [eventId, setEventId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(fn: () => Promise<CaseView>) {
    setBusy(true); setError(null)
    try { onApply(await fn()); setOpen(false) } catch (e) { setError(apiErrorMessage(e)) } finally { setBusy(false) }
  }

  const resolvable = blocker.resolution === 'provide_equipment_access_requests' || blocker.resolution === 'provide_offer_event'
    || restart

  return (
    <div className="mb-2 rounded border border-amber-200 bg-amber-50 p-2.5 text-[11px] text-slate-700">
      <div className="flex items-start gap-1.5">
        <AlertTriangle className="w-3.5 h-3.5 text-amber-600 shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <p className="font-semibold text-amber-800">{blocker.code}</p>
          <p className="mt-0.5"><span className="text-slate-500">Reason: </span>{blocker.reason}</p>
          {blocker.required_from && <p><span className="text-slate-500">Required from: </span>{blocker.required_from}</p>}
          {blocker.required_action && <p><span className="text-slate-500">Required action: </span>{blocker.required_action}</p>}
          {blocker.required_data && <p><span className="text-slate-500">Required data: </span><code>{blocker.required_data}</code></p>}
          {blocker.dependency && <p><span className="text-slate-500">Dependency: </span>{blocker.dependency}</p>}
          <p><span className="text-slate-500">After resolution: </span>{blocker.retryable === false ? 'not retryable — needs review'
            : 'retry / resume the stage'}</p>
          {!blocker.required_from && blocker.owner && <p><span className="text-slate-500">Owner: </span>{blocker.owner}</p>}

          {rec && (
            <div className="mt-1.5 rounded border border-indigo-200 bg-white p-2">
              <p className="flex items-center gap-1 font-semibold text-indigo-700">
                <Sparkles className="w-3 h-3" /> Recommended by UC04 for role <code>{rec.role_code}</code>
              </p>
              <p className="text-slate-500">Skill <code>{rec.skill}</code>{rec.config_version && <> · configuration <code>{rec.config_version}</code></>}</p>
              <p><span className="text-slate-500">Equipment: </span>{rec.equipment === null ? 'no recommendation (not configured for this role)'
                : rec.equipment.length ? rec.equipment.join(', ') : 'none for this role'}</p>
              <p><span className="text-slate-500">Access: </span>{rec.access === null ? 'no recommendation (not configured for this role)'
                : rec.access.length ? rec.access.join(', ') : 'none for this role'}</p>
              <p className="text-slate-500">A suggestion only: nothing is requested until you save it, and each item still needs its approval.</p>
            </div>
          )}
          {resolvable && !open && (
            <button disabled={!mayResolve.ok} title={mayResolve.reason ?? undefined} onClick={() => setOpen(true)}
              className="mt-1.5 px-2 py-1 rounded bg-indigo-600 text-white font-medium hover:bg-indigo-700 disabled:opacity-40">
              {restart ? 'Restart UC05 for this case' : 'Resolve blocker'}
            </button>
          )}
          {resolvable && !mayResolve.ok && <p className="mt-1 text-slate-500">Resolving {mayResolve.reason}.</p>}
          {blocker.resolution === 'retry_apply_decision' && blocker.approval_id && (
            <>
              <button disabled={busy || !allowed(me, 'resume_case').ok} title={allowed(me, 'resume_case').reason ?? undefined}
                onClick={() => submit(() => retryApproval(view.case_id, blocker.approval_id as string))}
                className="mt-1.5 inline-flex items-center gap-1 px-2 py-1 rounded bg-indigo-600 text-white font-medium hover:bg-indigo-700 disabled:opacity-40">
                {busy && <Loader className="w-3 h-3 animate-spin" />} Retry applying the decision
              </button>
              <p className="mt-1 text-slate-500">The recorded decision stands; only its follow-up (e.g. confirming the booking) runs again.</p>
            </>
          )}
          {blocker.resolution === 'decide_in_inbox' && (
            <p className="mt-1.5 text-slate-500">Decided in the Pending Approvals inbox by {blocker.owner ?? blocker.required_from ?? 'the approver'}
              {' '}(switch identity to that role); UC04 re-evaluates after each decision.</p>
          )}
          {blocker.resolution === 'external' && (
            <p className="mt-1.5 text-slate-500">Resolved outside this system by {blocker.required_from ?? 'the owner'}; then retry the stage.</p>
          )}

          {open && blocker.resolution === 'provide_equipment_access_requests' && (
            <div className="mt-2 space-y-1.5">
              <label className="block">Equipment item ids (from the client's catalogue)
                <input value={equipment} onChange={e => setEquipment(e.target.value)} placeholder="e.g. laptop-standard, headset-basic"
                  className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1" /></label>
              <label className="block">Access item ids
                <input value={access} onChange={e => setAccess(e.target.value)} placeholder="e.g. email-teams-account"
                  className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1" /></label>
              {prefilled && <p className="text-indigo-700">Pre-filled from UC04's recommendation. Review and edit before saving.</p>}
              {rec && !prefilled && (
                <button type="button" onClick={() => { setEquipment((rec.equipment ?? []).join(', ')); setAccess((rec.access ?? []).join(', ')) }}
                  className="text-indigo-700 underline">Use UC04's recommended items</button>
              )}
              <p className="text-slate-500">UC04 checks each id against the client's approved catalogue and its approval rules.</p>
              <div className="flex gap-2">
                <button disabled={busy} onClick={() => submit(() => recordEquipmentAccessRequests(view.case_id, split(equipment), split(access)))}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded bg-indigo-600 text-white disabled:opacity-50">
                  {busy && <Loader className="w-3 h-3 animate-spin" />} Save &amp; retry UC04
                </button>
                <button disabled={busy} onClick={() => setOpen(false)} className="px-2 py-1 text-slate-600">Cancel</button>
              </div>
            </div>
          )}
          {open && blocker.resolution === 'provide_offer_event' && (
            <div className="mt-2 space-y-1.5">
              <label className="block">Offer-acceptance event / message id
                <input value={eventId} onChange={e => setEventId(e.target.value)} className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1" /></label>
              <p className="text-slate-500">UC05 verifies the event exists and belongs to this employee.</p>
              <div className="flex gap-2">
                <button disabled={busy || !eventId.trim()} onClick={() => submit(() => recordOfferEvent(view.case_id, eventId.trim()))}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded bg-indigo-600 text-white disabled:opacity-50">
                  {busy && <Loader className="w-3 h-3 animate-spin" />} Save &amp; retry UC05
                </button>
                <button disabled={busy} onClick={() => setOpen(false)} className="px-2 py-1 text-slate-600">Cancel</button>
              </div>
            </div>
          )}
          {open && restart && (
            <div className="mt-2 space-y-1.5">
              <p className="text-slate-600">UC05 lost this case's records. Restarting re-creates the UC05 case from the approved
                form; the induction (and every later UC05 step) needs HR approval again. Nothing from the lost records is
                replayed. UC05 is asked first: if it still holds the records, nothing is restarted.</p>
              <label className="block">What did you check? (calendar holds, invitations, messages still live elsewhere)
                <textarea value={reason} onChange={e => setReason(e.target.value)} rows={2} maxLength={500}
                  className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1" /></label>
              <div className="flex gap-2">
                <button disabled={busy || !reason.trim()} onClick={() => submit(() => restartUc05AfterRecordsLost(view.case_id, reason.trim()))}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded bg-indigo-600 text-white disabled:opacity-50">
                  {busy && <Loader className="w-3 h-3 animate-spin" />} Restart UC05
                </button>
                <button disabled={busy} onClick={() => setOpen(false)} className="px-2 py-1 text-slate-600">Cancel</button>
              </div>
            </div>
          )}
          {error && <p className="mt-1 text-red-600">{error}</p>}
        </div>
      </div>
    </div>
  )
}
