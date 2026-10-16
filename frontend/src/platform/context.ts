import { createContext, use } from 'react';
import type { Api } from '../api/client';
import type { Platform, Session } from './platform';

export const PlatformContext = createContext<Platform | null>(null);
export const SessionContext = createContext<Session | null>(null);

export function usePlatform(): Platform {
  const platform = use(PlatformContext);
  if (!platform) throw new Error('usePlatform() must be used inside <PlatformContext>');
  return platform;
}

export function useApi(): Api {
  return usePlatform().api;
}

export function useSession(): Session {
  const session = use(SessionContext);
  if (!session) throw new Error('useSession() must be used inside <SessionContext>');
  return session;
}
