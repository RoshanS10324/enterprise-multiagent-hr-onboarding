import { BrowserRouter, Navigate, Route, Routes, useParams } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import OnboardingCases from './pages/OnboardingCases'
import CaseWorkflow from './pages/CaseWorkflow'
import UC01Detail from './pages/UC01Detail'
import UC02Detail from './pages/UC02Detail'
import UC03Detail from './pages/UC03Detail'
import UC04Detail from './pages/UC04Detail'
import UC05Detail from './pages/UC05Detail'
import WorkflowMonitor from './pages/WorkflowMonitor'

/** A link to a live case opens it on the dashboard. */
function LiveCaseRedirect() {
  const { caseId } = useParams()
  return <Navigate to={caseId ? `/?case=${encodeURIComponent(caseId)}` : '/'} replace />
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          {/* Live: everything here is read from and acted on through the orchestrator API. */}
          <Route index element={<Dashboard />} />
          <Route path="cases/:caseId" element={<LiveCaseRedirect />} />
          {/* Demo mode: the original prototype pages, on static synthetic data (each page says so). */}
          <Route path="demo/cases" element={<OnboardingCases />} />
          <Route path="demo/cases/:caseId" element={<CaseWorkflow />} />
          <Route path="demo/agents/uc01" element={<UC01Detail />} />
          <Route path="demo/agents/uc02" element={<UC02Detail />} />
          <Route path="demo/agents/uc03" element={<UC03Detail />} />
          <Route path="demo/agents/uc04" element={<UC04Detail />} />
          <Route path="demo/agents/uc05" element={<UC05Detail />} />
          <Route path="demo/monitor" element={<WorkflowMonitor />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
