import type { HistoryEntry } from '../api/types';
import { formatDateTime, formatRelative } from '../lib/format';
import { useNow } from '../lib/useNow';

type DotTone = 'success' | 'danger' | 'warning' | 'info' | 'violet' | 'neutral';

const SYSTEM = 'system';

/** Human wording for one HISTORY item (written by stream_processor from the DynamoDB stream). */
function describe(entry: HistoryEntry): { title: string; tone: DotTone } {
  const by = entry.actorEmail && entry.actorEmail !== SYSTEM ? entry.actorEmail : null;
  if (entry.kind === 'RECEIPT') {
    if (entry.extractionStatus === 'SUCCEEDED') return { title: 'Claude read the receipt', tone: 'violet' };
    if (entry.extractionStatus === 'FAILED') return { title: 'Receipt reading failed', tone: 'danger' };
    return { title: 'Receipt stored (AI extraction off)', tone: 'neutral' };
  }
  const { from, to } = entry;
  if (from === null && to === 'DRAFT') return { title: `Created as a draft${by ? ` by ${by}` : ''}`, tone: 'info' };
  if (to === 'PENDING_APPROVAL') return { title: 'Submitted for approval', tone: 'warning' };
  if (to === 'APPROVED') return { title: by ? `Approved by ${by}` : 'Auto-approved by policy', tone: 'success' };
  if (to === 'REJECTED') return { title: by ? `Rejected by ${by}` : 'Automatically rejected', tone: 'danger' };
  if (from === 'PENDING_APPROVAL' && to === 'DRAFT') return { title: 'Withdrawn by the owner', tone: 'neutral' };
  if (from === 'REJECTED' && to === 'DRAFT') return { title: 'Reopened for editing', tone: 'info' };
  return { title: `${from ?? '—'} → ${to ?? '—'}`, tone: 'neutral' };
}

export function HistoryTimeline({ history }: { history: HistoryEntry[] }) {
  const now = useNow();
  if (history.length === 0) {
    return <p className="muted">The audit trail appears here a moment after each change (DynamoDB stream → stream_processor).</p>;
  }
  return (
    <ol className="timeline">
      {history.map((entry) => {
        const { title, tone } = describe(entry);
        return (
          <li key={`${entry.at}#${entry.kind}#${entry.to ?? entry.extractionStatus ?? ''}`} className="timeline__item">
            <span className={`timeline__dot timeline__dot--${tone}`} aria-hidden="true" />
            <div className="timeline__title">{title}</div>
            <div className="timeline__meta">
              <time dateTime={entry.at} title={formatDateTime(entry.at)}>
                {formatRelative(entry.at, now)}
              </time>
            </div>
            {entry.comment && <div className="timeline__comment">{entry.comment}</div>}
          </li>
        );
      })}
    </ol>
  );
}
