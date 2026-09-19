// 路由：/login 公开；其余挂 RequireAuth（无 token → 跳登录）。
import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import AuditPage from './pages/AuditPage'
import AccountPage from './pages/AccountPage'
import AdminPage from './pages/AdminPage'
import AppearancePage from './pages/AppearancePage'
import EnvironmentPage from './pages/EnvironmentPage'
import LoginPage from './pages/LoginPage'
import LorePage from './pages/LorePage'
import NewProjectPage from './pages/NewProjectPage'
import ProjectsPage from './pages/ProjectsPage'
import SettingsPage from './pages/SettingsPage'
import WorkspacePage from './pages/WorkspacePage'

function RequireAuth() {
  const { session, status, validationError, revalidate, logout } = useAuth()
  if (status === 'checking') return <div className="empty">正在验证登录状态…</div>
  if (status === 'unavailable') {
    return (
      <div className="empty">
        <p>{validationError}</p>
        <button type="button" className="btn btn-primary" onClick={revalidate}>重试</button>{' '}
        <button type="button" className="btn btn-quiet" onClick={() => void logout()}>退出登录</button>
      </div>
    )
  }
  if (!session) return <Navigate to="/login" replace />
  // token 或账号改变即卸载整个受保护子树，旧 fetch 状态与 SSE 随组件清理一并丢弃。
  return <Outlet key={`${session.userId}:${session.token}`} />
}

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    element: <RequireAuth />,
    children: [
      { path: '/', element: <Navigate to="/projects" replace /> },
      { path: '/projects', element: <ProjectsPage /> },
      { path: '/account', element: <AccountPage /> },
      { path: '/admin', element: <AdminPage /> },
      { path: '/environment', element: <EnvironmentPage /> },
      { path: '/theme', element: <AppearancePage /> },
      { path: '/appearance', element: <Navigate to="/theme" replace /> },
      { path: '/projects/new', element: <NewProjectPage /> },
      { path: '/projects/:projectId', element: <WorkspacePage /> },
      { path: '/projects/:projectId/settings', element: <SettingsPage /> },
      { path: '/projects/:projectId/audit', element: <AuditPage /> },
      { path: '/projects/:projectId/lore', element: <LorePage /> },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
