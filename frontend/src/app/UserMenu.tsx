import { useEffect, useRef, useState } from 'react';
import { toasts } from '../components/toastStore';
import { initials } from '../lib/format';
import { useSession } from '../platform/context';

export function UserMenu() {
  const { viewer, signOut, refreshGroups, demo } = useSession();
  const [open, setOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  async function handleRefresh(refresh: () => Promise<void>) {
    setRefreshing(true);
    try {
      await refresh();
      toasts.push({ tone: 'success', title: 'Permissions refreshed', text: 'Your groups were re-read from a new ID token.' });
    } catch (error) {
      toasts.push({ tone: 'error', title: 'Could not refresh the session', text: error instanceof Error ? error.message : String(error) });
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <div className="user-menu" ref={rootRef}>
      <button
        type="button"
        className="user-menu__button"
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={`Account menu for ${viewer.name}`}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="avatar" aria-hidden="true">
          {initials(viewer.name || viewer.email)}
        </span>
      </button>
      {open && (
        <div className="user-menu__panel">
          <div className="user-menu__identity">
            <span className="avatar" aria-hidden="true">
              {initials(viewer.name || viewer.email)}
            </span>
            <div>
              <div className="user-menu__name">{viewer.name}</div>
              <div className="user-menu__email">{viewer.email}</div>
            </div>
          </div>
          <div className="stack-sm">
            <span className="subtle">Cognito groups (from the ID token)</span>
            <div className="row">
              {viewer.groups.length === 0 && <span className="badge badge--plain">no groups</span>}
              {viewer.groups.map((group) => (
                <span key={group} className={`badge badge--plain ${group === 'employees' ? '' : 'badge--violet'}`}>
                  {group}
                </span>
              ))}
            </div>
          </div>
          <div className="user-menu__actions">
            {refreshGroups && (
              <button type="button" className="btn btn-sm" disabled={refreshing} onClick={() => void handleRefresh(refreshGroups)}>
                Refresh permissions
              </button>
            )}
            {signOut && (
              <button type="button" className="btn btn-sm" onClick={signOut}>
                Sign out
              </button>
            )}
            {demo && <span className="subtle">Demo mode: switch roles in the purple bar above.</span>}
          </div>
        </div>
      )}
    </div>
  );
}
