/**
 * The orchestrator's graph (HR-Onboarding-Common orchestration/orchestrator_agent.py), coloured by the
 * persisted stage statuses. It only displays state; every transition is the backend's.
 *
 *   UC01 intake -> UC02 plan_tasks -> UC03 document_collection -> document_submission (await documents)
 *   -> UC03 validate_documents <-> UC02 replan_tasks (only on UC03 feedback)
 *   -> mandatory document gate -> UC04 equipment_access || UC05 orientation -> buddy -> team intro
 *   -> merge -> day_one_readiness (HR signoff) -> await joining day -> UC05 joining_day_welcome -> complete
 */
import type { CaseView } from '../api/onboardingApi'
import { STAGES, TONE_DOT, TONE_NODE, awaitingFormApproval, stageStatusView, workflowStatusView } from '../lib/stages'

interface Props {
  view: CaseView | null
  selected: string | null
  onSelect: (stage: string) => void
}

function Arrow({ label }: { label?: string }) {
  return (
    <div className="flex justify-center my-1">
      <div className="flex flex-col items-center">
        <div className="w-0.5 h-4 bg-slate-300" />
        <div className="w-0 h-0" style={{ borderLeft: '5px solid transparent', borderRight: '5px solid transparent',
          borderTop: '6px solid #94a3b8' }} />
        {label && <span className="text-[10px] text-slate-400 mt-0.5">{label}</span>}
      </div>
    </div>
  )
}

export default function WorkflowDiagram({ view, selected, onSelect }: Props) {
  const statuses = view?.stage_statuses ?? {}
  const current = view?.current_stage

  function Node({ stage, compact = false }: { stage: string; compact?: boolean }) {
    const meta = STAGES[stage]
    const s = stageStatusView(stage, statuses[stage], view)
    const isSelected = selected === stage
    const held = awaitingFormApproval(view)
    const isCurrent = stage === 'form_approval' ? held : current === stage && s.tone !== 'green' && !held
    return (
      <button
        type="button"
        onClick={() => onSelect(stage)}
        aria-pressed={isSelected}
        title={`${meta.uc} — ${meta.title}: ${s.label}`}
        className={`w-full rounded-lg border-2 ${compact ? 'px-2 py-2' : 'p-3'} text-center cursor-pointer
          hover:shadow-md transition-all select-none ${TONE_NODE[s.tone]}
          ${isSelected ? 'ring-2 ring-indigo-400 ring-offset-1' : ''}`}
      >
        <div className="flex items-center justify-center gap-2 flex-wrap">
          <span className={`w-2 h-2 rounded-full shrink-0 ${TONE_DOT[s.tone]} ${s.tone === 'blue' ? 'animate-pulse' : ''}`} />
          <span className="font-bold text-indigo-700 text-xs">{meta.uc}</span>
          {isCurrent && <span className="text-[10px] bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded font-medium">current</span>}
        </div>
        <p className={`font-semibold text-slate-800 mt-1 leading-tight ${compact ? 'text-xs' : 'text-sm'}`}>{meta.title}</p>
        {view && <p className="text-[11px] text-slate-500 mt-0.5">{s.label}</p>}
      </button>
    )
  }

  function WaitNode({ stage, label }: { stage: string; label: string }) {
    const s = stageStatusView(stage, statuses[stage], view)
    const active = s.tone === 'yellow' || s.tone === 'blue'
    const done = s.tone === 'green'
    return (
      <button type="button" onClick={() => onSelect(stage)}
        className={`rounded-lg border-2 border-dashed px-6 py-2 text-center ${
          done ? 'border-green-300 bg-green-50 text-green-700'
            : active ? 'border-amber-400 bg-amber-50 text-amber-700' : 'border-amber-200 bg-amber-50 text-amber-600'}
          ${selected === stage ? 'ring-2 ring-indigo-400 ring-offset-1' : ''}`}>
        <span className="text-xs font-semibold">⏳ {label}{view ? ` — ${s.label}` : ''}</span>
      </button>
    )
  }

  const replanActive = ['in_progress', 'complete', 'failed', 'blocked', 'retry_pending'].includes(statuses.replan_tasks ?? '')
  const planRevision = (view?.onboarding_plan as { plan_version?: number } | null | undefined)?.plan_version
  const completeAll = view?.workflow_status === 'completed'

  return (
    <div className="overflow-x-auto">
      <div className="min-w-[640px] max-w-3xl mx-auto">
        <div className="flex flex-col items-center gap-0">
          <div className="text-[10px] text-slate-400 mb-1">📝 Internal onboarding form submitted</div>
          <div className="w-72"><Node stage="form_approval" /></div>
          <div className="flex justify-center gap-6 text-[10px] mt-1">
            <span className={view?.form_approval?.status === 'approved' ? 'text-green-600 font-semibold' : 'text-slate-400'}>✓ approved → UC01</span>
            <span className={view?.form_approval?.status === 'rejected' ? 'text-red-600 font-semibold' : 'text-slate-400'}>✗ rejected → HR review / correction</span>
          </div>
          <Arrow />
          <div className="w-72"><Node stage="intake" /></div>
          <Arrow />
          <div className="w-72"><Node stage="plan_tasks" /></div>
          <Arrow label={planRevision ? `plan revision v${planRevision}` : undefined} />
          <div className="w-72"><Node stage="document_collection" /></div>
          <Arrow />
          <WaitNode stage="document_submission" label="Await Documents" />
          <Arrow />

          {/* Validation with the conditional UC03 -> UC02 replanning loop beside it */}
          <div className="relative w-72">
            <Node stage="validate_documents" />
            <div className="absolute top-1/2 -translate-y-1/2 left-full ml-6 w-48 hidden md:block">
              <div className="flex items-center gap-1 mb-1">
                <div className={`flex-1 h-px border-t-2 border-dashed ${replanActive ? 'border-orange-400' : 'border-slate-300'}`} />
                <span className={`text-[10px] font-semibold ${replanActive ? 'text-orange-600' : 'text-slate-400'}`}>↺ plan change</span>
              </div>
              <Node stage="replan_tasks" compact />
            </div>
          </div>

          <div className="w-full mt-2">
            <div className="flex items-center gap-3 mb-2">
              <div className="flex-1 h-px bg-slate-200" />
              <span className="text-xs font-semibold text-teal-600 whitespace-nowrap">⛨ Mandatory Document Gate → ⇶ Parallel Execution</span>
              <div className="flex-1 h-px bg-slate-200" />
            </div>
            <div className="flex gap-3 items-start">
              <div className="flex-1 flex flex-col items-center gap-2">
                <div className="text-xs font-semibold text-teal-600 text-center mb-1">
                  Branch: Equipment & Access {view?.branch_statuses?.uc04_equipment_access && `(${workflowStatusView(view.branch_statuses.uc04_equipment_access.status).label})`}
                </div>
                <div className="w-full"><Node stage="equipment_access" compact /></div>
              </div>
              <div className="flex-1 flex flex-col items-center gap-2">
                <div className="text-xs font-semibold text-teal-600 text-center mb-1">
                  Branch: Orientation {view?.branch_statuses?.uc05_orientation && `(${workflowStatusView(view.branch_statuses.uc05_orientation.status).label})`}
                </div>
                <div className="w-full"><Node stage="orientation_scheduling" compact /></div>
                <div className="w-0.5 h-3 bg-slate-300" />
                <div className="w-full"><Node stage="buddy_assignment" compact /></div>
                <div className="w-0.5 h-3 bg-slate-300" />
                <div className="w-full"><Node stage="team_introduction" compact /></div>
              </div>
            </div>
            <div className="flex items-center gap-3 mt-2">
              <div className="flex-1 h-px bg-slate-200" />
              <span className="text-xs font-semibold text-slate-500 whitespace-nowrap">⊕ Merge (both branches must reach an acceptable terminal state)</span>
              <div className="flex-1 h-px bg-slate-200" />
            </div>
          </div>

          <Arrow />
          <div className="w-72"><Node stage="day_one_readiness" /></div>
          <Arrow />
          <WaitNode stage="joining_day_welcome" label="Await Joining Day" />
          <Arrow />
          <div className="w-72"><Node stage="joining_day_welcome" /></div>
          <Arrow />
          <div className={`rounded-full px-8 py-2.5 font-bold text-sm border-2 ${completeAll
            ? 'bg-green-100 border-green-500 text-green-700' : 'bg-slate-50 border-slate-200 text-slate-400'}`}>
            ✓ Onboarding Complete
          </div>
        </div>
      </div>
    </div>
  )
}
