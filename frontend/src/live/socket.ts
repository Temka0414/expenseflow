/**
 * A WebSocket that stays connected to the API Gateway WebSocket API (SPEC §6).
 *
 * API Gateway facts this code is shaped around:
 *  - Browsers cannot set headers on the WebSocket handshake, so the Cognito *access* token travels in
 *    the query string (`?token=`) and the `$connect` Lambda authorizer validates it. The token is read
 *    fresh for every connection attempt, so a reconnect after an hour uses a refreshed token.
 *  - Idle connections are dropped after 10 minutes: we send `{"action":"ping"}` every 5 minutes (routed
 *    to `$default`, which just returns 200).
 *  - Every connection is closed after 2 hours no matter what: reconnecting is normal operation, not an
 *    error, hence the "stable connection" rule below that resets the backoff.
 *  - A rejected `$connect` (401) surfaces in the browser as a bare close (code 1006) without a reason,
 *    so authorization failures look like network failures here; backoff keeps them from hammering the API.
 */
import { backoffDelay, DEFAULT_BACKOFF, type BackoffOptions } from './backoff';
import { parseLiveMessage } from './events';
import type { LiveHandlers, LiveSource } from './source';

/** The subset of the browser WebSocket used here (lets tests substitute a fake). */
export interface WebSocketLike {
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

const OPEN = 1;
export const PING_INTERVAL_MS = 5 * 60_000;
/** A connection that lived this long counts as healthy: the next reconnect starts from attempt 0. */
export const STABLE_CONNECTION_MS = 30_000;
export const PING_MESSAGE = JSON.stringify({ action: 'ping' });

export interface ReconnectingSocketOptions {
  /** Resolves the URL (including a fresh token) for each connection attempt. */
  resolveUrl: () => Promise<string>;
  createSocket?: (url: string) => WebSocketLike;
  backoff?: BackoffOptions;
  random?: () => number;
  pingIntervalMs?: number;
  /** Calls `wake` when reconnecting right away makes sense; returns an unsubscribe function. */
  wakeSignals?: (wake: () => void) => () => void;
}

/** Reconnect immediately when the device comes back online or the tab becomes visible again. */
function browserWakeSignals(wake: () => void): () => void {
  if (typeof window === 'undefined' || typeof document === 'undefined') return () => undefined;
  const onVisible = () => {
    if (document.visibilityState === 'visible') wake();
  };
  window.addEventListener('online', wake);
  document.addEventListener('visibilitychange', onVisible);
  return () => {
    window.removeEventListener('online', wake);
    document.removeEventListener('visibilitychange', onVisible);
  };
}

export function connectReconnectingSocket(options: ReconnectingSocketOptions, handlers: LiveHandlers): () => void {
  const createSocket = options.createSocket ?? ((url: string) => new WebSocket(url));
  const backoff = options.backoff ?? DEFAULT_BACKOFF;
  const random = options.random ?? Math.random;
  const pingIntervalMs = options.pingIntervalMs ?? PING_INTERVAL_MS;

  let disposed = false;
  let attempt = 0;
  let socket: WebSocketLike | null = null;
  let openedAt: number | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let pingTimer: ReturnType<typeof setInterval> | undefined;

  const stopPing = () => {
    clearInterval(pingTimer);
    pingTimer = undefined;
  };

  const scheduleReconnect = () => {
    if (disposed) return;
    const delay = backoffDelay(attempt, backoff, random);
    attempt += 1;
    handlers.onStatus('reconnecting', delay);
    retryTimer = setTimeout(() => void open(), delay);
  };

  const open = async () => {
    retryTimer = undefined;
    handlers.onStatus('connecting');
    let ws: WebSocketLike;
    try {
      const url = await options.resolveUrl();
      if (disposed) return;
      ws = createSocket(url);
    } catch {
      // No token (signed out mid-session) or an invalid URL: treat like a failed connection.
      scheduleReconnect();
      return;
    }
    socket = ws;
    ws.onopen = () => {
      openedAt = Date.now();
      handlers.onStatus('open');
      pingTimer = setInterval(() => {
        if (ws.readyState === OPEN) ws.send(PING_MESSAGE);
      }, pingIntervalMs);
    };
    ws.onmessage = (message) => {
      const event = parseLiveMessage(message.data);
      if (event) handlers.onEvent(event);
    };
    // No onerror handler: the browser hides error details and `close` always follows, so the close
    // handler is the single place that decides what happens next.
    ws.onclose = () => {
      stopPing();
      socket = null;
      if (openedAt !== null && Date.now() - openedAt >= STABLE_CONNECTION_MS) attempt = 0;
      openedAt = null;
      scheduleReconnect();
    };
  };

  const wake = () => {
    if (disposed || socket !== null || retryTimer === undefined) return;
    clearTimeout(retryTimer);
    attempt = 0;
    void open();
  };
  const stopWakeSignals = (options.wakeSignals ?? browserWakeSignals)(wake);

  void open();

  return () => {
    disposed = true;
    clearTimeout(retryTimer);
    stopPing();
    stopWakeSignals();
    if (socket) {
      socket.onclose = null;
      socket.close(1000, 'client closed');
      socket = null;
    }
    handlers.onStatus('stopped');
  };
}

/** LiveSource backed by the WebSocket API; `token()` must return a Cognito access token. */
export function webSocketSource(wsUrl: string, token: () => Promise<string>): LiveSource {
  return {
    description: wsUrl,
    connect: (handlers) =>
      connectReconnectingSocket(
        {
          resolveUrl: async () => {
            const url = new URL(wsUrl);
            url.searchParams.set('token', await token());
            return url.toString();
          },
        },
        handlers,
      ),
  };
}
