import { Link, useNavigate } from 'react-router-dom';
import { PoweredBy } from '@/components/PoweredBy';
import { useAuth } from '@/stores/auth';
import { type ThemePref, useUi } from '@/stores/ui';
import { NotificationsSection } from './settings/NotificationsSection';
import { ViewerSection } from './settings/ViewerSection';

const THEMES: Array<{ value: ThemePref; label: string }> = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'system', label: 'System' },
];

export function SettingsPage() {
  const theme = useUi((s) => s.theme);
  const setTheme = useUi((s) => s.setTheme);
  const username = useAuth((s) => s.username);
  const logout = useAuth((s) => s.logout);
  const navigate = useNavigate();

  return (
    <main className="mx-auto flex min-h-full max-w-xl flex-col gap-6 p-4">
      <header className="flex items-center gap-3">
        <Link to="/projects" className="text-sm text-accent">
          ← Projects
        </Link>
        <h1 className="text-xl font-semibold">Settings</h1>
      </header>
      <section className="space-y-2">
        <h2 className="font-medium">Theme</h2>
        <div role="radiogroup" aria-label="Theme" className="flex gap-4">
          {THEMES.map((t) => (
            <label key={t.value} className="flex items-center gap-1.5 text-sm">
              <input
                type="radio"
                name="theme"
                value={t.value}
                checked={theme === t.value}
                onChange={() => setTheme(t.value)}
              />
              {t.label}
            </label>
          ))}
        </div>
      </section>
      <ViewerSection />
      <NotificationsSection />
      <section className="space-y-2">
        <h2 className="font-medium">Account</h2>
        <p className="text-sm">Signed in as {username}</p>
        <button
          type="button"
          className="rounded border border-border px-3 py-1.5 text-sm"
          onClick={async () => {
            await logout();
            navigate('/login', { replace: true });
          }}
        >
          Sign out
        </button>
      </section>
      <footer className="mt-auto">
        <PoweredBy />
      </footer>
    </main>
  );
}
