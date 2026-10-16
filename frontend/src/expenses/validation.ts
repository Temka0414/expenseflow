/**
 * Expense field rules (SPEC §5), written once and used twice:
 *  - by the form, for instant feedback before any request is sent;
 *  - by the demo backend, to answer with the same 422 shape the real API (pydantic) produces.
 * The real backend re-validates everything: client-side checks are a convenience, never a control.
 *
 * Issue `type`s are pydantic v2 error types, because that is what the API puts in `detail[].type`.
 */
import { isCategory, isCurrency, LIMITS } from '../domain';
import { addDays, isValidIsoDate } from '../lib/dates';
import { formatNumber } from '../lib/format';

export const EXPENSE_FIELDS = ['title', 'merchant', 'amount', 'currency', 'category', 'expenseDate', 'description'] as const;
export type ExpenseField = (typeof EXPENSE_FIELDS)[number];

export const FIELD_LABELS: Record<ExpenseField, string> = {
  title: 'Title',
  merchant: 'Merchant',
  amount: 'Amount',
  currency: 'Currency',
  category: 'Category',
  expenseDate: 'Date',
  description: 'Description',
};

export interface ValidationIssue {
  field: ExpenseField;
  /** pydantic error type, e.g. `string_too_long`. */
  type: string;
  message: string;
}

export interface ValidateOptions {
  /** PATCH semantics: absent fields are left alone; POST requires `title`. */
  partial: boolean;
  /** Local calendar date `YYYY-MM-DD`; expense dates may be at most one day after it (time zones). */
  today: string;
}

/** Removable optional fields accept `null` or `""` (= delete the attribute). */
const isBlank = (value: unknown) => value === null || value === '';

/** Validate a JSON request body for POST /expenses (partial=false) or PATCH /expenses/{id} (partial=true). */
export function validateExpenseInput(input: Record<string, unknown>, options: ValidateOptions): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const add = (field: ExpenseField, type: string, message: string) => issues.push({ field, type, message });

  if ('title' in input || !options.partial) validateTitle(input.title, add);
  validateText('merchant', input.merchant, LIMITS.merchantMax, add);
  validateText('description', input.description, LIMITS.descriptionMax, add);
  if ('amount' in input && !isBlank(input.amount)) validateAmount(input.amount, add);
  if ('currency' in input && !isCurrency(input.currency)) add('currency', 'literal_error', 'Choose a supported currency');
  if ('category' in input && !isBlank(input.category) && !isCategory(input.category)) {
    add('category', 'literal_error', 'Choose one of the categories');
  }
  if ('expenseDate' in input && !isBlank(input.expenseDate)) validateDate(input.expenseDate, options.today, add);
  return issues;
}

type AddIssue = (field: ExpenseField, type: string, message: string) => void;

function validateTitle(value: unknown, add: AddIssue): void {
  if (value === undefined) return add('title', 'missing', 'Title is required');
  if (typeof value !== 'string') return add('title', 'string_type', 'Title must be text');
  const length = value.trim().length;
  if (length === 0) return add('title', 'string_too_short', 'Title is required');
  if (length > LIMITS.titleMax) add('title', 'string_too_long', `Title must be at most ${LIMITS.titleMax} characters`);
}

function validateText(field: 'merchant' | 'description', value: unknown, max: number, add: AddIssue): void {
  if (value === undefined || isBlank(value)) return;
  if (typeof value !== 'string') return add(field, 'string_type', `${FIELD_LABELS[field]} must be text`);
  if (value.trim().length > max) add(field, 'string_too_long', `${FIELD_LABELS[field]} must be at most ${max} characters`);
}

function validateAmount(value: unknown, add: AddIssue): void {
  if (typeof value !== 'number' || !Number.isFinite(value)) return add('amount', 'decimal_parsing', 'Amount must be a number');
  if (value <= 0) return add('amount', 'greater_than', 'Amount must be greater than 0');
  if (value > LIMITS.amountMax) {
    return add('amount', 'less_than_equal', `Amount must be at most ${formatNumber(LIMITS.amountMax)}`);
  }
  if (!hasAtMostTwoDecimals(value)) add('amount', 'decimal_max_places', 'Amount can have at most 2 decimal places');
}

/** Binary floating point cannot represent 19.99 exactly, so compare with a tolerance. */
export function hasAtMostTwoDecimals(value: number): boolean {
  const cents = value * 10 ** LIMITS.amountDecimals;
  return Math.abs(cents - Math.round(cents)) < 1e-6;
}

function validateDate(value: unknown, today: string, add: AddIssue): void {
  if (typeof value !== 'string' || !isValidIsoDate(value)) {
    return add('expenseDate', 'date_from_datetime_parsing', 'Enter a valid date (YYYY-MM-DD)');
  }
  if (value < LIMITS.minExpenseDate) return add('expenseDate', 'greater_than_equal', 'Date must be in 2000 or later');
  if (value > addDays(today, 1)) add('expenseDate', 'value_error', 'Date cannot be in the future');
}

/** Approval comments (POST /approvals/{id}/decision): required to reject, at most 500 characters. */
export function commentIssue(decision: 'APPROVED' | 'REJECTED', comment: string): string | null {
  const length = comment.trim().length;
  if (decision === 'REJECTED' && length === 0) return 'Please tell the employee why the expense is rejected.';
  if (length > LIMITS.commentMax) return `Comments can be at most ${LIMITS.commentMax} characters.`;
  return null;
}
