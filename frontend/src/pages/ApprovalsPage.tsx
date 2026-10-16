import { useState } from 'react';
import { Link } from 'react-router';
import { toApiError, type ApiError } from '../api/errors';
import type { Decision, Expense } from '../api/types';
import { Dialog } from '../components/Dialog';
import { ExpenseRow } from '../components/ExpenseRow';
import { HistoryTimeline } from '../components/HistoryTimeline';
import { ReceiptPreview } from '../components/ReceiptPreview';
import { toasts } from '../components/toastStore';
import { Card, EmptyState, ErrorNotice, LoadingBlock, Notice, PageHeader, Spinner, StatusBadge } from '../components/ui';
import { CATEGORY_LABELS, LIMITS } from '../domain';
import { commentIssue } from '../expenses/validation';
import { verificationHints } from '../expenses/verification';
import { DETAIL_TYPES } from '../live/events';
import { useLiveRefresh } from '../live/LiveContext';
import { formatCalendarDate, formatDateTime, formatMoney } from '../lib/format';
import { usePagedList, useResource } from '../lib/useResource';
import { useApi } from '../platform/context';

const QUEUE_EVENTS = new Set<string>([DETAIL_TYPES.submitted, DETAIL_TYPES.withdrawn, DETAIL_TYPES.escalated]);

export default function ApprovalsPage() {
  const api = useApi();
  // GET /approvals queries GSI2 (STATUS#PENDING_APPROVAL), oldest first, without the caller's own expenses.
  const queue = usePagedList('approvals', (cursor, limit) => api.listApprovals({ cursor, limit }));
  const [selected, setSelected] = useState<Expense | null>(null);
  // Decisions are recorded asynchronously by the workflow: hide decided items until the queue catches up.
  const [decided, setDecided] = useState<ReadonlySet<string>>(new Set());
  useLiveRefresh((event) => QUEUE_EVENTS.has(event.detailType), queue.reload);

  const items = queue.items.filter((expense) => !decided.has(expense.id));

  return (
    <div className="page">
      <PageHeader
        title="Approvals"
        description="Expenses from your team waiting for a decision, oldest first. Your own expenses go to another manager."
      />
      <Card
        flush
        footer={
          queue.hasMore && (
            <button type="button" className="btn btn-sm" onClick={queue.loadMore} disabled={queue.loadingMore}>
              {queue.loadingMore && <Spinner small />} Load more
            </button>
          )
        }
      >
        {queue.error && <ErrorNotice error={queue.error} onRetry={queue.reload} />}
        {queue.loading && <LoadingBlock />}
        {!queue.loading && !queue.error && items.length === 0 && (
          <EmptyState title="All caught up">Nothing is waiting for your decision. New submissions appear here live.</EmptyState>
        )}
        {items.length > 0 && (
          <ul className="item-list">
            {items.map((expense) => (
              <li key={expense.id}>
                <ExpenseRow expense={expense} showOwner onSelect={setSelected} />
              </li>
            ))}
          </ul>
        )}
      </Card>
      {selected && (
        <ReviewDrawer
          key={selected.id}
          expense={selected}
          onClose={() => setSelected(null)}
          onDecided={(id) => {
            setDecided((current) => new Set(current).add(id));
            setSelected(null);
          }}
        />
      )}
    </div>
  );
}

function ReviewDrawer({ expense: summary, onClose, onDecided }: { expense: Expense; onClose: () => void; onDecided: (id: string) => void }) {
  const api = useApi();
  const detail = useResource(`expense:${summary.id}`, () => api.getExpense(summary.id));
  const [decision, setDecision] = useState<Decision | null>(null);
  const expense = detail.data?.expense ?? summary;
  const hints = verificationHints(expense);

  return (
    <Dialog
      open
      variant="drawer"
      onClose={onClose}
      title="Review expense"
      footer={
        <>
          <Link to={`/expenses/${expense.id}`} className="btn btn-ghost">
            Open full page
          </Link>
          <span className="spacer" />
          <button type="button" className="btn btn-danger-outline" onClick={() => setDecision('REJECTED')}>
            Reject
          </button>
          <button type="button" className="btn btn-success" onClick={() => setDecision('APPROVED')}>
            Approve
          </button>
        </>
      }
    >
      <div className="stack-sm">
        <div className="row-between">
          <h3>{expense.title}</h3>
          <StatusBadge status={expense.status} />
        </div>
        <span className="tile__value">{formatMoney(expense.amount, expense.currency)}</span>
      </div>
      <dl className="kv">
        <dt>Employee</dt>
        <dd>
          {expense.ownerName} <span className="subtle">{expense.ownerEmail}</span>
        </dd>
        <dt>Merchant</dt>
        <dd>{expense.merchant ?? '—'}</dd>
        <dt>Category</dt>
        <dd>{expense.category ? CATEGORY_LABELS[expense.category] : '—'}</dd>
        <dt>Date</dt>
        <dd>{formatCalendarDate(expense.expenseDate)}</dd>
        <dt>Submitted</dt>
        <dd>{formatDateTime(expense.submittedAt)}</dd>
        {expense.description && (
          <>
            <dt>Description</dt>
            <dd>{expense.description}</dd>
          </>
        )}
      </dl>
      {expense.approval?.escalated && <Notice tone="warning" title="Escalated">This request waited past the approval timeout.</Notice>}
      <section className="stack-sm" aria-label="AI verification">
        <h3>
          <span className="ai-header">
            <span className="ai-mark" aria-hidden="true">
              AI
            </span>
            Receipt check
          </span>
        </h3>
        <ul className="verify">
          {hints.map((hint) => (
            <li key={hint.text}>
              <span className={hint.ok ? 'verify__ok' : 'verify__warn'} aria-label={hint.ok ? 'OK' : 'Check'}>
                {hint.ok ? '✓' : '!'}
              </span>
              <span>{hint.text}</span>
            </li>
          ))}
        </ul>
      </section>
      {expense.receipt && <ReceiptPreview expenseId={expense.id} receipt={expense.receipt} />}
      {detail.error && <ErrorNotice error={detail.error} onRetry={detail.reload} title="Could not load the full expense" />}
      {detail.data && (
        <section className="stack-sm">
          <h3>History</h3>
          <HistoryTimeline history={detail.data.history} />
        </section>
      )}
      {decision && (
        <DecisionDialog
          expense={expense}
          decision={decision}
          onCancel={() => setDecision(null)}
          onDone={() => {
            setDecision(null);
            onDecided(expense.id);
          }}
        />
      )}
    </Dialog>
  );
}

function DecisionDialog({
  expense,
  decision,
  onCancel,
  onDone,
}: {
  expense: Expense;
  decision: Decision;
  onCancel: () => void;
  onDone: () => void;
}) {
  const api = useApi();
  const [comment, setComment] = useState('');
  const [touched, setTouched] = useState(false);
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const problem = commentIssue(decision, comment);
  const approving = decision === 'APPROVED';

  async function send() {
    setTouched(true);
    if (problem) return;
    setSending(true);
    setFailure(null);
    try {
      await api.decide(expense.id, decision, comment.trim() || null);
      toasts.push({
        tone: 'success',
        title: approving ? 'Approval sent' : 'Rejection sent',
        text: 'SendTaskSuccess resumed the paused workflow; it records the decision in a moment.',
      });
      onDone();
    } catch (error) {
      setFailure(toApiError(error));
      setSending(false);
    }
  }

  const notReady = failure?.isConflict && /not ready/i.test(failure.message);
  return (
    <Dialog
      open
      onClose={onCancel}
      title={approving ? 'Approve expense' : 'Reject expense'}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={sending}>
            Cancel
          </button>
          <button type="button" className={approving ? 'btn btn-success' : 'btn btn-danger'} onClick={() => void send()} disabled={sending}>
            {sending && <Spinner small />} {approving ? 'Approve' : 'Reject'}
          </button>
        </>
      }
    >
      <p>
        {expense.ownerName}: &ldquo;{expense.title}&rdquo;, {formatMoney(expense.amount, expense.currency)}
      </p>
      <div className="field">
        <label className="field__label" htmlFor="decision-comment">
          Comment {approving ? <span className="field__required">(optional)</span> : <span className="field__required">(required)</span>}
        </label>
        <textarea
          id="decision-comment"
          className="textarea"
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          aria-invalid={touched && problem ? true : undefined}
          aria-describedby="decision-comment-hint"
          placeholder={approving ? 'Anything the employee should know?' : 'What needs to change before you can approve it?'}
        />
        {touched && problem ? (
          <span className="field__error" id="decision-comment-hint">
            {problem}
          </span>
        ) : (
          <span className="field__hint" id="decision-comment-hint">
            {comment.trim().length}/{LIMITS.commentMax} characters
          </span>
        )}
      </div>
      {notReady ? (
        <Notice tone="warning" title="The workflow is not waiting for a decision yet">
          Step Functions starts a moment after submission (DynamoDB stream → EventBridge rule → ApprovalWorkflow), and the
          approval_request task must store its task token first. Try again in a few seconds.
        </Notice>
      ) : (
        failure && <ErrorNotice error={failure} title="The decision was not accepted" />
      )}
    </Dialog>
  );
}
