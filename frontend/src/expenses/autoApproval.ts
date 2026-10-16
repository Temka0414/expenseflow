/**
 * The auto-approval policy, mirroring the ApprovalWorkflow's `CanAutoApprove` Choice state (SPEC §8.2):
 *
 *   currency == default currency
 *   AND amount <= autoApproveLimit            (SSM parameter /expenseflow/{stage}/auto-approve-limit)
 *   AND extractionStatus == SUCCEEDED
 *   AND |receipt total - amount| <= max(1, amount * 1%)
 *
 * The decision is made server-side by Step Functions; the UI uses this copy only to *predict* the
 * outcome before submitting, and the demo backend uses it to play the workflow's part.
 */
import type { Expense } from '../api/types';
import { formatMoney } from '../lib/format';

export interface AutoApprovalPolicy {
  defaultCurrency: string;
  limit: number;
}

export interface AutoApprovalCheck {
  eligible: boolean;
  /** Why the expense would go to a manager instead (empty when eligible). */
  reasons: string[];
  /** What Claude read as the receipt total, when available. */
  extractedTotal: number | null;
}

export function totalTolerance(amount: number): number {
  return Math.max(1, amount * 0.01);
}

export function checkAutoApproval(
  expense: Pick<Expense, 'amount' | 'currency' | 'extraction'>,
  policy: AutoApprovalPolicy,
): AutoApprovalCheck {
  const reasons: string[] = [];
  const { amount, currency } = expense;
  const extraction = expense.extraction;
  const extractedTotal = extraction?.status === 'SUCCEEDED' ? (extraction.result?.total ?? null) : null;

  if (currency !== policy.defaultCurrency) {
    reasons.push(`Only ${policy.defaultCurrency} expenses can be auto-approved (this one is ${currency}).`);
  }
  if (amount === null) {
    reasons.push('The amount is missing.');
  } else if (amount > policy.limit) {
    reasons.push(`${formatMoney(amount, currency)} is above the auto-approval limit of ${formatMoney(policy.limit, policy.defaultCurrency)}.`);
  }
  if (!extraction || extraction.status !== 'SUCCEEDED') {
    reasons.push(
      extraction?.status === 'PROCESSING'
        ? 'Claude is still reading the receipt; auto-approval needs the AI-verified total.'
        : 'There is no AI-verified receipt total.',
    );
  } else if (extractedTotal === null) {
    reasons.push('Claude could not find a total on the receipt.');
  } else if (amount !== null && Math.abs(extractedTotal - amount) > totalTolerance(amount)) {
    reasons.push(
      `The receipt total (${formatMoney(extractedTotal, currency)}) does not match the amount (${formatMoney(amount, currency)}).`,
    );
  }
  return { eligible: reasons.length === 0, reasons, extractedTotal };
}
