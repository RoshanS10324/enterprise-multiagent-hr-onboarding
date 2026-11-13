import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Clock, CheckCircle, Mail, Users } from 'lucide-react'
import { mockAgentExecutions, mockCases } from '../data/mockData'
import StatusBadge from '../components/StatusBadge'
import StepTimeline from '../components/StepTimeline'
import ExpandableJSON from '../components/ExpandableJSON'

type SubView = 'orientation' | 'buddy' | 'team' | 'welcome'

export default function UC05Detail() {
  const exec = mockAgentExecutions['uc05']
  const [sub, setSub] = useState<SubView>('orientation')
  const case003 = mockCases[2]
  const case004 = mockCases[3]

  const subItems: { id: SubView; label: string }[] = [
    { id: 'orientation', label: 'Orientation Scheduling' },
    { id: 'buddy', label: 'Buddy Assignment' },
    { id: 'team', label: 'Team Introduction' },
    { id: 'welcome', label: 'Welcome Message' },
  ]

  const navigate = useNavigate()

  return (
    <div className="space-y-5">
      <button onClick={() => navigate(-1)} className="inline-flex items-center gap-1 text-sm text-indigo-600 hover:text-indigo-800 font-medium">
        <ArrowLeft className="w-4 h-4" /> Back to Workflow
      </button>

      {/* Header */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-sm font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">UC05</span>
              <StatusBadge status={exec.status} size="md" />
              <span className="text-xs bg-teal-100 text-teal-700 px-2 py-0.5 rounded font-medium">parallel branch</span>
            </div>
            <h1 className="text-xl font-bold text-slate-900">UC05 — Orientation, Buddy & Welcome</h1>
            <p className="text-sm text-slate-500 mt-1">{exec.description}</p>
          </div>
          <div className="text-xs text-amber-600 bg-amber-50 border border-amber-200 px-3 py-1 rounded-full font-medium shrink-0">
            ⚠️ Synthetic data
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4 pt-4 border-t border-slate-100">
          <div>
            <p className="text-xs text-slate-400">Sub-activities</p>
            <p className="text-2xl font-bold text-indigo-600">4</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Approval Gates</p>
            <p className="text-2xl font-bold text-amber-600">2</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Duration</p>
            <p className="text-lg font-bold text-slate-700">{exec.duration}</p>
          </div>
          <div>
            <p className="text-xs text-slate-400">Case</p>
            <p className="text-lg font-bold text-slate-700">{exec.case_id}</p>
          </div>
        </div>
      </div>

      {/* Sub-activity nav */}
      <div className="flex gap-2 flex-wrap">
        {subItems.map((item) => (
          <button
            key={item.id}
            onClick={() => setSub(item.id)}
            className={`px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
              sub === item.id
                ? 'bg-indigo-600 text-white border-indigo-600 shadow-sm'
                : 'bg-white text-slate-600 border-slate-200 hover:border-indigo-300'
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {/* Orientation Scheduling */}
      {sub === 'orientation' && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* CASE-001 (complete) */}
            <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
              <div className="flex items-center gap-2 mb-3">
                <CheckCircle className="w-4 h-4 text-green-600" />
                <h3 className="text-sm font-semibold text-slate-900">CASE-001 — Sarah Chen</h3>
                <StatusBadge status="complete" size="xs" />
              </div>
              <div className="space-y-2 text-xs">
                <div className="flex justify-between"><span className="text-slate-400">Session ID</span><span className="text-slate-700 font-mono">session-sarah-001</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Scheduled Start</span><span className="text-slate-700">2026-11-15T09:00+04:00</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Session Status</span><span className="text-green-600 font-medium">completed</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Approval ID</span><span className="text-slate-700 font-mono">apr-001-approved</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Approval Status</span><span className="text-green-600 font-medium">approved</span></div>
              </div>
            </div>

            {/* CASE-003 (pending approval) */}
            <div className="bg-white rounded-lg border border-amber-200 shadow-sm p-5">
              <div className="flex items-center gap-2 mb-3">
                <Clock className="w-4 h-4 text-amber-600" />
                <h3 className="text-sm font-semibold text-slate-900">CASE-003 — Priya Sharma</h3>
                <StatusBadge status="awaiting_approval" size="xs" />
              </div>
              <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 mb-3">
                <p className="text-xs font-semibold text-amber-800">HR Action Required</p>
                <p className="text-xs text-amber-700 mt-1">{case003.pending_approvals[0].description}</p>
                <p className="text-xs text-amber-500 mt-1 font-mono">{case003.pending_approvals[0].id}</p>
              </div>
              <div className="space-y-2 text-xs">
                <div className="flex justify-between"><span className="text-slate-400">Session ID</span><span className="text-slate-700 font-mono">{case003.orientation?.session_id}</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Scheduled Start</span><span className="text-slate-700">{case003.orientation?.scheduled_start}</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Session Status</span><span className="text-blue-600 font-medium">{case003.orientation?.session_status}</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Approval Status</span><span className="text-amber-600 font-medium">pending HR approval</span></div>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-slate-900 mb-3">Orientation Step Detail (CASE-001)</h3>
            <StepTimeline steps={[exec.steps[0]]} />
          </div>
        </div>
      )}

      {/* Buddy Assignment */}
      {sub === 'buddy' && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* CASE-001 */}
            <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
              <div className="flex items-center gap-2 mb-3">
                <Users className="w-4 h-4 text-green-600" />
                <h3 className="text-sm font-semibold text-slate-900">CASE-001 — Sarah Chen</h3>
                <StatusBadge status="complete" size="xs" />
              </div>
              <div className="space-y-2 text-xs">
                <div className="flex justify-between"><span className="text-slate-400">Buddy ID</span><span className="text-slate-700 font-mono">EMP-TEST-022</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Buddy Name</span><span className="text-slate-700">Alex Martinez</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Buddy Role</span><span className="text-slate-700">Senior Software Engineer</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Approval Status</span><span className="text-green-600 font-medium">approved</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Status</span><span className="text-green-600 font-medium">confirmed</span></div>
              </div>
            </div>

            {/* CASE-004 */}
            <div className="bg-white rounded-lg border border-amber-200 shadow-sm p-5">
              <div className="flex items-center gap-2 mb-3">
                <Clock className="w-4 h-4 text-amber-600" />
                <h3 className="text-sm font-semibold text-slate-900">CASE-004 — Mohamed Al-Rashid</h3>
                <StatusBadge status="awaiting_approval" size="xs" />
              </div>
              <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 mb-3">
                <p className="text-xs font-semibold text-amber-800">HR Action Required</p>
                <p className="text-xs text-amber-700 mt-1">{case004.pending_approvals[0].description}</p>
                <p className="text-xs text-amber-500 mt-1 font-mono">{case004.pending_approvals[0].id}</p>
              </div>
              <div className="space-y-2 text-xs">
                <div className="flex justify-between"><span className="text-slate-400">Proposed Buddy</span><span className="text-slate-700 font-mono">{case004.buddy_assignment?.proposed_buddy_id}</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Approval ID</span><span className="text-slate-700 font-mono">{case004.buddy_assignment?.approval_id}</span></div>
                <div className="flex justify-between"><span className="text-slate-400">Status</span><span className="text-amber-600 font-medium">proposed</span></div>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-slate-900 mb-3">Buddy Assignment Step Detail (CASE-001)</h3>
            <StepTimeline steps={[exec.steps[1]]} />
          </div>
        </div>
      )}

      {/* Team Introduction */}
      {sub === 'team' && (
        <div className="space-y-4">
          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <div className="flex items-center gap-2 mb-3">
              <Users className="w-4 h-4 text-green-600" />
              <h3 className="text-sm font-semibold text-slate-900">CASE-001 — Sarah Chen</h3>
              <StatusBadge status="complete" size="xs" />
            </div>
            <div className="space-y-2 text-xs">
              <div className="flex justify-between"><span className="text-slate-400">Team</span><span className="text-slate-700">Engineering Platform Squad</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Team Lead</span><span className="text-slate-700">David Park</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Meeting Date</span><span className="text-slate-700">2026-11-15T10:30+04:00</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Attendees</span><span className="text-slate-700">8 team members</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Status</span><span className="text-green-600 font-medium">completed</span></div>
            </div>
          </div>
          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-slate-900 mb-3">Team Introduction Step Detail</h3>
            <StepTimeline steps={[exec.steps[2]]} />
          </div>
        </div>
      )}

      {/* Welcome Message */}
      {sub === 'welcome' && (
        <div className="space-y-4">
          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <div className="flex items-center gap-2 mb-3">
              <Mail className="w-4 h-4 text-green-600" />
              <h3 className="text-sm font-semibold text-slate-900">Joining-Day Welcome — CASE-001 Sarah Chen</h3>
              <StatusBadge status="complete" size="xs" />
            </div>
            <div className="space-y-2 text-xs mb-4">
              <div className="flex justify-between"><span className="text-slate-400">Draft ID</span><span className="text-slate-700 font-mono">draft-welcome-001</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Status</span><span className="text-green-600 font-medium">delivered</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Delivery Channels</span><span className="text-slate-700">email + Slack DM</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Sent By</span><span className="text-slate-700">Sarah Johnson (HR Admin)</span></div>
              <div className="flex justify-between"><span className="text-slate-400">Delivery Time</span><span className="text-slate-700">2026-11-15T08:30+04:00</span></div>
            </div>

            {/* Mock welcome message preview */}
            <div className="bg-indigo-50 border border-indigo-200 rounded-lg p-4">
              <p className="text-xs font-semibold text-indigo-800 mb-2">Welcome Message Preview</p>
              <p className="text-sm text-indigo-900 font-medium">Welcome to Enterprise Digital Platform, Sarah!</p>
              <p className="text-xs text-indigo-700 mt-2 leading-relaxed">
                We are thrilled to have you join our Engineering team today. Your onboarding buddy Alex Martinez is ready to help you settle in.
                Your laptop and all system accesses are provisioned and ready. The team is looking forward to meeting you at 10:30 AM.
                Feel free to reach out to HR at any time. We wish you a wonderful first day!
              </p>
              <p className="text-xs text-indigo-500 mt-2">— Sarah Johnson, HR Administrator</p>
            </div>
          </div>

          <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-slate-900 mb-3">Welcome Message Step Detail</h3>
            <StepTimeline steps={[exec.steps[3]]} />
          </div>

          <ExpandableJSON data={exec.output_summary} label="View full UC05 output JSON" />
        </div>
      )}

      {/* Full history */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-5">
        <h2 className="text-base font-semibold text-slate-900 mb-4">Full Execution History (CASE-001)</h2>
        <div className="space-y-2">
          {exec.history.map((h, i) => (
            <div key={i} className="flex items-start gap-4 py-2 border-b border-slate-50 last:border-0">
              <span className="text-xs text-slate-400 whitespace-nowrap font-mono">
                {new Date(h.timestamp).toLocaleDateString()}
              </span>
              <StatusBadge status={h.status} size="xs" />
              <span className="text-sm text-slate-700">{h.note}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
