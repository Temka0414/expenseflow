import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { toApiError, type ApiError } from '../api/errors';
import type { Expense, ExpenseDetail } from '../api/types';
import { AiPanel } from '../components/AiPanel';
import { ConfirmDialog } from '../components/Dialog';
import { ExpenseForm } from '../components/ExpenseForm';
import { HistoryTimeline } from '../components/HistoryTimeline';
import { ReceiptPreview } from '../components/ReceiptPreview';
import { ReceiptUploader } from '../components/ReceiptUploader';
import { toasts } from '../components/toastStore';
import { Card, ErrorNotice, LoadingBlock, Notice, PageHeader, Spinner, StatusBadge } from '../components/ui';
import { CATEGORY_LABELS } from '../domain';
import { checkAutoApproval } from '../expenses/autoApproval';
import { changedFields, patchFor, rebaseValues, validateForm, valuesFromExpense, type FormErrors, type FormValues } from '../expenses/form';
import { serverFieldErrors, withoutField } from '../expenses/serverErrors';
import { submitBlockers } from '../expenses/submit';
import { patchForSuggestions, type Suggestion } from '../expenses/suggestions';
import type { ExpenseField } from '../expenses/validation';
import { expenseIdOf } from '../live/events';
import { useLiveRefresh } from '../live/LiveContext';
import { executionLink, s3ObjectLink, consoleLinks } from '../lib/console';
import { todayIso } from '../lib/dates';
import { formatCalendarDate, formatDateTime, formatMoney } from '../lib/format';
import { useResource, type Resource } from '../lib/useResource';
import { useApi, usePlatform, useSession } from '../platform/context';

/** While S3/Step Functions work in the background, poll as a fallback for the events that announce the result. */
const BACKGROUND_POLL_MS = 4_000;

type Busy = 'save' | 'apply' | 'submit' | 'withdraw' | 'delete' | null;

export default function ExpenseDetailPage() {
  const { expenseId = '' } = useParams();
  const api = useApi();
  const detail = useResource(`expense:${expenseId}`, () => api.getExpense(expenseId));
  // Any pushed event about this expense (receipt read, approved, ...) triggers a background refetch.
  useLiveRefresh((event) => expenseIdOf(event) === expenseId, detail.reload);

  if (!detail.data) {
    if (detail.error) {
      return (
        <div className="page">
          <PageHeader eyebrow={<Link to="/expenses">← My expenses</Link>} title={detail.error.isNotFound ? 'Expense not found' : 'Expense'} />
          <ErrorNotice error={detail.error} onRetry={detail.error.isNotFound ? undefined : detail.reload} />
        </div>
      );
    }
    return <LoadingBlock label="Loading expense…" />;
  }
  return <ExpenseView key={expenseId} detail={detail.data} resource={detail} />;
}

function ExpenseView({ detail, resource }: { detail: ExpenseDetail; resource: Resource<ExpenseDetail> }) {
  const api = useApi();
  const platform = usePlatform();
  const { viewer, roles } = useSession();
  const navigate = useNavigate();
  const { expense, history } = detail;
  const { reload, mutate } = resource;

  const [editingRejected, setEditingRejected] = useState(false);
  const [edits, setEdits] = useState<{ base: FormValues; values: FormValues } | null>(null);
  const [showErrors, setShowErrors] = useState(false);
  const [serverErrors, setServerErrors] = useState<FormErrors>({});
  const [busy, setBusy] = useState<Busy>(null);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [conflict, setConflict] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const isOwner = expense.ownerId === viewer.userId;
  const editable = isOwner && (expense.status === 'DRAFT' || (expense.status === 'REJECTED' && editingRejected));

  // The form shows the server copy plus the user's unsaved edits. When the server copy changes underneath
  // (live event, applied suggestion, 409 reload) untouched fields follow it and edited fields are kept.
  const serverValues = valuesFromExpense(expense);
  const values = edits ? rebaseValues(edits.values, edits.base, serverValues) : serverValues;
  const dirty = editable && changedFields(values, serverValues).length > 0;
  const clientErrors = validateForm(values, todayIso());
  const errors: FormErrors = { ...serverErrors, ...(showErrors ? clientErrors : {}) };

  const waitingInBackground = expense.pendingUpload || expense.extraction?.status === 'PROCESSING';
  useEffect(() => {
    if (!waitingInBackground) return;
    const timer = setInterval(reload, BACKGROUND_POLL_MS);
    return () => clearInterval(timer);
  }, [waitingInBackground, reload]);

  const replaceExpense = (updated: Expense) => mutate((current) => current && { ...current, expense: updated });

  async function run(action: Exclude<Busy, null>, work: () => Promise<void>) {
    setBusy(action);
    setFailure(null);
    try {
      await work();
    } catch (error) {
      const apiError = toApiError(error);
      if (apiError.isConflict && (action === 'save' || action === 'apply')) {
        // Optimistic concurrency: someone (or the receipt pipeline) wrote a newer version first.
        setConflict(true);
        reload();
      } else {
        setServerErrors(serverFieldErrors(apiError));
        setFailure(apiError);
      }
    } finally {
      setBusy(null);
    }
  }

  function change(field: ExpenseField, value: string) {
    setEdits({ base: serverValues, values: { ...values, [field]: value } });
    setServerErrors((current) => withoutField(current, field));
  }

  const save = () =>
    run('save', async () => {
      setShowErrors(true);
      if (Object.keys(clientErrors).length > 0) return;
      const updated = await api.updateExpense(expense.id, patchFor(values, serverValues, expense.version));
      replaceExpense(updated);
      setEdits(null);
      setShowErrors(false);
      setConflict(false);
      setEditingRejected(false);
      reload();
      toasts.push({ tone: 'success', title: 'Saved', text: `Version ${updated.version} stored in DynamoDB.` });
    });

  const applySuggestions = (suggestions: Suggestion[]) =>
    run('apply', async () => {
      const updated = await api.updateExpense(expense.id, patchForSuggestions(suggestions, expense.version));
      replaceExpense(updated);
      if (edits) {
        // Applied fields now come from the server: drop any local text for them.
        const fresh = valuesFromExpense(updated);
        const next = { ...values };
        for (const suggestion of suggestions) next[suggestion.field] = fresh[suggestion.field];
        setEdits({ base: fresh, values: next });
      }
      setConflict(false);
      reload();
      toasts.push({ tone: 'success', title: `Applied ${suggestions.length} suggestion${suggestions.length === 1 ? '' : 's'}` });
    });

  const submit = () =>
    run('submit', async () => {
      replaceExpense(await api.submitExpense(expense.id));
      reload();
      toasts.push({
        title: 'Submitted for approval',
        text: 'DynamoDB stream → EventBridge → the ApprovalWorkflow is checking the auto-approval policy.',
      });
    });

  const withdraw = () =>
    run('withdraw', async () => {
      replaceExpense(await api.withdrawExpense(expense.id));
      reload();
      toasts.push({ title: 'Withdrawn', text: 'The workflow execution was stopped; the expense is a draft again.' });
    });

  const remove = () =>
    run('delete', async () => {
      await api.deleteExpense(expense.id);
      setConfirmDelete(false);
      toasts.push({ title: 'Expense deleted', text: 'Its DynamoDB items and receipt objects were removed.' });
      void navigate('/expenses');
    });

  const blockers = submitBlockers(expense, { isOwner, hasUnsavedChanges: dirty, uploading });
  const prediction = checkAutoApproval(expense, { defaultCurrency: platform.config.defaultCurrency, limit: platform.config.autoApproveLimit });
  const backLink = isOwner ? <Link to="/expenses">← My expenses</Link> : roles.canApprove ? <Link to="/approvals">← Approvals</Link> : null;

  return (
    <div className="page">
      <PageHeader
        eyebrow={backLink}
        title={expense.title}
        description={
          <span className="row">
            <StatusBadge status={expense.status} />
            <span className="num">{formatMoney(expense.amount, expense.currency)}</span>
            {!isOwner && <span>· {expense.ownerName}</span>}
          </span>
        }
        actions={
          isOwner && (
            <>
              {expense.status === 'PENDING_APPROVAL' && (
                <button type="button" className="btn" onClick={() => void withdraw()} disabled={busy !== null}>
                  {busy === 'withdraw' && <Spinner small />} Withdraw
                </button>
              )}
              {expense.status === 'REJECTED' && !editingRejected && (
                <button type="button" className="btn btn-primary" onClick={() => setEditingRejected(true)}>
                  Edit &amp; resubmit
                </button>
              )}
              {expense.status === 'DRAFT' && (
                <button type="button" className="btn btn-danger-outline" onClick={() => setConfirmDelete(true)} disabled={busy !== null}>
                  Delete
                </button>
              )}
            </>
          )
        }
      />

      {conflict && (
        <Notice tone="warning" title="This expense changed while you were editing">
          The server has a newer version (now {expense.version}), so your save was rejected with 409 instead of overwriting
          it. Your edits are kept on top of the latest copy: review them and save again.
        </Notice>
      )}
      {failure && <ErrorNotice error={failure} title="That did not work" />}
      <StatusNotice expense={expense} isOwner={isOwner} editingRejected={editingRejected} />

      <div className="detail-layout">
        <div className="stack">
          <Card
            title="Details"
            description={editable ? 'Changes are saved with the version you loaded (optimistic concurrency).' : undefined}
          >
            {editable ? (
              <form
                className="stack"
                noValidate
                onSubmit={(event) => {
                  event.preventDefault();
                  void save();
                }}
              >
                <ExpenseForm values={values} errors={errors} onChange={change} disabled={busy === 'save'} />
                <div className="form-actions">
                  <button type="submit" className="btn btn-primary" disabled={!dirty || busy !== null}>
                    {busy === 'save' && <Spinner small />} {expense.status === 'REJECTED' ? 'Save as draft' : 'Save changes'}
                  </button>
                  {(dirty || editingRejected) && (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => {
                        setEdits(null);
                        setShowErrors(false);
                        setServerErrors({});
                        setEditingRejected(false);
                      }}
                    >
                      Discard
                    </button>
                  )}
                </div>
              </form>
            ) : (
              <ReadOnlyDetails expense={expense} />
            )}
          </Card>

          {isOwner && expense.status === 'DRAFT' && (
            <Card title="Submit for approval">
              <div className="stack">
                {prediction.eligible ? (
                  <Notice tone="success" title="Likely auto-approved">
                    It is within the {formatMoney(platform.config.autoApproveLimit, platform.config.defaultCurrency)} limit and
                    matches the receipt total Claude read, so the workflow can approve it without a manager.
                  </Notice>
                ) : (
                  <Notice title="A manager will review this expense">
                    <ul>
                      {prediction.reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  </Notice>
                )}
                {blockers.length > 0 && (
                  <ul className="subtle" aria-label="Why submitting is not possible yet">
                    {blockers.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                )}
                <div>
                  <button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={blockers.length > 0 || busy !== null}>
                    {busy === 'submit' && <Spinner small />} Submit for approval
                  </button>
                </div>
              </div>
            </Card>
          )}

          <Card title="History" description="Audit trail written by stream_processor from the DynamoDB stream.">
            <HistoryTimeline history={history} />
          </Card>
        </div>

        <div className="stack">
          <Card title="Receipt">
            <div className="stack">
              {expense.receipt && <ReceiptPreview expenseId={expense.id} receipt={expense.receipt} />}
              {!expense.receipt && !(isOwner && expense.status === 'DRAFT') && <p className="muted">No receipt was uploaded.</p>}
              {isOwner && expense.status === 'DRAFT' && (
                <ReceiptUploader
                  expenseId={expense.id}
                  expenseDate={expense.expenseDate}
                  replacing={expense.receipt !== null}
                  onUploadingChange={setUploading}
                  onUploaded={(note) => {
                    reload();
                    toasts.push({
                      tone: 'success',
                      title: 'Receipt uploaded to S3',
                      text: note ?? 'S3 notifies EventBridge, which starts the ReceiptPipeline state machine.',
                    });
                  }}
                />
              )}
            </div>
          </Card>
          <Card
            title={
              <span className="ai-header">
                <span className="ai-mark" aria-hidden="true">
                  AI
                </span>
                Claude&apos;s suggestions
              </span>
            }
            description="Read from the receipt by Claude on Amazon Bedrock. You decide what to apply."
          >
            <AiPanel expense={expense} canApply={editable} applying={busy === 'apply'} onApply={(list) => void applySuggestions(list)} />
          </Card>
          <UnderTheHood expense={expense} />
        </div>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this draft?"
        confirmLabel="Delete expense"
        danger
        busy={busy === 'delete'}
        onCancel={() => setConfirmDelete(false)}
        onConfirm={() => void remove()}
      >
        <p>
          &ldquo;{expense.title}&rdquo; and its receipt are deleted permanently (the DynamoDB item collection and the S3
          objects).
        </p>
      </ConfirmDialog>
    </div>
  );
}

function StatusNotice({ expense, isOwner, editingRejected }: { expense: Expense; isOwner: boolean; editingRejected: boolean }) {
  const approval = expense.approval;
  switch (expense.status) {
    case 'PENDING_APPROVAL':
      return (
        <Notice title={approval?.escalated ? 'Escalated: waiting for a decision' : 'Waiting for approval'}>
          {approval?.requestedAt
            ? `The ApprovalWorkflow is paused on a task token since ${formatDateTime(approval.requestedAt)} until a manager decides.`
            : 'The ApprovalWorkflow checks the auto-approval policy; if it does not apply, the expense waits for a manager.'}
          {isOwner && ' You can withdraw it to make changes.'}
        </Notice>
      );
    case 'APPROVED':
      return (
        <Notice tone="success" title={approval?.autoApproved ? 'Auto-approved by policy' : `Approved by ${approval?.approverEmail ?? 'a manager'}`}>
          {approval?.comment && <span>{approval.comment}</span>}
          <span className="subtle">Decided {formatDateTime(expense.decidedAt)}</span>
        </Notice>
      );
    case 'REJECTED':
      return (
        <Notice tone="danger" title={`Rejected${approval?.approverEmail ? ` by ${approval.approverEmail}` : ''}`}>
          {approval?.comment && <span>&ldquo;{approval.comment}&rdquo;</span>}
          {isOwner && (
            <span className="subtle">
              {editingRejected
                ? 'Saving turns it back into a draft (REJECTED → DRAFT); then submit it again.'
                : 'Use “Edit & resubmit” to fix it.'}
            </span>
          )}
        </Notice>
      );
    case 'DRAFT':
      return null;
  }
}

function ReadOnlyDetails({ expense }: { expense: Expense }) {
  return (
    <dl className="kv">
      <dt>Merchant</dt>
      <dd>{expense.merchant ?? '—'}</dd>
      <dt>Amount</dt>
      <dd className="num">{formatMoney(expense.amount, expense.currency)}</dd>
      <dt>Category</dt>
      <dd>{expense.category ? CATEGORY_LABELS[expense.category] : '—'}</dd>
      <dt>Date</dt>
      <dd>{formatCalendarDate(expense.expenseDate)}</dd>
      <dt>Description</dt>
      <dd>{expense.description ?? '—'}</dd>
      <dt>Owner</dt>
      <dd>
        {expense.ownerName} ({expense.ownerEmail})
      </dd>
      <dt>Submitted</dt>
      <dd>{formatDateTime(expense.submittedAt)}</dd>
    </dl>
  );
}

function UnderTheHood({ expense }: { expense: Expense }) {
  const { config, mode } = usePlatform();
  const links = consoleLinks(config);
  return (
    <Card title="Under the hood" description={mode === 'demo' ? 'Demo mode: these console links show where to look in a real deployment.' : undefined}>
      <dl className="kv">
        <dt>DynamoDB key</dt>
        <dd>
          <code>PK=EXPENSE#{expense.id}</code> <code>SK=META</code>
        </dd>
        <dt>Version</dt>
        <dd>
          {expense.version} <span className="subtle">(+1 on every write; PATCH sends it back as a condition)</span>
        </dd>
        <dt>Updated</dt>
        <dd>{formatDateTime(expense.updatedAt)}</dd>
        {expense.receipt && (
          <>
            <dt>Receipt object</dt>
            <dd>
              <a href={s3ObjectLink(config, config.console.receiptsBucket, expense.receipt.key)} target="_blank" rel="noreferrer">
                <code>{expense.receipt.key}</code>
              </a>
            </dd>
          </>
        )}
        <dt>Receipt pipeline</dt>
        <dd>
          <a href={links.receiptPipeline} target="_blank" rel="noreferrer">
            ReceiptPipeline state machine
          </a>
        </dd>
        <dt>Approval</dt>
        <dd>
          {expense.workflowExecutionArn ? (
            <a href={executionLink(config, expense.workflowExecutionArn)} target="_blank" rel="noreferrer">
              View workflow execution
            </a>
          ) : (
            <span className="muted">No workflow execution yet (it starts when the expense is submitted).</span>
          )}
        </dd>
      </dl>
    </Card>
  );
}
