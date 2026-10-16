/**
 * Turning Claude's receipt reading into form suggestions (human in the loop: the model proposes, the
 * employee decides with "Apply"). Only values that would pass the API's own validation are offered.
 */
import type { Expense, ExpensePatch, ExtractionResult } from '../api/types';
import { CATEGORY_LABELS, isCategory, isCurrency, LIMITS } from '../domain';
import { isValidIsoDate } from '../lib/dates';
import { formatCalendarDate, formatMoney } from '../lib/format';
import { hasAtMostTwoDecimals, FIELD_LABELS, type ExpenseField } from './validation';

export type SuggestableField = Extract<ExpenseField, 'merchant' | 'amount' | 'currency' | 'category' | 'expenseDate'>;

export interface Suggestion {
  field: SuggestableField;
  label: string;
  /** Value sent in the PATCH body. */
  value: string | number;
  display: string;
  currentDisplay: string;
  /** The expense already has this value. */
  matches: boolean;
}

function categoryLabel(value: string | null): string {
  return value && isCategory(value) ? CATEGORY_LABELS[value] : '—';
}

export function suggestionsFor(result: ExtractionResult, expense: Expense): Suggestion[] {
  const suggestions: Suggestion[] = [];
  const add = (field: SuggestableField, value: string | number, display: string, currentDisplay: string, current: unknown) =>
    suggestions.push({ field, label: FIELD_LABELS[field], value, display, currentDisplay, matches: current === value });

  const receiptCurrency = result.currency && isCurrency(result.currency) ? result.currency : null;
  const merchant = result.merchant?.trim();
  if (merchant && merchant.length <= LIMITS.merchantMax) {
    add('merchant', merchant, merchant, expense.merchant ?? '—', expense.merchant);
  }
  if (result.total !== null && result.total > 0 && result.total <= LIMITS.amountMax && hasAtMostTwoDecimals(result.total)) {
    const currency = receiptCurrency ?? expense.currency;
    add('amount', result.total, formatMoney(result.total, currency), formatMoney(expense.amount, expense.currency), expense.amount);
  }
  if (receiptCurrency) add('currency', receiptCurrency, receiptCurrency, expense.currency, expense.currency);
  if (result.category && isCategory(result.category)) {
    add('category', result.category, CATEGORY_LABELS[result.category], categoryLabel(expense.category), expense.category);
  }
  if (result.date && isValidIsoDate(result.date)) {
    add('expenseDate', result.date, formatCalendarDate(result.date), formatCalendarDate(expense.expenseDate), expense.expenseDate);
  }
  return suggestions;
}

/** PATCH body applying the given suggestions, guarded by the version the user was looking at. */
export function patchForSuggestions(suggestions: readonly Suggestion[], version: number): ExpensePatch {
  const patch: Record<string, unknown> = { version };
  for (const suggestion of suggestions) patch[suggestion.field] = suggestion.value;
  return patch;
}
