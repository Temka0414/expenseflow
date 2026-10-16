import { Link, useSearchParams } from 'react-router';
import { ExpenseRow } from '../components/ExpenseRow';
import { Card, EmptyState, ErrorNotice, LoadingBlock, PageHeader, Spinner } from '../components/ui';
import { EXPENSE_STATUSES, STATUS_LABELS, type ExpenseStatus } from '../domain';
import { asExpenseDetail } from '../live/events';
import { useLiveRefresh } from '../live/LiveContext';
import { usePagedList } from '../lib/useResource';
import { useApi, useSession } from '../platform/context';

const FILTERS: ReadonlyArray<ExpenseStatus | null> = [null, ...EXPENSE_STATUSES];

function parseStatus(value: string | null): ExpenseStatus | null {
  return EXPENSE_STATUSES.find((status) => status === value) ?? null;
}

export default function ExpensesPage() {
  const api = useApi();
  const { viewer } = useSession();
  const [params, setParams] = useSearchParams();
  const status = parseStatus(params.get('status'));

  // GET /expenses queries GSI1 (OWNER#{me}, newest first); "Load more" follows DynamoDB's LastEvaluatedKey.
  const list = usePagedList(`expenses:${status ?? 'ALL'}`, (cursor, limit) => api.listExpenses({ status, cursor, limit }));
  useLiveRefresh((event) => asExpenseDetail(event)?.ownerId === viewer.userId, list.reload);

  return (
    <div className="page">
      <PageHeader
        title="My expenses"
        description="Newest first. Open an expense to edit it, add the receipt or submit it."
        actions={
          <Link to="/expenses/new" className="btn btn-primary">
            New expense
          </Link>
        }
      />
      <div className="tabs" role="group" aria-label="Filter by status">
        {FILTERS.map((filter) => (
          <button
            key={filter ?? 'ALL'}
            type="button"
            className="tab"
            aria-pressed={filter === status}
            onClick={() => setParams(filter ? { status: filter } : {})}
          >
            {filter ? STATUS_LABELS[filter] : 'All'}
          </button>
        ))}
      </div>
      <Card
        flush
        footer={
          list.hasMore && (
            <button type="button" className="btn btn-sm" onClick={list.loadMore} disabled={list.loadingMore}>
              {list.loadingMore && <Spinner small />} Load more
            </button>
          )
        }
      >
        {list.error && <ErrorNotice error={list.error} onRetry={list.reload} />}
        {list.loading && <LoadingBlock />}
        {!list.loading && !list.error && list.items.length === 0 && (
          <EmptyState
            title={status ? `No ${STATUS_LABELS[status].toLowerCase()} expenses` : 'No expenses yet'}
            action={
              <Link to="/expenses/new" className="btn btn-primary btn-sm">
                New expense
              </Link>
            }
          />
        )}
        {list.items.length > 0 && (
          <ul className="item-list">
            {list.items.map((expense) => (
              <li key={expense.id}>
                <ExpenseRow expense={expense} />
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
