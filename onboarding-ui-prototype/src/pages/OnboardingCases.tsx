import { useState, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { Search, Filter, ChevronLeft, ChevronRight } from 'lucide-react'
import { mockCases } from '../data/mockData'
import StatusBadge from '../components/StatusBadge'

const PAGE_SIZE = 5

type FilterStatus = 'all' | 'in_progress' | 'complete' | 'blocked' | 'awaiting_approval' | 'failed'
type FilterDept = 'all' | 'Engineering' | 'Product' | 'Design' | 'Finance' | 'Human Resources'

export default function OnboardingCases() {
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<FilterStatus>('all')
  const [deptFilter, setDeptFilter] = useState<FilterDept>('all')
  const [page, setPage] = useState(1)

  const filtered = useMemo(() => {
    return mockCases.filter((c) => {
      const q = search.toLowerCase()
      const matchSearch =
        !q ||
        c.employee_name.toLowerCase().includes(q) ||
        c.case_id.toLowerCase().includes(q) ||
        c.job_title.toLowerCase().includes(q) ||
        c.department.toLowerCase().includes(q)

      const matchStatus =
        statusFilter === 'all' ||
        c.overall_status === statusFilter ||
        (statusFilter === 'awaiting_approval' && c.pending_approvals.length > 0)

      const matchDept = deptFilter === 'all' || c.department === deptFilter

      return matchSearch && matchStatus && matchDept
    })
  }, [search, statusFilter, deptFilter])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageItems = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  function handleSearch(v: string) {
    setSearch(v)
    setPage(1)
  }

  return (
    <div className="space-y-5">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Onboarding Cases</h1>
        <p className="text-sm text-slate-500 mt-1">
          All active employee onboarding cases — <span className="text-amber-600 font-medium">⚠️ Synthetic demonstration data</span>
        </p>
      </div>

      {/* Filters */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm p-4">
        <div className="flex flex-wrap gap-3 items-center">
          {/* Search */}
          <div className="relative flex-1 min-w-48">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
            <input
              type="text"
              placeholder="Search by name, case ID, job title..."
              value={search}
              onChange={(e) => handleSearch(e.target.value)}
              className="w-full pl-9 pr-4 py-2 text-sm bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-300 placeholder:text-slate-400"
            />
          </div>

          {/* Status filter */}
          <div className="flex items-center gap-1.5">
            <Filter className="w-4 h-4 text-slate-400" />
            <select
              value={statusFilter}
              onChange={(e) => { setStatusFilter(e.target.value as FilterStatus); setPage(1) }}
              className="text-sm border border-slate-200 rounded-lg px-3 py-2 bg-slate-50 focus:outline-none focus:ring-2 focus:ring-indigo-300 text-slate-700"
            >
              <option value="all">All Statuses</option>
              <option value="in_progress">In Progress</option>
              <option value="complete">Complete</option>
              <option value="blocked">Blocked</option>
              <option value="awaiting_approval">Awaiting Approval</option>
              <option value="failed">Failed</option>
            </select>
          </div>

          {/* Department filter */}
          <select
            value={deptFilter}
            onChange={(e) => { setDeptFilter(e.target.value as FilterDept); setPage(1) }}
            className="text-sm border border-slate-200 rounded-lg px-3 py-2 bg-slate-50 focus:outline-none focus:ring-2 focus:ring-indigo-300 text-slate-700"
          >
            <option value="all">All Departments</option>
            <option value="Engineering">Engineering</option>
            <option value="Product">Product</option>
            <option value="Design">Design</option>
            <option value="Finance">Finance</option>
            <option value="Human Resources">Human Resources</option>
          </select>

          <span className="text-xs text-slate-400 ml-auto">{filtered.length} case{filtered.length !== 1 ? 's' : ''} found</span>
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-lg border border-slate-200 shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-200">
                {['Case ID', 'Employee', 'Job Title', 'Dept', 'Location', 'Joining Date', 'Current Stage', 'Status', 'Last Updated', 'Actions'].map((h) => (
                  <th key={h} className="text-left text-xs font-semibold text-slate-500 px-4 py-3 whitespace-nowrap">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pageItems.length === 0 ? (
                <tr>
                  <td colSpan={10} className="text-center py-16 text-slate-400 text-sm">
                    No cases match your filters.
                  </td>
                </tr>
              ) : (
                pageItems.map((c) => (
                  <tr key={c.case_id} className="border-b border-slate-50 hover:bg-slate-50 transition-colors">
                    <td className="px-4 py-3 text-xs font-mono text-indigo-600 font-medium">{c.case_id}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <div className="w-7 h-7 bg-indigo-100 rounded-full flex items-center justify-center text-xs font-bold text-indigo-700 shrink-0">
                          {c.employee_name.split(' ').map((n) => n[0]).join('')}
                        </div>
                        <div>
                          <p className="text-sm font-medium text-slate-900 whitespace-nowrap">{c.employee_name}</p>
                          <p className="text-xs text-slate-400">{c.employee_id}</p>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-sm text-slate-700 whitespace-nowrap">{c.job_title}</td>
                    <td className="px-4 py-3 text-sm text-slate-600">{c.department}</td>
                    <td className="px-4 py-3 text-sm text-slate-600">{c.location}</td>
                    <td className="px-4 py-3 text-sm text-slate-600 whitespace-nowrap">{c.joining_date}</td>
                    <td className="px-4 py-3">
                      <span className="text-xs font-mono text-slate-500 whitespace-nowrap">
                        {c.current_stage.replace(/_/g, ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-col gap-1">
                        <StatusBadge status={c.overall_status} size="xs" />
                        {c.pending_approvals.length > 0 && (
                          <span className="text-xs text-amber-600 font-medium">
                            {c.pending_approvals.length} approval{c.pending_approvals.length > 1 ? 's' : ''}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 whitespace-nowrap">
                      {new Date(c.last_updated).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <Link
                          to={`/demo/cases/${c.case_id}`}
                          className="text-xs font-medium text-indigo-600 hover:text-indigo-800 whitespace-nowrap"
                        >
                          View Workflow
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-slate-100">
            <span className="text-xs text-slate-500">
              Page {page} of {totalPages}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="p-1.5 rounded border border-slate-200 text-slate-500 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="p-1.5 rounded border border-slate-200 text-slate-500 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
