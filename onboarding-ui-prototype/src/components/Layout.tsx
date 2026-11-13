import { Outlet } from 'react-router-dom'
import ChatBot from './ChatBot'

export default function Layout() {
  return (
    <div className="flex h-screen bg-slate-50">
      <div className="flex-1 flex flex-col overflow-hidden">
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
      <ChatBot />
    </div>
  )
}
