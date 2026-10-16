import { Link } from 'react-router';
import type { Expense } from '../api/types';
import { CATEGORY_LABELS } from '../domain';
import { formatCalendarDate, formatMoney } from '../lib/format';
import { StatusBadge } from './ui';

function Summary({ expense, showOwner }: { expense: Expense; showOwner: boolean }) {
  const extraction = expense.extraction?.status;
  return (
    <>
      <div className="item-row__main">
        <div className="item-row__title">{expense.title}</div>
        <div className="item-row__meta">
          {showOwner && <span>{expense.ownerName}</span>}
          <span className={showOwner ? 'dot-sep' : undefined}>{expense.merchant ?? 'No merchant yet'}</span>
          <span className="dot-sep">{formatCalendarDate(expense.expenseDate)}</span>
          {expense.category && <span className="dot-sep">{CATEGORY_LABELS[expense.category]}</span>}
          {(expense.pendingUpload || extraction === 'PROCESSING') && <span className="dot-sep">Reading receipt…</span>}
          {extraction === 'SUCCEEDED' && <span className="dot-sep">Receipt read by AI</span>}
        </div>
      </div>
      <div className="item-row__side">
        <span className="item-row__amount">{formatMoney(expense.amount, expense.currency)}</span>
        <StatusBadge status={expense.status} />
      </div>
    </>
  );
}

/** One expense in a list: a link to its page, or a button (approvals open a drawer instead). */
export function ExpenseRow({
  expense,
  showOwner = false,
  onSelect,
}: {
  expense: Expense;
  showOwner?: boolean;
  onSelect?: (expense: Expense) => void;
}) {
  if (onSelect) {
    return (
      <button type="button" className="item-row" onClick={() => onSelect(expense)}>
        <Summary expense={expense} showOwner={showOwner} />
      </button>
    );
  }
  return (
    <Link to={`/expenses/${expense.id}`} className="item-row">
      <Summary expense={expense} showOwner={showOwner} />
    </Link>
  );
}
