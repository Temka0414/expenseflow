import { Link } from 'react-router';
import type { Expense, MonthlyTotal } from '../api/types';
import { ExpenseRow } from '../components/ExpenseRow';
import { Card, EmptyState, ErrorNotice, LoadingBlock, PageHeader, StatusBadge } from '../components/ui';
import { EXPENSE_STATUSES, type ExpenseStatus } from '../domain';
import { isExpenseEvent } from '../live/events';
import { useLiveRefresh } from '../live/LiveContext';
import { currentMonth } from '../lib/dates';
import { formatMoney, formatMonth } from '../lib/format';
import { useResource } from '../lib/useResource';
import { useApi, usePlatform, useSession } from '../platform/context';

/** Per-currency sums: totals are kept per month AND currency (`TOTAL#{yyyy-mm}#{currency}`), never converted. */
function sumByCurrency(rows: readonly MonthlyTotal[]): Array<{ currency: string; amount: number; count: number }> {
  const sums = new Map<string, { currency: string; amount: number; count: number }>();
  for (const row of rows) {
    const sum = sums.get(row.currency) ?? { currency: row.currency, amount: 0, count: 0 };
    sum.amount = Math.round((sum.amount + row.approvedAmount) * 100) / 100;
    sum.count += row.approvedCount;
    sums.set(row.currency, sum);
  }
  return [...sums.values()];
}

function MoneyTile({ label, rows, defaultCurrency }: { label: string; rows: readonly MonthlyTotal[]; defaultCurrency: string }) {
  const sums = sumByCurrency(rows);
  const main = sums.find((sum) => sum.currency === defaultCurrency) ?? { currency: defaultCurrency, amount: 0, count: 0 };
  const others = sums.filter((sum) => sum.currency !== defaultCurrency);
  const count = sums.reduce((total, sum) => total + sum.count, 0);
  return (
    <div className="tile">
      <span className="tile__label">{label}</span>
      <span className="tile__value">{formatMoney(main.amount, main.currency)}</span>
      <span className="tile__hint">
        {count} approved expense{count === 1 ? '' : 's'}
        {others.map((sum) => ` · + ${formatMoney(sum.amount, sum.currency)}`).join('')}
      </span>
    </div>
  );
}

function countStatuses(items: readonly Expense[]): Record<ExpenseStatus, number> {
  const counts = Object.fromEntries(EXPENSE_STATUSES.map((status) => [status, 0])) as Record<ExpenseStatus, number>;
  for (const item of items) counts[item.status] += 1;
  return counts;
}

function MonthlyTable({ mine, org }: { mine: readonly MonthlyTotal[]; org: readonly MonthlyTotal[] | null }) {
  const months = [...new Set([...mine, ...(org ?? [])].map((row) => row.month))].sort().reverse();
  if (months.length === 0) return <EmptyState title="Nothing approved yet this year" />;
  const cell = (rows: readonly MonthlyTotal[], month: string) => {
    const sums = sumByCurrency(rows.filter((row) => row.month === month));
    return sums.length === 0 ? '—' : sums.map((sum) => `${formatMoney(sum.amount, sum.currency)} (${sum.count})`).join(' · ');
  };
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Month</th>
            <th className="right">Mine</th>
            {org && <th className="right">Whole organisation</th>}
          </tr>
        </thead>
        <tbody>
          {months.map((month) => (
            <tr key={month}>
              <td>{formatMonth(month)}</td>
              <td className="right num">{cell(mine, month)}</td>
              {org && <td className="right num">{cell(org, month)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function DashboardPage() {
  const api = useApi();
  const { config } = usePlatform();
  const { viewer, roles } = useSession();
  const month = currentMonth();
  const year = Number(month.slice(0, 4));

  const stats = useResource(`stats:${year}`, () => api.stats(year));
  const expenses = useResource('expenses:latest', () => api.listExpenses({ limit: 100 }));
  const queue = useResource(roles.canApprove ? 'approvals:latest' : null, () => api.listApprovals({ limit: 100 }));

  useLiveRefresh(isExpenseEvent, () => {
    stats.reload();
    expenses.reload();
    queue.reload();
  });

  const items = expenses.data?.items ?? [];
  const counts = countStatuses(items);
  const firstName = viewer.name.split(/\s+/)[0] || viewer.name;

  return (
    <div className="page">
      <PageHeader
        eyebrow={formatMonth(month)}
        title={`Welcome back, ${firstName}`}
        description="Your expenses at a glance. Everything on this page refreshes by itself when something changes."
        actions={
          <Link to="/expenses/new" className="btn btn-primary">
            New expense
          </Link>
        }
      />

      {stats.error && <ErrorNotice error={stats.error} onRetry={stats.reload} title="Could not load your totals" />}
      <div className="tiles">
        <MoneyTile
          label="Approved this month"
          rows={stats.data?.mine.filter((row) => row.month === month) ?? []}
          defaultCurrency={config.defaultCurrency}
        />
        <MoneyTile label={`Approved in ${year}`} rows={stats.data?.mine ?? []} defaultCurrency={config.defaultCurrency} />
        <Link to="/expenses?status=PENDING_APPROVAL" className="tile">
          <span className="tile__label">Waiting for approval</span>
          <span className="tile__value">{expenses.data ? counts.PENDING_APPROVAL : '…'}</span>
          <span className="tile__hint">Submitted, not decided yet</span>
        </Link>
        <Link to="/expenses?status=DRAFT" className="tile">
          <span className="tile__label">Drafts</span>
          <span className="tile__value">{expenses.data ? counts.DRAFT : '…'}</span>
          <span className="tile__hint">Not submitted yet</span>
        </Link>
        {roles.canApprove && (
          <Link to="/approvals" className="tile">
            <span className="tile__label">Your approval queue</span>
            <span className="tile__value">
              {queue.data ? `${queue.data.items.length}${queue.data.nextCursor ? '+' : ''}` : '…'}
            </span>
            <span className="tile__hint">Expenses from your team</span>
          </Link>
        )}
      </div>

      <div className="grid-2">
        <Card
          title="Recent expenses"
          actions={
            <Link to="/expenses" className="btn btn-sm btn-ghost">
              View all
            </Link>
          }
          flush
        >
          {expenses.error && <ErrorNotice error={expenses.error} onRetry={expenses.reload} />}
          {expenses.loading && <LoadingBlock />}
          {expenses.data && items.length === 0 && (
            <EmptyState
              title="No expenses yet"
              action={
                <Link to="/expenses/new" className="btn btn-primary btn-sm">
                  Create your first expense
                </Link>
              }
            >
              Create one, upload the receipt, and let Claude fill in the details.
            </EmptyState>
          )}
          {items.length > 0 && (
            <>
              <div className="row card__body">
                {EXPENSE_STATUSES.map((status) => (
                  <span key={status} className="row">
                    <StatusBadge status={status} /> <span className="num">{counts[status]}</span>
                  </span>
                ))}
              </div>
              <ul className="item-list">
                {items.slice(0, 6).map((expense) => (
                  <li key={expense.id}>
                    <ExpenseRow expense={expense} />
                  </li>
                ))}
              </ul>
            </>
          )}
        </Card>

        <div className="stack">
          <Card title="How approval works">
            <div className="stack-sm">
              <p>
                Expenses in <strong>{config.defaultCurrency}</strong> up to{' '}
                <strong>{formatMoney(config.autoApproveLimit, config.defaultCurrency)}</strong> whose receipt total Claude
                verified are <strong>approved automatically</strong> by the ApprovalWorkflow state machine.
              </p>
              <p className="muted">
                Everything else waits for a manager: Step Functions pauses with a task token until someone decides.
              </p>
              <Link to="/learn">See what happens under the hood →</Link>
            </div>
          </Card>
          <Card title={`Approved per month, ${year}`} flush>
            {stats.loading ? <LoadingBlock /> : stats.data && <MonthlyTable mine={stats.data.mine} org={stats.data.org} />}
          </Card>
        </div>
      </div>
    </div>
  );
}
