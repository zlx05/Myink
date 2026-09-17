// 路由：/login 公开；其余挂 RequireAuth（无 token → 跳登录）。
import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import AuditPage from './pages/AuditPage'
import AppearancePage from './pages/AppearancePage'
import EnvironmentPage from './pages/EnvironmentPage'
import LoginPage from './pages/LoginPage'
import LorePage from './pages/LorePage'
import NewProjectPage from './pages/NewProjectPage'
import ProjectsPage from './pages/ProjectsPage'
import SettingsPage from './pages/SettingsPage'
import WorkspacePage from './pages/WorkspacePage'

function RequireAuth() {
  const { session } = useAuth()
  if (!session) return <Navigate to="/login" replace />
  return <Outlet />
}

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    element: <RequireAuth />,
    children: [
      { path: '/', element: <Navigate to="/projects" replace /> },
      { path: '/projects', element: <ProjectsPage /> },
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
