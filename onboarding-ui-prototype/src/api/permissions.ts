/**
 * What the current identity may do, as the backend's policy says (GET /v1/orchestration/me). Read once per
 * page load (switching identity reloads the page). Advisory only: every route still enforces its own check.
 */
import { useEffect, useState } from 'react'
import apiClient from './apiClient'

export type Action = 'read_cases' | 'start_case' | 'resume_case' | 'retry_stage' | 'submit_documents' |
  'review_documents' | 'view_documents' | 'read_identity' | 'approve_forms' | 'change_document_requirements' |
  'uc05_operations'

export interface Me {
  user_id: string
  client_id: string
  roles: string[]
  permissions: Record<Action, boolean>
  required_roles: Record<Action, string[]>
}

let cached: Promise<Me> | null = null

export function usePermissions(): Me | null {
  const [me, setMe] = useState<Me | null>(null)
  useEffect(() => {
    let alive = true
    cached ??= apiClient.get<Me>('/v1/orchestration/me').then(r => r.data)
    cached.then(m => { if (alive) setMe(m) }).catch(() => { cached = null })
    return () => { alive = false }
  }, [])
  return me
}

/** Whether `action` is allowed (true while unknown: the backend still decides), and why not. */
export function allowed(me: Me | null, action: Action): { ok: boolean; reason: string | null } {
  if (!me) return { ok: true, reason: null }
  if (me.permissions[action]) return { ok: true, reason: null }
  const roles = me.required_roles[action] ?? []
  return { ok: false, reason: `requires ${roles.length ? roles.join(' or ') : 'a role'} — you are acting as ${me.user_id} (${me.roles.join(', ')})` }
}
