import type { Transport } from './client';

/**
 * Transport for the real HTTP API.
 *
 * - `Authorization: Bearer <ID token>`: the HTTP API's JWT authorizer validates signature, issuer,
 *   expiry and `aud` (= app client id) before Lambda is invoked. We send the *ID* token because it
 *   carries the `email` and `name` claims the backend records on expenses.
 * - The token getter runs before every request so Amplify can refresh an expired token transparently.
 * - `apigw-requestid` is only readable cross-origin because the API's CORS config lists it in
 *   `expose_headers`; without that the browser hides every non-safelisted response header.
 */
export function createFetchTransport(apiUrl: string, getIdToken: () => Promise<string>): Transport {
  const base = apiUrl.replace(/\/+$/, '');
  return async ({ method, path, body }) => {
    const headers: Record<string, string> = { Authorization: `Bearer ${await getIdToken()}` };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(`${base}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    return {
      status: response.status,
      body: parseJson(text),
      requestId: response.headers.get('apigw-requestid'),
    };
  };
}

function parseJson(text: string): unknown {
  if (text === '') return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    // e.g. an HTML error page from a proxy: keep the text so the error message can show it.
    return { message: text.slice(0, 200) };
  }
}
