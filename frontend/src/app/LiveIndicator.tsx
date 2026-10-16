import { Link } from 'react-router';
import { useLiveSnapshot } from '../live/LiveContext';
import type { LiveStatus } from '../live/source';

const LABELS: Record<LiveStatus, string> = {
  connecting: 'Connecting…',
  open: 'Live',
  reconnecting: 'Reconnecting…',
  stopped: 'Offline',
};

/** WebSocket connection state; links to the raw event log on the Learn page. */
export function LiveIndicator() {
  const { status, retryInMs } = useLiveSnapshot();
  const detail =
    status === 'reconnecting' && retryInMs !== null
      ? `Live updates: reconnecting in ${Math.ceil(retryInMs / 1000)} s (exponential backoff)`
      : `Live updates: ${LABELS[status].toLowerCase()}`;
  return (
    <Link to="/learn#live" className="live-indicator" title={detail} aria-label={detail}>
      <span className={`live-dot live-dot--${status}`} aria-hidden="true" />
      <span className="live-indicator__text">{LABELS[status]}</span>
    </Link>
  );
}
