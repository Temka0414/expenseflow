/**
 * Fan-out point between one LiveSource connection and many React components.
 * Components subscribe to events (to refresh data) while the connection status and a short raw event
 * log (for the Learn page) are exposed as an immutable snapshot for `useSyncExternalStore`.
 */
import type { LiveEvent } from './events';
import type { LiveSource, LiveStatus } from './source';

export interface LoggedEvent {
  seq: number;
  receivedAt: string;
  event: LiveEvent;
}

export interface LiveSnapshot {
  status: LiveStatus;
  retryInMs: number | null;
  connectedAt: string | null;
  log: readonly LoggedEvent[];
}

const LOG_SIZE = 50;

export class LiveHub {
  readonly source: LiveSource;
  #snapshot: LiveSnapshot = { status: 'connecting', retryInMs: null, connectedAt: null, log: [] };
  #eventListeners = new Set<(event: LiveEvent) => void>();
  #reconnectListeners = new Set<() => void>();
  #snapshotListeners = new Set<() => void>();
  #seq = 0;
  #hasConnectedBefore = false;

  constructor(source: LiveSource) {
    this.source = source;
  }

  /** Connect; returns the disconnect function (React effect cleanup - safe to call start() again). */
  start(): () => void {
    return this.source.connect({
      onEvent: (event) => this.#receive(event),
      onStatus: (status, retryInMs) => this.#setStatus(status, retryInMs),
    });
  }

  subscribe(listener: (event: LiveEvent) => void): () => void {
    this.#eventListeners.add(listener);
    return () => this.#eventListeners.delete(listener);
  }

  /** Called after a *re*connect: events sent while we were offline are lost, so listeners should refetch. */
  onReconnect(listener: () => void): () => void {
    this.#reconnectListeners.add(listener);
    return () => this.#reconnectListeners.delete(listener);
  }

  readonly subscribeSnapshot = (listener: () => void): (() => void) => {
    this.#snapshotListeners.add(listener);
    return () => this.#snapshotListeners.delete(listener);
  };

  readonly getSnapshot = (): LiveSnapshot => this.#snapshot;

  clearLog(): void {
    this.#update({ log: [] });
  }

  #receive(event: LiveEvent): void {
    this.#seq += 1;
    const entry: LoggedEvent = { seq: this.#seq, receivedAt: new Date().toISOString(), event };
    this.#update({ log: [entry, ...this.#snapshot.log].slice(0, LOG_SIZE) });
    for (const listener of this.#eventListeners) listener(event);
  }

  #setStatus(status: LiveStatus, retryInMs?: number): void {
    const connectedAt = status === 'open' ? new Date().toISOString() : status === 'stopped' ? null : this.#snapshot.connectedAt;
    this.#update({ status, retryInMs: retryInMs ?? null, connectedAt });
    if (status !== 'open') return;
    if (this.#hasConnectedBefore) for (const listener of this.#reconnectListeners) listener();
    this.#hasConnectedBefore = true;
  }

  #update(patch: Partial<LiveSnapshot>): void {
    this.#snapshot = { ...this.#snapshot, ...patch };
    for (const listener of this.#snapshotListeners) listener();
  }
}
