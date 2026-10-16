/**
 * Hints for the approver: how well the claimed expense matches what Claude read on the receipt.
 * Purely advisory - the human approver decides; the same total check drives auto-approval (SPEC §8.2).
 */
import type { Expense } from '../api/types';
import { formatCalendarDate, formatMoney } from '../lib/format';
import { totalTolerance } from './autoApproval';

export interface VerificationHint {
  ok: boolean;
  text: string;
}

const normalise = (value: string) => value.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

export function verificationHints(expense: Expense): VerificationHint[] {
  const extraction = expense.extraction;
  if (!expense.receipt) return [{ ok: false, text: 'No receipt was uploaded.' }];
  if (!extraction || extraction.status !== 'SUCCEEDED' || !extraction.result) {
    return [{ ok: false, text: 'There is no AI reading of the receipt to compare with.' }];
  }
  const result = extraction.result;
  const hints: VerificationHint[] = [];
  if (!result.isReceipt) hints.push({ ok: false, text: 'Claude says the file does not look like a receipt.' });

  if (result.total === null || expense.amount === null) {
    hints.push({ ok: false, text: 'No receipt total to compare with the claimed amount.' });
  } else if (result.currency && result.currency !== expense.currency) {
    hints.push({ ok: false, text: `The receipt is in ${result.currency}, the claim in ${expense.currency}.` });
  } else if (Math.abs(result.total - expense.amount) <= totalTolerance(expense.amount)) {
    hints.push({ ok: true, text: `Receipt total ${formatMoney(result.total, expense.currency)} matches the claimed amount.` });
  } else {
    hints.push({
      ok: false,
      text: `Receipt total ${formatMoney(result.total, expense.currency)} differs from the claimed ${formatMoney(expense.amount, expense.currency)}.`,
    });
  }

  if (result.merchant && expense.merchant) {
    const same = normalise(result.merchant).includes(normalise(expense.merchant)) || normalise(expense.merchant).includes(normalise(result.merchant));
    hints.push(same ? { ok: true, text: 'Merchant matches the receipt.' } : { ok: false, text: `The receipt says "${result.merchant}".` });
  }
  if (result.date && expense.expenseDate) {
    hints.push(
      result.date === expense.expenseDate
        ? { ok: true, text: 'Date matches the receipt.' }
        : { ok: false, text: `The receipt is dated ${formatCalendarDate(result.date)}.` },
    );
  }
  if (result.confidence === 'low') hints.push({ ok: false, text: 'Claude reported low confidence in its reading.' });
  return hints;
}
