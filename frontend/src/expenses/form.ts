/**
 * The expense form model: string values (what inputs hold), conversion to API bodies, and the merge
 * that lets the server copy change underneath an open form without losing what the user typed.
 */
import type { Expense, ExpenseInput, ExpensePatch } from '../api/types';
import type { Category, Currency } from '../domain';
import { EXPENSE_FIELDS, validateExpenseInput, type ExpenseField } from './validation';

export type FormValues = Record<ExpenseField, string>;
export type FormErrors = Partial<Record<ExpenseField, string>>;

export function emptyValues(defaultCurrency: string, today: string): FormValues {
  return {
    title: '',
    merchant: '',
    amount: '',
    currency: defaultCurrency,
    category: '',
    expenseDate: today,
    description: '',
  };
}

export function valuesFromExpense(expense: Expense): FormValues {
  return {
    title: expense.title,
    merchant: expense.merchant ?? '',
    amount: expense.amount === null ? '' : String(expense.amount),
    currency: expense.currency,
    category: expense.category ?? '',
    expenseDate: expense.expenseDate ?? '',
    description: expense.description ?? '',
  };
}

/** Accepts `1280`, `1,280`, `19.99`; returns NaN for anything else so validation can flag it. */
export function parseAmount(text: string): number | null {
  const cleaned = text.replace(/[,\s]/g, '');
  if (cleaned === '') return null;
  return /^\d+(\.\d+)?$/.test(cleaned) ? Number(cleaned) : Number.NaN;
}

/** The JSON value a field is sent as: blank optional fields become null (= remove the attribute). */
function wireValue(field: ExpenseField, value: string): string | number | null {
  const trimmed = value.trim();
  if (field === 'amount') return parseAmount(trimmed);
  if (field === 'title' || field === 'currency') return trimmed;
  return trimmed === '' ? null : trimmed;
}

function normalized(field: ExpenseField, value: string): string {
  const wire = wireValue(field, value);
  return wire === null ? '' : String(wire);
}

export function validateForm(values: FormValues, today: string): FormErrors {
  const body = Object.fromEntries(EXPENSE_FIELDS.map((field) => [field, wireValue(field, values[field])]));
  const errors: FormErrors = {};
  for (const issue of validateExpenseInput(body, { partial: false, today })) errors[issue.field] ??= issue.message;
  return errors;
}

export function changedFields(values: FormValues, base: FormValues): ExpenseField[] {
  return EXPENSE_FIELDS.filter((field) => normalized(field, values[field]) !== normalized(field, base[field]));
}

/**
 * PATCH body with only the changed fields plus `version`. The server applies it with a DynamoDB
 * condition `version = :v`: if someone else saved in between, it answers 409 instead of silently
 * overwriting their change (optimistic concurrency).
 */
export function patchFor(values: FormValues, base: FormValues, version: number): ExpensePatch {
  const patch: Record<string, unknown> = { version };
  for (const field of changedFields(values, base)) patch[field] = wireValue(field, values[field]);
  return patch;
}

export function createInputFor(values: FormValues): ExpenseInput {
  const input: ExpenseInput = { title: values.title.trim(), currency: values.currency as Currency };
  const merchant = wireValue('merchant', values.merchant);
  const amount = wireValue('amount', values.amount);
  const category = wireValue('category', values.category);
  const expenseDate = wireValue('expenseDate', values.expenseDate);
  const description = wireValue('description', values.description);
  if (typeof merchant === 'string') input.merchant = merchant;
  if (typeof amount === 'number') input.amount = amount;
  if (typeof category === 'string') input.category = category as Category;
  if (typeof expenseDate === 'string') input.expenseDate = expenseDate;
  if (typeof description === 'string') input.description = description;
  return input;
}

/**
 * Three-way merge when the server copy changes while the form is open (a live event, an AI suggestion
 * applied, a save): fields the user has not touched follow the server; fields they edited keep their
 * local text.
 */
export function rebaseValues(local: FormValues, oldBase: FormValues, newBase: FormValues): FormValues {
  const merged = { ...local };
  for (const field of EXPENSE_FIELDS) {
    if (normalized(field, local[field]) === normalized(field, oldBase[field])) merged[field] = newBase[field];
  }
  return merged;
}
