import { toasts, type ToastInput } from '../components/toastStore';
import { asEscalationDetail, asExpenseDetail, asReportDetail, DETAIL_TYPES, type LiveEvent } from '../live/events';
import { useLiveEvent } from '../live/LiveContext';
import { formatMoney, formatMonth } from '../lib/format';
import { useSession } from '../platform/context';
import type { Viewer } from '../platform/platform';

/** Turns pushed EventBridge events into toasts. Renders nothing itself. */
export function LiveToasts() {
  const { viewer } = useSession();
  useLiveEvent((event) => {
    const toast = toastFor(event, viewer);
    if (toast) toasts.push(toast);
  });
  return null;
}

function toastFor(event: LiveEvent, viewer: Viewer): ToastInput | null {
  const expense = asExpenseDetail(event);
  if (expense) {
    // The person who clicked already sees the result on screen: only tell everyone else.
    if (expense.actorId === viewer.userId) return null;
    const link = { to: `/expenses/${expense.expenseId}`, label: 'Open expense' };
    const name = expense.title ?? 'Your expense';
    const amount = formatMoney(expense.amount, expense.currency);
    switch (event.detailType) {
      case DETAIL_TYPES.receiptProcessed:
        if (expense.extractionStatus === 'SUCCEEDED') {
          return { tone: 'success', title: 'Claude read your receipt', text: `Review the suggestions for "${name}".`, link };
        }
        if (expense.extractionStatus === 'FAILED') {
          return { tone: 'warning', title: 'Receipt reading failed', text: `Fill in "${name}" by hand.`, link };
        }
        return { title: 'Receipt stored', text: `AI extraction is switched off for this deployment ("${name}").`, link };
      case DETAIL_TYPES.approved:
        return {
          tone: 'success',
          title: expense.autoApproved ? 'Auto-approved by policy' : 'Expense approved',
          text: `${name} (${amount})${expense.actorEmail && !expense.autoApproved ? ` by ${expense.actorEmail}` : ''}`,
          link,
        };
      case DETAIL_TYPES.rejected:
        return { tone: 'warning', title: 'Expense rejected', text: `${name}: ${expense.comment ?? 'no comment'}`, link };
      case DETAIL_TYPES.submitted:
        return {
          title: 'New expense to review',
          text: `${expense.ownerName ?? expense.ownerEmail ?? 'Someone'} submitted "${name}" (${amount}).`,
          link: { to: '/approvals', label: 'Open approvals' },
        };
      case DETAIL_TYPES.withdrawn:
        return { title: 'Expense withdrawn', text: `"${name}" was withdrawn by its owner.` };
      default:
        return null;
    }
  }
  const escalation = asEscalationDetail(event);
  if (escalation) {
    return {
      tone: 'warning',
      title: 'Approval escalated',
      text: `"${escalation.title}" (${formatMoney(escalation.amount, escalation.currency)}) has waited too long for a decision.`,
      link: { to: '/approvals', label: 'Open approvals' },
    };
  }
  const report = asReportDetail(event);
  if (report) {
    return {
      tone: 'success',
      title: `Report for ${formatMonth(report.month)} is ready`,
      text: `${report.rowCount} approved expense${report.rowCount === 1 ? '' : 's'}.`,
      link: { to: '/reports', label: 'Open reports' },
    };
  }
  return null;
}
