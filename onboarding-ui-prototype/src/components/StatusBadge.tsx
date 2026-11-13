import type { StageStatus } from '../types'

const config: Record<string, { bg: string; text: string; label: string }> = {
  complete: { bg: 'bg-green-100', text: 'text-green-700', label: 'Complete' },
  in_progress: { bg: 'bg-blue-100', text: 'text-blue-700', label: 'In Progress' },
  running: { bg: 'bg-blue-100', text: 'text-blue-700', label: 'Running' },
  awaiting_approval: { bg: 'bg-amber-100', text: 'text-amber-700', label: 'Awaiting Approval' },
  waiting: { bg: 'bg-amber-100', text: 'text-amber-700', label: 'Waiting' },
  failed: { bg: 'bg-red-100', text: 'text-red-700', label: 'Failed' },
  uncertain: { bg: 'bg-orange-100', text: 'text-orange-700', label: 'Uncertain' },
  blocked: { bg: 'bg-red-50', text: 'text-red-600', label: 'Blocked' },
  not_started: { bg: 'bg-slate-100', text: 'text-slate-500', label: 'Not Started' },
  pending: { bg: 'bg-slate-100', text: 'text-slate-500', label: 'Pending' },
  not_requested: { bg: 'bg-slate-100', text: 'text-slate-400', label: 'Not Requested' },
  at_risk: { bg: 'bg-orange-100', text: 'text-orange-700', label: 'At Risk' },
}

interface StatusBadgeProps {
  status: string;
  size?: 'xs' | 'sm' | 'md';
}

export default function StatusBadge({ status, size = 'sm' }: StatusBadgeProps) {
  const c = config[status] ?? config.not_started
  const sizeClass =
    size === 'xs'
      ? 'text-xs px-1.5 py-0.5'
      : size === 'md'
      ? 'text-sm px-3 py-1'
      : 'text-xs px-2 py-1'
  return (
    <span className={`inline-flex items-center rounded-full font-medium ${c.bg} ${c.text} ${sizeClass}`}>
      {c.label}
    </span>
  )
}

export function statusDotColor(status: string): string {
  const dotMap: Record<string, string> = {
    complete: 'bg-green-500',
    in_progress: 'bg-blue-500',
    running: 'bg-blue-500',
    awaiting_approval: 'bg-amber-500',
    waiting: 'bg-amber-500',
    failed: 'bg-red-500',
    uncertain: 'bg-orange-500',
    blocked: 'bg-red-400',
    not_started: 'bg-slate-300',
    pending: 'bg-slate-300',
    not_requested: 'bg-slate-200',
    at_risk: 'bg-orange-500',
  }
  return dotMap[status] ?? 'bg-slate-300'
}

export function statusBorderColor(status: string): string {
  const borderMap: Record<string, string> = {
    complete: 'border-green-400 bg-green-50',
    in_progress: 'border-blue-400 bg-blue-50',
    running: 'border-blue-400 bg-blue-50',
    awaiting_approval: 'border-amber-400 bg-amber-50',
    waiting: 'border-amber-400 bg-amber-50',
    failed: 'border-red-400 bg-red-50',
    uncertain: 'border-orange-400 bg-orange-50',
    blocked: 'border-red-300 bg-red-50',
    not_started: 'border-slate-200 bg-slate-50',
    pending: 'border-slate-200 bg-slate-50',
    not_requested: 'border-slate-200 bg-white',
    at_risk: 'border-orange-400 bg-orange-50',
  }
  return borderMap[status] ?? 'border-slate-200 bg-slate-50'
}
