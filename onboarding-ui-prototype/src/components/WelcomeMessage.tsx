/**
 * The joining-day welcome message (GET /cases/{id}/welcome-message): UC05's draft once it is written on the joining
 * day, before that a preview from the client's approved template. Read-only -- the message is drafted, approved by
 * HR and sent by UC05 itself. It names the joiner, so it is shown only to roles that may read names.
 */
import { useEffect, useState } from 'react'
import { Mail } from 'lucide-react'
import { apiErrorMessage } from '../api/apiClient'
import { getWelcomeMessage } from '../api/onboardingApi'
import type { WelcomeMessageView } from '../api/onboardingApi'
import { allowed, usePermissions } from '../api/permissions'

export default function WelcomeMessage({ caseId, refreshKey }: { caseId: string; refreshKey?: string }) {
  const me = usePermissions()
  const mayRead = allowed(me, 'read_identity')
  const [msg, setMsg] = useState<WelcomeMessageView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!mayRead.ok) return
    let alive = true
    getWelcomeMessage(caseId).then(m => { if (alive) { setMsg(m); setError(null) } })
      .catch(e => { if (alive) setError(apiErrorMessage(e)) })
    return () => { alive = false }
  }, [caseId, refreshKey, mayRead.ok])

  if (!mayRead.ok) return <p className="text-[11px] text-slate-500">The welcome message {mayRead.reason}.</p>
  if (error) return <p className="text-[11px] text-red-600">Welcome message unavailable: {error}</p>
  if (!msg) return <p className="text-[11px] text-slate-400">Loading the welcome message…</p>

  if (msg.status === 'no_welcome_configuration') {
    return (
      <div className="rounded border border-amber-200 bg-amber-50 p-2 text-[11px] text-slate-700">
        <p className="font-semibold text-amber-800">No welcome template for this client</p>
        <p>{msg.detail}</p>
        <p className="mt-1 text-slate-500">UC05 drafts the welcome from the client's approved "welcome" template
          ({msg.source === 'supabase' ? 'client_configs.settings_payload.uc05_welcome' : "UC05's local client configuration"}).
          Without one the joining-day welcome stops with MISSING_DATA.</p>
      </div>
    )
  }
  if (msg.status !== 'draft' && msg.status !== 'preview') {
    return <p className="text-[11px] text-slate-500">No welcome message yet ({msg.status.replace(/_/g, ' ')}{msg.detail ? `: ${msg.detail}` : ''}).</p>
  }
  const isDraft = msg.status === 'draft'
  return (
    <div className="rounded border border-slate-200 bg-white p-2.5 text-xs text-slate-700">
      <p className="flex items-center gap-1 text-[11px] font-semibold text-indigo-700">
        <Mail className="w-3.5 h-3.5" />
        {isDraft ? `Welcome draft · ${msg.draft_status ?? 'drafted'}` : 'Welcome message preview — not sent'}
      </p>
      <p className="mt-1.5"><span className="text-slate-500">Subject: </span><span className="font-medium">{msg.subject}</span></p>
      <pre className="mt-1 whitespace-pre-wrap font-sans text-xs leading-snug text-slate-700">{msg.body}</pre>
      <p className="mt-1.5 text-[11px] text-slate-500">
        {isDraft
          ? 'As UC05 drafted it on the joining day. HR approves (or edits) it before UC05 sends it.'
          : `From the client's approved template. On the joining day (${msg.joining_date ?? '—'}) UC05 drafts it, ${msg.personal_note ? 'adds a short personal note, ' : ''}and HR approves it before it is sent.`}
      </p>
    </div>
  )
}
