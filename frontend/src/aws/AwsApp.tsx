import { Authenticator, createTheme, defaultDarkModeOverride, ThemeProvider } from '@aws-amplify/ui-react';
import { useEffect, useMemo, useState } from 'react';
import { AppRoot } from '../app/AppRoot';
import { rolesFor } from '../auth/groups';
import { LoadingBlock, Logo, Notice } from '../components/ui';
import type { Platform, Session, Viewer } from '../platform/platform';
import { loadViewer } from './session';

/** Amplify UI theme in ExpenseFlow's colors; `colorMode="system"` follows the OS light/dark setting. */
const theme = createTheme({
  name: 'expenseflow',
  tokens: {
    colors: {
      // Amplify UI v6 derives buttons, links and focus rings from the `primary` scale (default: teal).
      primary: {
        10: { value: '#e8eefc' },
        20: { value: '#c9d7f8' },
        40: { value: '#7ea4ff' },
        60: { value: '#3d6be0' },
        80: { value: '#2f5fd0' },
        90: { value: '#244bb0' },
        100: { value: '#1a3780' },
      },
    },
    radii: { small: { value: '6px' }, medium: { value: '10px' }, large: { value: '14px' } },
  },
  overrides: [defaultDarkModeOverride],
});

function AuthHeader() {
  return (
    <div className="auth-brand">
      <Logo />
      <span className="auth-brand__name">ExpenseFlow</span>
      <span className="muted">Team expenses, approved on AWS</span>
    </div>
  );
}

function AuthFooter() {
  return <p className="auth-footer">Accounts live in an Amazon Cognito user pool. New users join the employees group.</p>;
}

const components = { Header: AuthHeader, Footer: AuthFooter };
const formFields = { signUp: { name: { label: 'Full name', placeholder: 'e.g. Aiko Tanaka' } } };

/**
 * The Amplify Authenticator renders the sign-in / sign-up / confirm-code screens against Cognito and
 * only renders its children once a user is signed in.
 */
export function AwsApp({ platform }: { platform: Platform }) {
  return (
    <ThemeProvider theme={theme} colorMode="system">
      <Authenticator signUpAttributes={['name']} loginMechanisms={['email']} components={components} formFields={formFields}>
        {({ signOut }) => <SignedIn platform={platform} signOut={() => signOut?.()} />}
      </Authenticator>
    </ThemeProvider>
  );
}

function SignedIn({ platform, signOut }: { platform: Platform; signOut: () => void }) {
  const [viewer, setViewer] = useState<Viewer | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadViewer().then(
      (loaded) => {
        if (!cancelled) setViewer(loaded);
      },
      (reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const session = useMemo(
    (): Session | null =>
      viewer && {
        viewer,
        roles: rolesFor(viewer.groups),
        signOut,
        refreshGroups: async () => setViewer(await loadViewer(true)),
      },
    [viewer, signOut],
  );

  if (error) {
    return (
      <div className="boot-error">
        <Notice tone="danger" title="Could not read your session">
          <span>{error}</span>
          <span>
            <button type="button" className="btn btn-sm" onClick={signOut}>
              Sign out
            </button>
          </span>
        </Notice>
      </div>
    );
  }
  if (!session) return <LoadingBlock label="Signing in…" />;
  // Keyed by groups: the WebSocket's audiences (USER#, GROUP#managers...) are fixed at $connect, so a
  // permission refresh must also reconnect the socket with the new token.
  return <AppRoot key={session.viewer.groups.join(',')} platform={platform} session={session} />;
}
