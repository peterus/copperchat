import { type FormEvent, useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { PoweredBy } from '@/components/PoweredBy';
import { useAuth } from '@/stores/auth';

export function LoginPage() {
  const login = useAuth((s) => s.login);
  const error = useAuth((s) => s.error);
  const status = useAuth((s) => s.status);
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const ok = await login(username, password);
    const from = (location.state as { from?: string } | null)?.from;
    if (ok) {
      // busy stays set: the redirect below must not race this navigation.
      navigate(from && from !== '/login' ? from : '/projects', { replace: true });
      return;
    }
    setBusy(false);
  }

  // Already signed in: logging in again would keep the old session's socket and state.
  if (status === 'authenticated' && !busy) return <Navigate to="/" replace />;

  return (
    <main className="flex min-h-full items-center justify-center p-6">
      <form onSubmit={onSubmit} className="w-full max-w-sm space-y-4 rounded-xl border border-border bg-surface p-6">
        <h1 className="text-xl font-semibold">CopperChat</h1>
        <label className="block text-sm">
          Username
          <input className="mt-1 w-full rounded border border-border bg-bg px-3 py-2" autoComplete="username"
            value={username} onChange={(e) => setUsername(e.target.value)} required />
        </label>
        <label className="block text-sm">
          Password
          <input type="password" className="mt-1 w-full rounded border border-border bg-bg px-3 py-2"
            autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <button type="submit" disabled={busy} className="w-full rounded bg-accent px-3 py-2 font-medium text-accent-fg disabled:opacity-60">
          Sign in
        </button>
        <PoweredBy />
      </form>
    </main>
  );
}
