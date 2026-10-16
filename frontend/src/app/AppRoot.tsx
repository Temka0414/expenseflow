import { Toaster } from '../components/Toaster';
import { LiveEventsProvider } from '../live/LiveEventsProvider';
import { PlatformContext, SessionContext } from '../platform/context';
import type { Platform, Session } from '../platform/platform';
import { AppRoutes } from './AppRoutes';
import { LiveToasts } from './LiveToasts';

/**
 * Everything below the sign-in boundary. AWS mode and demo mode both render this with their own
 * Platform (API transport, live events source, S3 uploader) and Session (who is signed in).
 */
export function AppRoot({ platform, session }: { platform: Platform; session: Session }) {
  return (
    <PlatformContext value={platform}>
      <SessionContext value={session}>
        <LiveEventsProvider source={platform.live}>
          <AppRoutes />
          <LiveToasts />
          <Toaster />
        </LiveEventsProvider>
      </SessionContext>
    </PlatformContext>
  );
}
