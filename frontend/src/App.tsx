import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { LoginPage } from '@/pages/LoginPage';
import { ProjectPage } from '@/pages/ProjectPage';
import { ProjectPickerPage } from '@/pages/ProjectPickerPage';
import { RequireAuth } from '@/pages/RequireAuth';
import { SettingsPage } from '@/pages/SettingsPage';

/** Router-less route table (tests wrap it in a MemoryRouter). */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route path="/projects" element={<ProjectPickerPage />} />
        <Route path="/p/:projectId" element={<ProjectPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}
