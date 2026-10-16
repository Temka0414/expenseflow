/**
 * "Request inspector": an in-memory log of every API call this tab made, shown on the Learn page.
 * The `apigw-requestid` header is the thread that ties a click in the browser to the HTTP API access
 * log line and (via Powertools' correlation id) to the Lambda logs in CloudWatch.
 *
 * A tiny external store consumed with `useSyncExternalStore`: snapshots are immutable arrays, so React
 * re-renders only when an entry is added.
 */
import { useSyncExternalStore } from 'react';

export type RequestTarget = 'api' | 's3';

export interface InspectedRequest {
  id: number;
  target: RequestTarget;
  method: string;
  path: string;
  /** 0 = no response (network error / CORS). */
  status: number;
  durationMs: number;
  requestId: string | null;
  startedAt: string;
  error: string | null;
}

const MAX_ENTRIES = 100;

class RequestLog {
  #entries: readonly InspectedRequest[] = [];
  #listeners = new Set<() => void>();
  #nextId = 1;

  record(entry: Omit<InspectedRequest, 'id'>): void {
    this.#entries = [{ ...entry, id: this.#nextId++ }, ...this.#entries].slice(0, MAX_ENTRIES);
    this.#emit();
  }

  clear(): void {
    this.#entries = [];
    this.#emit();
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  readonly getSnapshot = (): readonly InspectedRequest[] => this.#entries;

  #emit(): void {
    for (const listener of this.#listeners) listener();
  }
}

export const requestLog = new RequestLog();

export function useRequestLog(): readonly InspectedRequest[] {
  return useSyncExternalStore(requestLog.subscribe, requestLog.getSnapshot);
}
