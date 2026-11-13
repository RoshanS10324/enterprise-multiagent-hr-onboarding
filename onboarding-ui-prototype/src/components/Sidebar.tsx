import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Users,
  GitBranch,
  Activity,
  FileText,
  CheckSquare,
  Clock,
  BarChart3,
  Settings,
} from 'lucide-react'

const navItems = [
  { icon: LayoutDashboard, label: 'Dashboard', path: '/' },
  { icon: Users, label: 'Onboarding Cases', path: '/cases' },
  { icon: GitBranch, label: 'Agent Workflows', path: '/agents/uc01' },
  { icon: Activity, label: 'Workflow Monitor', path: '/monitor' },
  { icon: FileText, label: 'Documents', path: '/documents' },
  { icon: CheckSquare, label: 'Tasks & Activities', path: '/tasks' },
  { icon: Clock, label: 'Approvals', path: '/approvals' },
  { icon: BarChart3, label: 'Reports', path: '/reports' },
  { icon: Settings, label: 'Settings', path: '/settings' },
]

export default function Sidebar() {
  return (
    <div className="w-64 bg-white border-r border-slate-200 flex flex-col shrink-0">
      {/* Logo */}
      <div className="h-16 flex items-center px-6 border-b border-slate-200">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center">
            <GitBranch className="w-4 h-4 text-white" />
          </div>
          <span className="text-sm font-bold text-slate-900 leading-tight">
            Onboarding<br />Control Center
          </span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
        {navItems.map((item) => (
          <NavLink
            key={item.path}
            to={item.path}
            end={item.path === '/'}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-indigo-50 text-indigo-700'
                  : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
              }`
            }
          >
            <item.icon className="w-4 h-4 shrink-0" />
            {item.label}
          </NavLink>
        ))}
      </nav>

      {/* User */}
      <div className="px-4 py-4 border-t border-slate-200">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-indigo-600 rounded-full flex items-center justify-center text-white text-xs font-bold shrink-0">
            SJ
          </div>
          <div className="min-w-0">
            <p className="text-sm font-medium text-slate-900 truncate">Sarah Johnson</p>
            <p className="text-xs text-slate-500 truncate">HR Administrator</p>
          </div>
        </div>
      </div>
    </div>
  )
}
