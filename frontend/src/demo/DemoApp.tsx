import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { requestLog } from '../api/inspector';
import { AppRoot } from '../app/AppRoot';
import { toasts } from '../components/toastStore';
import { rolesFor } from '../auth/groups';
import type { DemoRole, Platform, Session } from '../platform/platform';
import type { DemoBackend } from './backend';
import { DEMO_ROLE_IDS, DEMO_USERS, type DemoRoleId, type DemoUser } from './users';

/** Who the demo backend's "JWT authorizer" believes is calling: switched by the role buttons. */
export interface DemoIdentity {
  get: () => DemoUser;
  set: (roleId: DemoRoleId) => void;
}

const ROLES: readonly DemoRole[] = DEMO_ROLE_IDS.map((id) => ({
  id,
  label: DEMO_USERS[id].label,
  description: `${DEMO_USERS[id].name} (${DEMO_USERS[id].label.toLowerCase()}): ${DEMO_USERS[id].description}`,
}));

function sessionFor(user: DemoUser): Omit<Session, 'demo'> {
  return {
    viewer: { userId: user.userId, email: user.email, name: user.name, groups: user.groups },
    roles: rolesFor(user.groups),
  };
}

export function DemoApp({
  backend,
  identity,
  platform,
}: {
  backend: DemoBackend;
  identity: DemoIdentity;
  platform: Omit<Platform, 'live'>;
}) {
  const navigate = useNavigate();
  const [roleId, setRoleId] = useState<DemoRoleId>(() => identity.get().roleId);
  const [generation, setGeneration] = useState(0);
  const user = DEMO_USERS[roleId];

  const session = useMemo((): Session => {
    const switchRole = (id: string) => {
      const next = DEMO_ROLE_IDS.find((candidate) => candidate === id);
      if (!next || next === roleId) return;
      identity.set(next);
      setRoleId(next);
    };
    const reset = () => {
      backend.reset();
      requestLog.clear();
      setGeneration((value) => value + 1);
      void navigate('/');
      toasts.push({ tone: 'success', title: 'Demo data reset', text: 'The sample expenses are back.' });
    };
    return { ...sessionFor(user), demo: { roles: ROLES, currentRoleId: roleId, switchRole, reset } };
  }, [backend, identity, navigate, roleId, user]);
  const rolePlatform = useMemo((): Platform => ({ ...platform, live: backend.liveSource(user) }), [backend, platform, user]);

  // A new key per role/reset remounts the tree: the "WebSocket" reconnects with the new user's audiences
  // and every page refetches as the new user, exactly like signing out and in again.
  return (
    <AppRoot key={`${roleId}:${generation}`} platform={rolePlatform} session={session} />
  );
}
