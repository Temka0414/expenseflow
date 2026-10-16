import { useEffect, useState, type ReactNode } from 'react';
import { LiveHub } from './hub';
import { LiveContext } from './LiveContext';
import type { LiveSource } from './source';

/**
 * Owns the single live connection of the app. StrictMode mounts effects twice in development
 * (start -> stop -> start); the source's stop function tears the first connection down completely.
 * The source is read once: give the provider a new `key` to switch to a different source.
 */
export function LiveEventsProvider({ source, children }: { source: LiveSource; children: ReactNode }) {
  const [hub] = useState(() => new LiveHub(source));
  useEffect(() => hub.start(), [hub]);
  return <LiveContext value={hub}>{children}</LiveContext>;
}
