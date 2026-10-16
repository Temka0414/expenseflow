/**
 * Cognito tokens via Amplify. `fetchAuthSession()` returns the cached tokens and transparently uses the
 * refresh token when they are about to expire (ID/access tokens live 1 hour by default), so callers
 * ask for a token right before every request instead of storing one.
 */
import { fetchAuthSession } from 'aws-amplify/auth';
import { parseGroups } from '../auth/groups';
import type { Viewer } from '../platform/platform';

async function tokens(forceRefresh = false) {
  const session = await fetchAuthSession({ forceRefresh });
  if (!session.tokens) throw new Error('You are signed out. Sign in again.');
  return session.tokens;
}

/** ID token: for the HTTP API's JWT authorizer (it carries `email`, `name` and `cognito:groups`). */
export async function idToken(): Promise<string> {
  const { idToken: token } = await tokens();
  if (!token) throw new Error('The session has no ID token. Sign in again.');
  return token.toString();
}

/** Access token: for the WebSocket `$connect` authorizer, which validates it with Cognito `GetUser`. */
export async function accessToken(): Promise<string> {
  return (await tokens()).accessToken.toString();
}

/**
 * Who is signed in, read from the ID token's claims. `forceRefresh` swaps the tokens for new ones -
 * needed after an admin changes the user's groups, because groups are baked into the token when it is issued.
 */
export async function loadViewer(forceRefresh = false): Promise<Viewer> {
  const { idToken: token } = await tokens(forceRefresh);
  const claims = token?.payload;
  if (!claims?.sub) throw new Error('The session has no ID token. Sign in again.');
  const email = typeof claims.email === 'string' ? claims.email : '';
  const name = typeof claims.name === 'string' && claims.name.trim() !== '' ? claims.name : email;
  return { userId: claims.sub, email, name, groups: parseGroups(claims['cognito:groups']) };
}
