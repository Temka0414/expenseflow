/**
 * What an expense needs before it can be submitted (POST /expenses/{id}/submit validates the same
 * required fields server-side, then performs the conditional DRAFT -> PENDING_APPROVAL write).
 */
import type { Expense } from '../api/types';
import { FIELD_LABELS, type ExpenseField } from './validation';

export const REQUIRED_FOR_SUBMIT = ['title', 'merchant', 'amount', 'currency', 'category', 'expenseDate'] as const satisfies readonly ExpenseField[];

export function missingForSubmit(expense: Pick<Expense, (typeof REQUIRED_FOR_SUBMIT)[number]>): ExpenseField[] {
  return REQUIRED_FOR_SUBMIT.filter((field) => {
    const value = expense[field];
    if (field === 'amount') return typeof value !== 'number' || value <= 0;
    return value === null || value === '';
  });
}

export interface SubmitContext {
  isOwner: boolean;
  hasUnsavedChanges: boolean;
  uploading: boolean;
}

/** Human-readable reasons the Submit button is disabled (empty = can submit). */
export function submitBlockers(expense: Expense, context: SubmitContext): string[] {
  if (!context.isOwner) return ['Only the owner can submit this expense.'];
  if (expense.status !== 'DRAFT') return ['Only drafts can be submitted.'];
  const reasons: string[] = [];
  const missing = missingForSubmit(expense);
  if (missing.length > 0) reasons.push(`Fill in: ${missing.map((field) => FIELD_LABELS[field].toLowerCase()).join(', ')}.`);
  if (context.hasUnsavedChanges) reasons.push('Save your changes first.');
  if (context.uploading) reasons.push('Wait for the receipt upload to finish.');
  return reasons;
}
