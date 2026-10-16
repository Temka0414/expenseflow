import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backoffCeiling, backoffDelay, DEFAULT_BACKOFF } from './backoff';
import { connectReconnectingSocket, PING_MESSAGE, STABLE_CONNECTION_MS, type WebSocketLike } from './socket';
import type { LiveStatus } from './source';

describe('backoff schedule', () => {
  it('doubles the ceiling per attempt and caps it at 60 s', () => {
    const ceilings = Array.from({ length: 9 }, (_, attempt) => backoffCeiling(attempt));
    expect(ceilings).toEqual([1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 60_000, 60_000, 60_000]);
    expect(backoffCeiling(10_000)).toBe(60_000);
  });

  it('draws the delay between the floor and the ceiling (full jitter)', () => {
    expect(backoffDelay(3, DEFAULT_BACKOFF, () => 0)).toBe(250);
    expect(backoffDelay(3, DEFAULT_BACKOFF, () => 1)).toBe(8_000);
    expect(backoffDelay(3, DEFAULT_BACKOFF, () => 0.5)).toBe(4_125);
  });

  it('never goes below the floor or above the cap, whatever the random source returns', () => {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      for (const r of [0, 0.25, 0.999]) {
        const delay = backoffDelay(attempt, DEFAULT_BACKOFF, () => r);
        expect(delay).toBeGreaterThanOrEqual(DEFAULT_BACKOFF.minMs);
        expect(delay).toBeLessThanOrEqual(DEFAULT_BACKOFF.maxMs);
      }
    }
  });
});

class FakeSocket implements WebSocketLike {
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readonly sent: string[] = [];
  closedByClient = false;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.closedByClient = true;
    this.readyState = 3;
  }

  serverOpens(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  serverCloses(): void {
    this.readyState = 3;
    this.onclose?.({ code: 1006 } as CloseEvent);
  }

  serverSends(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

describe('reconnecting socket', () => {
  let sockets: FakeSocket[];
  let statuses: Array<[LiveStatus, number | undefined]>;
  let events: unknown[];
  let tokenCalls: number;

  beforeEach(() => {
    vi.useFakeTimers();
    sockets = [];
    statuses = [];
    events = [];
    tokenCalls = 0;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function connect() {
    return connectReconnectingSocket(
      {
        resolveUrl: () => {
          tokenCalls += 1;
          return Promise.resolve(`wss://example.test/live?token=t${tokenCalls}`);
        },
        createSocket: (url) => {
          const socket = new FakeSocket(url);
          sockets.push(socket);
          return socket;
        },
        random: () => 1, // always wait for the full ceiling: makes the schedule observable
        wakeSignals: () => () => undefined,
      },
      {
        onEvent: (event) => events.push(event),
        onStatus: (status, retryInMs) => statuses.push([status, retryInMs]),
      },
    );
  }

  const retryDelays = () => statuses.filter(([s]) => s === 'reconnecting').map(([, ms]) => ms);

  it('backs off exponentially while connections keep failing, with a fresh token each time', async () => {
    const stop = connect();
    await vi.advanceTimersByTimeAsync(0);
    for (const wait of [1_000, 2_000, 4_000, 8_000]) {
      sockets.at(-1)?.serverCloses();
      await vi.advanceTimersByTimeAsync(wait);
    }
    expect(retryDelays()).toEqual([1_000, 2_000, 4_000, 8_000]);
    expect(sockets.map((s) => s.url)).toEqual([
      'wss://example.test/live?token=t1',
      'wss://example.test/live?token=t2',
      'wss://example.test/live?token=t3',
      'wss://example.test/live?token=t4',
      'wss://example.test/live?token=t5',
    ]);
    stop();
  });

  it('resets the backoff after a stable connection (e.g. the 2-hour API Gateway limit)', async () => {
    const stop = connect();
    await vi.advanceTimersByTimeAsync(0);
    sockets[0].serverCloses(); // attempt 0 -> 1 s
    await vi.advanceTimersByTimeAsync(1_000);
    sockets[1].serverOpens();
    sockets[1].serverCloses(); // flapping: opened and closed immediately -> keeps backing off (2 s)
    await vi.advanceTimersByTimeAsync(2_000);
    sockets[2].serverOpens();
    await vi.advanceTimersByTimeAsync(STABLE_CONNECTION_MS);
    sockets[2].serverCloses(); // healthy connection ended -> back to 1 s
    expect(retryDelays()).toEqual([1_000, 2_000, 1_000]);
    stop();
  });

  it('pings every 5 minutes while open and forwards parsed events', async () => {
    const stop = connect();
    await vi.advanceTimersByTimeAsync(0);
    sockets[0].serverOpens();
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(sockets[0].sent).toEqual([PING_MESSAGE, PING_MESSAGE]);

    sockets[0].serverSends({ type: 'event', source: 'expenseflow.expenses', detailType: 'Expense Approved', time: 't', detail: { expenseId: 'x' } });
    sockets[0].serverSends({ type: 'pong' });
    expect(events).toHaveLength(1);
    stop();
  });

  it('stops cleanly: closes the socket and never reconnects', async () => {
    const stop = connect();
    await vi.advanceTimersByTimeAsync(0);
    sockets[0].serverOpens();
    stop();
    expect(sockets[0].closedByClient).toBe(true);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(sockets).toHaveLength(1);
    expect(statuses.at(-1)).toEqual(['stopped', undefined]);
  });

  it('does not open a socket when stopped while the token was still being fetched (React StrictMode)', async () => {
    const stop = connect();
    stop();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(sockets).toHaveLength(0);
  });
});
