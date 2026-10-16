import { createContext, use, useEffect, useEffectEvent, useSyncExternalStore } from 'react';
import type { LiveEvent } from './events';
import type { LiveHub, LiveSnapshot } from './hub';

export const LiveContext = createContext<LiveHub | null>(null);

export function useLiveHub(): LiveHub {
  const hub = use(LiveContext);
  if (!hub) throw new Error('useLiveHub() must be used inside <LiveEventsProvider>');
  return hub;
}

export function useLiveSnapshot(): LiveSnapshot {
  const hub = useLiveHub();
  return useSyncExternalStore(hub.subscribeSnapshot, hub.getSnapshot);
}

/** Run `onEvent` for every live event; the latest callback is always used without resubscribing. */
export function useLiveEvent(onEvent: (event: LiveEvent) => void): void {
  const hub = useLiveHub();
  const handle = useEffectEvent(onEvent);
  useEffect(() => hub.subscribe((event) => handle(event)), [hub]);
}

/**
 * Keep server data fresh: call `refresh` when a relevant event arrives and after every reconnect
 * (pushes sent while the socket was down are not replayed, so a refetch closes the gap).
 */
export function useLiveRefresh(isRelevant: (event: LiveEvent) => boolean, refresh: () => void): void {
  const hub = useLiveHub();
  const onEvent = useEffectEvent((event: LiveEvent) => {
    if (isRelevant(event)) refresh();
  });
  const onReconnect = useEffectEvent(() => refresh());
  useEffect(() => {
    const stopEvents = hub.subscribe((event) => onEvent(event));
    const stopReconnect = hub.onReconnect(() => onReconnect());
    return () => {
      stopEvents();
      stopReconnect();
    };
  }, [hub]);
}
