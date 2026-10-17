import { useEffect } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '@/stores/auth';

export function RequireAuth() {
  const status = useAuth((s) => s.status);
  const check = useAuth((s) => s.check);
  const location = useLocation();
  useEffect(() => {
    if (status === 'unknown') void check();
  }, [status, check]);
  if (status === 'unknown') return <p className="p-6 text-muted">Loading…</p>;
  if (status === 'anonymous') return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}
