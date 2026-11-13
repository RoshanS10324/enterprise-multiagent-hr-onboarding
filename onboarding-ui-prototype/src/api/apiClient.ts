/**
 * Axios instance for the orchestrator API (HR-Onboarding-Common/scripts/serve_orchestrator.py).
 *
 * Identity: no identity provider is integrated yet. In local development the backend runs with
 * --dev-identity and trusts the three X-* headers below (InsecureHeaderIdentityProvider). They identify
 * a development user; they are not secrets, and switching between them is a development convenience only --
 * with a real identity provider the user is whoever signed in. Never put a Supabase key or any credential here — every
 * Supabase call (database and Storage) is made by the backend.
 */
import axios, { AxiosError } from 'axios'

export const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export interface DevIdentity {
  key: string
  label: string
  userId: string
  clientId: string
  roles: string
}

const CLIENT = import.meta.env.VITE_DEV_CLIENT_ID ?? 'CLIENT-TEST-A'

/** Development identities the UI can act as (VITE_DEV_IDENTITIES: a JSON array overrides the defaults). The
 * requester and the approver are different people: the form approval gate enforces separation of duties. */
export const DEV_IDENTITIES: DevIdentity[] = (() => {
  try {
    const raw = import.meta.env.VITE_DEV_IDENTITIES
    if (raw) return JSON.parse(raw) as DevIdentity[]
  } catch { /* fall back to the defaults */ }
  return [
    // hr_admin (document review, approvals routing) + hr_onboarding_officer: UC04 runs on behalf of the person who
    // triggered the step and allows only hr_onboarding_officer / hiring_manager / onboarding_orchestrator callers
    // (UC04 policy/uc04_policy.json allowed_caller_roles).
    { key: 'user:hr-admin-01', label: 'HR Admin & Onboarding Officer (submits forms)',
      userId: import.meta.env.VITE_DEV_USER_ID ?? 'user:hr-admin-01',
      clientId: CLIENT, roles: import.meta.env.VITE_DEV_USER_ROLES ?? 'hr_admin,hr_onboarding_officer' },
    // The form approver role is the backend policy's (gate onboarding_form_approval: hr_lead, a role UC02 accepts).
    { key: 'user:hr-lead-01', label: 'HR Lead (approves forms)', userId: 'user:hr-lead-01', clientId: CLIENT,
      roles: 'hr_lead' },
  ]
})()

const STORAGE_KEY = 'onboarding.devIdentity'

// Earlier builds saved a role-style key; they map to the same users.
const LEGACY_KEYS: Record<string, string> = { requester: 'user:hr-admin-01', approver: 'user:hr-lead-01' }

function savedKey(): string | null {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    return saved ? LEGACY_KEYS[saved] ?? saved : null
  } catch { return null }
}

const SAVED_IDENTITY_KEY = 'onboarding.devIdentity.record'

/** The identity saved on the last switch, as it was then (a registry-only user such as a team lead is not among the
 * built-in identities, and the registry is read only after the first requests). */
function savedIdentity(): DevIdentity | null {
  try {
    const raw = localStorage.getItem(SAVED_IDENTITY_KEY)
    const rec = raw ? JSON.parse(raw) as DevIdentity : null
    return rec && rec.key === savedKey() && rec.userId && rec.clientId ? rec : null
  } catch { return null }
}

function initialIdentity(): DevIdentity {
  // Never fall back to another (more privileged) user while the registry is still loading: the first requests
  // would otherwise go out as the default HR identity.
  return DEV_IDENTITIES.find(i => i.key === savedKey()) ?? savedIdentity() ?? DEV_IDENTITIES[0]
}

/** Adopt the backend's development identity registry (GET /v1/orchestration/dev/identities): the labels and roles
 * shown are the server's, and the server resolves roles itself (the X-User-Roles header is then ignored). */
export function adoptRegistry(entries: Array<{ user_id: string; label: string; roles: string[]; clients: string[] }>): void {
  const usable = entries.filter(e => e.clients.length > 0)
  if (!usable.length) return
  DEV_IDENTITIES.splice(0, DEV_IDENTITIES.length, ...usable.map(e => ({
    key: e.user_id, label: e.label, userId: e.user_id, roles: e.roles.join(','),
    clientId: e.clients.includes(CLIENT) ? CLIENT : e.clients[0],
  })))
  current = DEV_IDENTITIES.find(i => i.key === savedKey()) ?? DEV_IDENTITIES[0]
}

let current = initialIdentity()

/** The identity the UI currently acts as. */
export function currentIdentity(): DevIdentity {
  return current
}

/** Switch identity and reload, so every view is re-read under the new identity. */
export function switchIdentity(key: string): void {
  const next = DEV_IDENTITIES.find(i => i.key === key)
  if (!next) return
  current = next
  try {
    localStorage.setItem(STORAGE_KEY, key)
    localStorage.setItem(SAVED_IDENTITY_KEY, JSON.stringify(next))
  } catch { /* not persisted; still applied for this page */ }
  window.location.reload()
}

const apiClient = axios.create({
  baseURL: BASE_URL,
  // UC stages run synchronously inside a request (UC01 -> UC02 -> UC03 on start); give them time.
  timeout: 120_000,
  headers: { 'Content-Type': 'application/json' },
})

// Identity headers on every request, from the identity currently selected.
apiClient.interceptors.request.use(config => {
  config.headers.set('X-User-Id', current.userId)
  config.headers.set('X-Client-Id', current.clientId)
  config.headers.set('X-User-Roles', current.roles)
  return config
})

// Log only method, path and status: never request or response bodies (they can carry employee data).
apiClient.interceptors.response.use(
  r => r,
  (err: AxiosError) => {
    const status = err.response?.status ?? 'network'
    console.warn(`[API] ${err.config?.method?.toUpperCase()} ${err.config?.url} → ${status}`)
    return Promise.reject(err)
  },
)

/** A readable message for an API error: the backend's `detail` (ids and reasons only), or the transport error. */
export function apiErrorMessage(err: unknown): string {
  const e = err as AxiosError<{ detail?: unknown }>
  if (e?.response) {
    const detail = e.response.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join('.')}: ${d.msg ?? ''}`)
        .join('; ')
    }
    return `HTTP ${e.response.status}`
  }
  if (e?.code === 'ECONNABORTED') return 'The request timed out'
  return 'The backend is unreachable'
}

export function isNetworkError(err: unknown): boolean {
  const e = err as AxiosError
  return !!e && !e.response
}

export default apiClient
