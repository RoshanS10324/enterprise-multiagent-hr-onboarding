/**
 * Live data hooks. Controlled polling only (no Supabase Realtime is configured for the frontend, and
 * the browser never talks to Supabase): one request in flight per hook, intervals that slow down when
 * nothing is moving, cleanup on unmount. On a failed refresh the last known data is kept and marked
 * stale; nothing synthetic is ever substituted.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { apiErrorMessage, isNetworkError } from './apiClient'
import { getCase, getStageExecutions, healthCheck, listCases } from './onboardingApi'
import type { CaseSummary, CaseView, HealthView, StageExecution } from './onboardingApi'

export type BackendStatus = 'checking' | 'online' | 'offline'

/** True while mounted. Set again on every mount: React StrictMode (development) mounts, unmounts and
 * remounts each component, and a flag cleared on the simulated unmount would discard every later result. */
function useMounted() {
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  return mounted
}

function usePoll(fn: () => Promise<void>, intervalMs: number | null, deps: unknown[]) {
  const inFlight = useRef(false)
  const run = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try { await fn() } finally { inFlight.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => {
    void run()
    if (intervalMs === null) return
    const id = setInterval(() => { if (document.visibilityState === 'visible') void run() }, intervalMs)
    return () => clearInterval(id)
  }, [run, intervalMs])
  return run
}

export function useBackendHealth() {
  const [status, setStatus] = useState<BackendStatus>('checking')
  const [health, setHealth] = useState<HealthView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const mounted = useMounted()

  const check = usePoll(async () => {
    try {
      const h = await healthCheck()
      if (!mounted.current) return
      setHealth(h); setStatus('online'); setError(null)
    } catch (e) {
      if (!mounted.current) return
      setStatus('offline'); setError(apiErrorMessage(e))
    }
  }, 15_000, [])

  const retry = useCallback(() => { setStatus('checking'); void check() }, [check])
  return { status, health, error, retry }
}

export function useCaseList(enabled: boolean) {
  const [cases, setCases] = useState<CaseSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const mounted = useMounted()

  const refresh = usePoll(async () => {
    if (!enabled) return
    try {
      const data = await listCases()
      if (!mounted.current) return
      setCases(data); setError(null); setStale(false); setLastUpdated(new Date())
    } catch (e) {
      if (!mounted.current) return
      setError(apiErrorMessage(e)); setStale(true)
    }
  }, enabled ? 10_000 : null, [enabled])

  return { cases, error, stale, lastUpdated, refresh }
}

const FAST = new Set(['running', 'pending', 'retry_pending'])
const DONE = new Set(['completed', 'failed'])

export function useCaseDetail(caseId: string | null) {
  const [caseView, setCaseView] = useState<CaseView | null>(null)
  const [executions, setExecutions] = useState<Record<string, StageExecution> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [stale, setStale] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const mounted = useMounted()
  const current = useRef<string | null>(caseId)
  useEffect(() => {
    current.current = caseId
    setCaseView(null); setExecutions(null); setError(null); setNotFound(false); setStale(false)
  }, [caseId])

  const status = caseView?.workflow_status ?? ''
  const interval = !caseId ? null : FAST.has(status) ? 3_000 : DONE.has(status) ? 30_000 : 8_000

  const refresh = usePoll(async () => {
    if (!caseId) return
    try {
      const [view, stages] = await Promise.all([getCase(caseId), getStageExecutions(caseId).catch(() => null)])
      if (!mounted.current || current.current !== caseId) return
      setCaseView(view); if (stages) setExecutions(stages)
      setError(null); setStale(false); setNotFound(false); setLastUpdated(new Date())
    } catch (e) {
      if (!mounted.current || current.current !== caseId) return
      const status404 = (e as { response?: { status?: number } })?.response?.status === 404
      setNotFound(status404)
      setError(apiErrorMessage(e)); setStale(isNetworkError(e) || !status404)
    }
  }, interval, [caseId])

  /** Apply a CaseView a command returned, then re-read the stage executions. */
  const apply = useCallback((view: CaseView) => {
    if (current.current !== view.case_id && current.current !== null) {
      // The case was renamed by UC01 at intake: follow the authoritative id.
      current.current = view.case_id
    }
    setCaseView(view); setStale(false); setError(null); setLastUpdated(new Date())
    void refresh()
  }, [refresh])

  return { caseView, executions, error, notFound, stale, lastUpdated, refresh, apply }
}
