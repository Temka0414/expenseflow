import type { LiveEvent } from './events';

export type LiveStatus = 'connecting' | 'open' | 'reconnecting' | 'stopped';

export interface LiveHandlers {
  onEvent: (event: LiveEvent) => void;
  /** `retryInMs` is set while waiting to reconnect. */
  onStatus: (status: LiveStatus, retryInMs?: number) => void;
}

/** Where live events come from: the WebSocket API in AWS mode, an in-process emitter in demo mode. */
export interface LiveSource {
  /** Human-readable endpoint, shown on the Learn page. */
  readonly description: string;
  /** Start delivering events; returns a function that stops and releases everything. */
  connect(handlers: LiveHandlers): () => void;
}
