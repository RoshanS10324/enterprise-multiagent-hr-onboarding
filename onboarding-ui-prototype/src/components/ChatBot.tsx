import { useState, useRef, useEffect } from 'react'
import { MessageCircle, X, Send } from 'lucide-react'
import { useBackendHealth } from '../api/useOnboardingData'

interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  timestamp: string;
}

const suggestedQuestions = [
  'What is the current status of all cases?',
  'Which cases are running right now?',
  'Are there any delayed cases?',
  'What documents are still pending?',
  'What tasks are outstanding?',
  'Is there any failed equipment provisioning?',
  'What approvals are pending?',
  'Explain the AI onboarding workflow',
]

function getResponse(input: string): string {
  const q = input.toLowerCase()
  if (q.includes('status') || q.includes('overview') || q.includes('summary')) {
    return 'Based on synthetic demonstration data:\n\n• 2 cases In Progress (CASE-003 Priya Sharma, CASE-004 Mohamed Al-Rashid)\n• 1 case Completed (CASE-001 Sarah Chen)\n• 1 case Blocked (CASE-002 James Okafor — missing documents)\n• 1 case Failed (CASE-005 Emma Wilson — IT system outage)\n\nTotal: 5 active onboarding cases.'
  }
  if (q.includes('running') || q.includes('progress') || q.includes('active')) {
    return 'Currently running:\n\n• CASE-003 (Priya Sharma) — at orientation_scheduling, awaiting HR approval for induction session booking on 2026-12-08T09:00.\n• CASE-004 (Mohamed Al-Rashid) — at buddy_assignment, awaiting HR approval for proposed buddy EMP-TEST-050.'
  }
  if (q.includes('delay') || q.includes('block') || q.includes('stuck')) {
    return 'CASE-002 (James Okafor) is delayed because:\n\n• Passport copy was not submitted\n• Emirates ID address page is unreadable\n\nError code: MISSING_REQUIRED_DOCUMENT (retryable: true). The employee needs to resubmit via the document portal before validation can proceed.'
  }
  if (q.includes('document') || q.includes('doc')) {
    return 'Pending document issues:\n\n• CASE-002 (James Okafor) — passport and educational certificate are outstanding; Emirates ID address page is invalid.\n\nAll other cases (CASE-001, 003, 004, 005) have submitted complete document sets.'
  }
  if (q.includes('task')) {
    return 'Task summary:\n\n• CASE-001 (Sarah Chen) — 12/12 tasks complete\n• CASE-003 (Priya Sharma) — 7/11 tasks complete\n• CASE-004 (Mohamed Al-Rashid) — 8/10 tasks complete\n• CASE-005 (Emma Wilson) — 6/9 tasks; 2 failed due to IT outage\n• CASE-002 (James Okafor) — 0 tasks started (blocked on documents)'
  }
  if (q.includes('equipment') || q.includes('laptop') || q.includes('hardware')) {
    return 'CASE-005 (Emma Wilson) has a critical equipment provisioning failure:\n\nError: PROVISIONING_SYSTEM_UNAVAILABLE — IT service desk system is offline. MacBook Pro 14" and Access Card could not be provisioned.\n\nAction required: IT team must resolve system outage. Retryable: false (manual IT intervention needed).'
  }
  if (q.includes('approval') || q.includes('pending approval') || q.includes('approve')) {
    return 'Pending approvals requiring HR action:\n\n1. CASE-003 (Priya Sharma) — Approval ID: apr-073e75c76217\n   Gate: scheduling_approval\n   Description: UC05 induction booking tentative hold — approve or reject.\n\n2. CASE-004 (Mohamed Al-Rashid) — Approval ID: apr-380cec887672\n   Gate: buddy_assignment\n   Description: EMP-TEST-050 proposed as onboarding buddy.'
  }
  if (q.includes('explain') || q.includes('workflow') || q.includes('how') || q.includes('agent')) {
    return 'The AI Onboarding workflow has 5 agents:\n\n• UC01 — Creates employee record from offer letter\n• UC02 — (A) Collects required documents, (B) Plans onboarding tasks\n• UC03 — Validates all submitted documents\n• UC04 — Provisions equipment and system access (runs in parallel)\n• UC05 — Schedules orientation, assigns buddy, sends welcome (runs in parallel)\n\nAfter the parallel branches merge, Day-1 readiness is evaluated and HR signs off. On joining day, UC05 delivers a personalised welcome message.'
  }
  return 'This is a demonstration prototype with synthetic data. The AI Onboarding Control Center monitors 5 agents (UC01–UC05) across the full employee onboarding lifecycle from offer acceptance to Day-1 welcome.\n\nTry asking about: status overview, pending approvals, documents, equipment issues, or how the workflow works.'
}

export default function ChatBot() {
  // Canned answers about fixed synthetic cases: shown only when the backend runs in demo mode, never next to live data.
  const health = useBackendHealth()
  const [isOpen, setIsOpen] = useState(false)
  const [messages, setMessages] = useState<Message[]>([
    {
      id: 'welcome',
      role: 'assistant',
      text: 'Hi! I\'m the Onboarding AI Assistant (Demo Mode). I can answer questions about the 5 synthetic onboarding cases. What would you like to know?',
      timestamp: new Date().toISOString(),
    },
  ])
  const [inputText, setInputText] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  function handleSend(text?: string) {
    const q = (text ?? inputText).trim()
    if (!q) return
    const userMsg: Message = {
      id: `msg-${Date.now()}`,
      role: 'user',
      text: q,
      timestamp: new Date().toISOString(),
    }
    const botMsg: Message = {
      id: `msg-${Date.now() + 1}`,
      role: 'assistant',
      text: getResponse(q),
      timestamp: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, userMsg, botMsg])
    setInputText('')
  }

  if (health.health?.mode !== 'demo') return null
  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col items-end gap-3">
      {/* Chat panel */}
      {isOpen && (
        <div className="w-96 h-[520px] bg-white rounded-xl shadow-2xl border border-slate-200 flex flex-col overflow-hidden">
          {/* Header */}
          <div className="bg-indigo-700 px-4 py-3 flex items-center justify-between shrink-0">
            <div className="flex items-center gap-2">
              <MessageCircle className="w-4 h-4 text-white" />
              <span className="text-sm font-semibold text-white">Onboarding AI Assistant</span>
              <span className="text-xs bg-amber-400 text-amber-900 px-2 py-0.5 rounded-full font-medium">Demo Mode</span>
            </div>
            <button onClick={() => setIsOpen(false)} className="text-indigo-200 hover:text-white transition-colors">
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`max-w-[85%] rounded-xl px-3 py-2 text-sm whitespace-pre-line ${
                    msg.role === 'user'
                      ? 'bg-indigo-600 text-white'
                      : 'bg-slate-100 text-slate-800'
                  }`}
                >
                  {msg.text}
                </div>
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>

          {/* Suggested questions */}
          {messages.length <= 2 && (
            <div className="px-4 pb-2 flex flex-wrap gap-1.5">
              {suggestedQuestions.slice(0, 4).map((q) => (
                <button
                  key={q}
                  onClick={() => handleSend(q)}
                  className="text-xs bg-indigo-50 text-indigo-700 hover:bg-indigo-100 px-2 py-1 rounded-full transition-colors border border-indigo-200"
                >
                  {q}
                </button>
              ))}
            </div>
          )}

          {/* Input */}
          <div className="px-4 pb-4 pt-2 border-t border-slate-100 shrink-0">
            <div className="flex gap-2">
              <input
                type="text"
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSend()}
                placeholder="Ask about onboarding status..."
                className="flex-1 text-sm px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-300 placeholder:text-slate-400"
              />
              <button
                onClick={() => handleSend()}
                className="p-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors shrink-0"
              >
                <Send className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Toggle button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-14 h-14 bg-indigo-600 text-white rounded-full shadow-lg flex items-center justify-center cursor-pointer hover:bg-indigo-700 transition-colors"
        aria-label="Toggle AI assistant"
      >
        {isOpen ? <X className="w-6 h-6" /> : <MessageCircle className="w-6 h-6" />}
      </button>
    </div>
  )
}
