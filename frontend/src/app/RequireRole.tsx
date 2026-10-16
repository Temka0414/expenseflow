import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { Notice } from '../components/ui';
import { usePlatform } from '../platform/context';

/**
 * Hides pages the user's Cognito groups do not unlock. This is UX only: the API enforces the same
 * rule on every request (403), because anything in the browser can be bypassed.
 */
export function RequireRole({ allowed, who, children }: { allowed: boolean; who: string; children: ReactNode }) {
  const { mode } = usePlatform();
  if (allowed) return children;
  return (
    <div className="page">
      <Notice tone="warning" title={`This page is for ${who}`}>
        <span>
          {mode === 'demo' ? (
            <>Switch the role in the purple demo bar above to try it.</>
          ) : (
            <>
              Ask an administrator to run <code>scripts/add_user_to_group.py</code> for your account, then use
              &ldquo;Refresh permissions&rdquo; in the user menu (group membership travels inside the ID token).
            </>
          )}
        </span>
        <Link to="/">Back to the dashboard</Link>
      </Notice>
    </div>
  );
}
