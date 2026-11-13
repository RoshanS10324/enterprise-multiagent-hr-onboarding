/**
 * Onboarding Agent dashboard: backend health, the tenant's cases, and the selected case's workflow and
 * stage details, all read from the orchestrator. The selected case lives in the URL (?case=…), so a
 * refresh reloads the same persisted case and never starts a new one.
 */
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AlertCircle, Clock, FlaskConical, Plus, RefreshCw, Wifi, WifiOff } from 'lucide-react'
import { BASE_URL, DEV_IDENTITIES, adoptRegistry, currentIdentity, switchIdentity } from '../api/apiClient'
import { getDevIdentities, getProfile } from '../api/onboardingApi'
import { allowed, usePermissions } from '../api/permissions'
import type { ApprovalInboxItem, CaseProfile, CaseView } from '../api/onboardingApi'
import { useBackendHealth, useCaseDetail, useCaseList } from '../api/useOnboardingData'
import ApprovalsInbox from '../components/ApprovalsInbox'
import CaseList from '../components/CaseList'
import StageSidebar from '../components/StageSidebar'
import StartOnboardingModal from '../components/StartOnboardingModal'
import WorkflowDiagram from '../components/WorkflowDiagram'
import { STAGES, TONE_BADGE, currentStageTitle, fmtTime, roleLabel, stageStatusView, workflowStatusView } from '../lib/stages'

const PROGRESS_STAGES = ['intake', 'plan_tasks', 'document_collection', 'document_submission', 'validate_documents',
  'equipment_access', 'orientation_scheduling', 'buddy_assignment', 'team_introduction', 'day_one_readiness',
  'joining_day_welcome']

export default function Dashboard() {
  const [params, setParams] = useSearchParams()
  const caseId = params.get('case')
  const [selectedStage, setSelectedStage] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [profile, setProfile] = useState<CaseProfile | null>(null)
  const me = usePermissions()
  const mayStart = allowed(me, 'start_case')

  const health = useBackendHealth()
  const online = health.status === 'online'
  const devMode = health.health?.identity === 'dev-header-trust' || health.health?.identity === 'dev-registry'
  const [identities, setIdentities] = useState(() => [...DEV_IDENTITIES])
  useEffect(() => {
    if (health.health?.identity !== 'dev-registry') return
    let alive = true
    getDevIdentities().then(list => { if (alive && list) { adoptRegistry(list); setIdentities([...DEV_IDENTITIES]) } })
    return () => { alive = false }
  }, [health.health?.identity])
  const list = useCaseList(online && (me?.permissions.read_cases ?? true))  // a team lead/buddy lists no cases
  const detail = useCaseDetail(me?.permissions.read_cases === false ? null : caseId)  // no case reads for a team lead
  const view = detail.caseView
  // Demo mode = the server runs UC stand-ins. (The backend labels every outcome data_origin=synthetic in
  // both modes, because all UC connectors are mocks; that is shown separately.)
  const demo = health.health?.mode === 'demo'
  const syntheticConnectors = !demo && view?.data_origin === 'synthetic'

  useEffect(() => {
    setProfile(null)
    if (!caseId || !online) return
    let alive = true
    getProfile(caseId).then(p => { if (alive) setProfile(p) }).catch(() => { /* shown via the case error */ })
    return () => { alive = false }
  }, [caseId, online])

  // UC01 may give the case its authoritative id at intake: follow it in the URL.
  useEffect(() => {
    if (view && caseId && view.case_id !== caseId) setParams({ case: view.case_id }, { replace: true })
  }, [view, caseId, setParams])

  function selectCase(id: string) {
    setSelectedStage(null)
    setParams({ case: id })
  }

  function onStarted(started: CaseView) {
    setShowForm(false)
    setSelectedStage('intake')
    setParams({ case: started.case_id })
    detail.apply(started)
    void list.refresh()
  }

  function onApply(updated: CaseView) {
    detail.apply(updated)
    void list.refresh()
  }

  /** A decision from the approvals inbox: update the open case if it is that one (UC01 may have renamed it). */
  function onInboxDecision(updated: CaseView, item: ApprovalInboxItem) {
    if (caseId && (caseId === item.case_id || caseId === updated.case_id)) onApply(updated)
    else void list.refresh()
  }

  function openCase(id: string, stage: string) {
    setParams({ case: id })
    setSelectedStage(stage)
  }

  function refreshAll() {
    health.retry()
    void list.refresh()
    void detail.refresh()
  }

  const done = view ? PROGRESS_STAGES.filter(s => view.stage_statuses[s] === 'complete').length : 0
  const ws = workflowStatusView(view?.workflow_status)
  const selectedCaseSummary = list.cases?.find(c => c.case_id === caseId)

  return (
    <div className="space-y-4">
      {showForm && <StartOnboardingModal onClose={() => setShowForm(false)} onStarted={onStarted} />}

      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-slate-900">Onboarding Agent</h1>
        <div className="flex items-center gap-2">
          <div className={`inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full font-medium ${
            online ? 'bg-green-100 text-green-700' : health.status === 'checking' ? 'bg-blue-100 text-blue-600' : 'bg-red-100 text-red-600'}`}
            title={online ? `${health.health?.agent} v${health.health?.version} · store ${health.health?.state_store ?? '—'}` : health.error ?? ''}>
            {online ? <Wifi className="w-3 h-3" /> : <WifiOff className="w-3 h-3" />}
            {online ? 'Online' : health.status === 'checking' ? 'Connecting…' : 'Offline'}
          </div>
          {demo && (
            <div className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full font-medium bg-amber-100 text-amber-700">
              <FlaskConical className="w-3 h-3" /> Demo mode
            </div>
          )}
          {devMode && (
            <label className="inline-flex items-center gap-1.5 text-xs text-slate-500"
              title={health.health?.identity === 'dev-registry'
                ? 'Development identity: the backend resolves this user’s roles and clients from its registry (config/dev_identities.yaml). With a real identity provider you are whoever signed in.'
                : 'Development identity (the backend trusts identity headers in --dev-identity mode). With a real identity provider you are whoever signed in.'}>
              Acting as
              <select value={currentIdentity().key} onChange={e => switchIdentity(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700">
                {identities.map(i => <option key={i.key} value={i.key}>{i.label} · {i.userId}</option>)}
              </select>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">dev</span>
            </label>
          )}
          <button onClick={refreshAll} className="p-1.5 text-slate-400 hover:text-slate-600" title="Refresh">
            <RefreshCw className={`w-4 h-4 ${health.status === 'checking' ? 'animate-spin' : ''}`} />
          </button>
          <button onClick={() => setShowForm(true)} disabled={!online || !mayStart.ok}
            title={mayStart.ok ? undefined : `Start New Onboarding ${mayStart.reason}`}
            className="inline-flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded-lg hover:bg-indigo-700 shadow-sm disabled:opacity-50 disabled:cursor-not-allowed">
            <Plus className="w-4 h-4" /> Start New Onboarding
          </button>
        </div>
      </div>

      {health.status === 'offline' && (
        <div className="flex items-center gap-2 bg-red-50 border border-red-200 rounded-lg px-4 py-2 text-xs text-red-700">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span className="flex-1">Backend unreachable at {BASE_URL} ({health.error}). No data is shown until it responds.
            {view && ' The case below is the last known state and may be out of date.'}</span>
          <button onClick={health.retry} className="font-semibold underline">Retry</button>
        </div>
      )}
      {demo && (
        <div className="flex items-center gap-2 bg-amber-50 border border-amber-200 rounded-lg px-4 py-2 text-xs text-amber-700">
          <FlaskConical className="w-4 h-4 shrink-0" />
          Demo mode: the backend runs synthetic stand-ins for UC01, UC02, UC04 and UC05 (data_origin = synthetic). Results are not real employee records.
        </div>
      )}
      {health.health?.state_store === 'SupabaseCaseStateStore' && health.health?.uc01_store === 'memory' && (
        <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-lg px-4 py-2 text-xs text-amber-800">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>The workflow state is kept in Supabase, but UC01 runs in memory on this backend and does not store its
            cases in onboarding_cases. A new case therefore stops after its form approval (blocker CASE_NOT_IN_DATABASE).
            Enable UC01’s Supabase mode on the backend (UC01_WORKER_ENV__SUPABASE_URL and
            UC01_WORKER_ENV__UC01_FIELD_ENCRYPTION_KEY), or run it with ORCHESTRATOR_STATE_STORE=sqlite.</span>
        </div>
      )}
      {syntheticConnectors && (
        <div className="flex items-center gap-2 bg-slate-50 border border-slate-200 rounded-lg px-4 py-2 text-xs text-slate-600">
          <FlaskConical className="w-4 h-4 shrink-0" />
          Live agents. The backend labels these results data_origin = synthetic: the UC agents run on synthetic test
          records and mock connectors (email, calendar, IT desk); no real-time external system is connected.
        </div>
      )}
      {(detail.stale || list.stale) && online && (
        <div className="flex items-center gap-2 bg-orange-50 border border-orange-200 rounded-lg px-4 py-2 text-xs text-orange-700">
          <Clock className="w-4 h-4 shrink-0" />
          Showing the last known state (updated {fmtTime((detail.lastUpdated ?? list.lastUpdated)?.toISOString())}); the latest refresh failed: {detail.error ?? list.error}
        </div>
      )}

      <ApprovalsInbox enabled={online} onOpenCase={openCase} onDecided={onInboxDecision} />

      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-6">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-slate-900">Onboarding Cases</h2>
          <span className="text-xs text-slate-400">
            {currentIdentity().clientId} · {list.lastUpdated ? `updated ${list.lastUpdated.toLocaleTimeString()}` : online ? 'loading…' : 'offline'}
          </span>
        </div>
        {me && !me.permissions.read_cases
          ? <p className="text-sm text-slate-500 py-4 text-center">You are acting as {me.user_id} ({me.roles.join(', ')}): this role
              does not list cases. The approvals that name you as the person on record are in the inbox above.</p>
          : list.cases ? <CaseList cases={list.cases} selected={caseId} onSelect={selectCase} />
          : <p className="text-sm text-slate-400 py-4 text-center">{online ? 'Loading cases…' : 'Cases load once the backend is online.'}</p>}
      </div>

      <div className="flex gap-4 items-start">
        <div className={`bg-white rounded-lg border border-slate-200 shadow-sm p-6 transition-all duration-300 ${selectedStage && view ? 'flex-1 min-w-0' : 'w-full'}`}>
          <div className="flex items-start justify-between mb-4 gap-4">
            <div>
              <h2 className="text-lg font-semibold text-slate-900">Onboarding Workflow Architecture</h2>
              {view ? (
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                  <span className="font-medium text-slate-700">{profile?.candidate_name ?? selectedCaseSummary?.profile.candidate_name ?? view.case_id}</span>
                  <span>· {view.case_id}</span>
                  <span className={`px-2 py-0.5 rounded-full font-medium ${TONE_BADGE[ws.tone]}`}>{ws.label}</span>
                  <span>· {done}/{PROGRESS_STAGES.length} stages complete</span>
                  <span>· current: {currentStageTitle(view.current_stage, view.form_approval?.required ? view.form_approval.status : null, view.stage_statuses.intake)}</span>
                  <span>· updated {fmtTime(view.updated_at)}</span>
                </div>
              ) : (
                <p className="mt-1 text-xs text-slate-500">
                  {caseId && detail.notFound ? `Case ${caseId} was not found for this client.`
                    : caseId ? 'Loading case…' : 'Select a case above, or start a new onboarding, to see its live workflow.'}
                </p>
              )}
            </div>
            <span className="text-xs text-slate-400 bg-slate-50 border border-slate-200 px-2 py-1 rounded whitespace-nowrap">
              Click a node to view Input &amp; Output
            </span>
          </div>

          {view && (view.blockers.length > 0 || view.pending_actions.length > 0) && (
            <div className="mb-4 grid gap-2 sm:grid-cols-2">
              {view.pending_actions.slice(0, 4).map((a, i) => {
                const formGate = a.action === 'decide_approval' && a.reference === view.form_approval?.approval_id
                return (
                  <button key={`p${i}`} onClick={() => setSelectedStage(formGate ? 'form_approval' : a.stage)}
                    className="text-left text-xs bg-amber-50 border border-amber-200 rounded px-3 py-2 text-amber-800 hover:bg-amber-100">
                    {formGate ? (
                      <><span className="font-semibold">HR form approval pending</span> · Internal Onboarding Form · required
                        approver: {roleLabel(a.owner)} · requested {fmtTime(view.form_approval?.requested_at)}</>
                    ) : (
                      <><span className="font-semibold">{a.action.replace(/_/g, ' ')}</span> · {STAGES[a.stage]?.title ?? a.stage}
                        {a.owner ? ` · ${a.owner}` : ''}{a.documents?.length ? ` · ${a.documents.length} document(s)` : ''}</>
                    )}
                  </button>
                )
              })}
              {view.blockers.slice(0, 4).map((b, i) => (
                // Same rule as the stage errors below: red only when the blocker's stage is in a red state (failed,
                // blocked); a stage waiting for a person (e.g. documents awaiting HR confirmation) is amber.
                <button key={`b${i}`} onClick={() => setSelectedStage(b.stage)}
                  className={`text-left text-xs rounded px-3 py-2 border ${
                    stageStatusView(b.stage, view.stage_statuses[b.stage], view).tone === 'red'
                      ? 'bg-red-50 border-red-200 text-red-700 hover:bg-red-100'
                      : 'bg-amber-50 border-amber-200 text-amber-800 hover:bg-amber-100'}`}>
                  <span className="font-semibold">{b.code}</span> · {STAGES[b.stage]?.title ?? b.stage} — {b.reason.slice(0, 120)}
                </button>
              ))}
            </div>
          )}
          {view && Object.entries(view.stage_errors).filter(([st]) => stageStatusView(st, view.stage_statuses[st]).tone === 'red').map(([st, e]) => (
            <div key={st} className="mb-3 text-xs bg-red-50 border border-red-200 rounded px-3 py-2 text-red-700">
              {STAGES[st]?.uc} {STAGES[st]?.title ?? st}: <span className="font-semibold">{e.code}</span> — {e.detail}
            </div>
          ))}

          <WorkflowDiagram view={view} selected={selectedStage}
            onSelect={st => setSelectedStage(prev => (prev === st ? null : st))} />
        </div>

        {selectedStage && view && (
          <StageSidebar stage={selectedStage} view={view} profile={profile ?? selectedCaseSummary?.profile ?? null}
            execution={detail.executions?.[selectedStage] ?? null} health={health.health}
            onApply={onApply} onClose={() => setSelectedStage(null)} />
        )}
      </div>
    </div>
  )
}
