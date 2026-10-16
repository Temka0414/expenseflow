import type { ApiError } from '../api/errors';
import type { FormErrors } from './form';
import { EXPENSE_FIELDS, type ExpenseField } from './validation';

/** 422 issues from the API keyed by form field, so they show next to the input they are about. */
export function serverFieldErrors(error: ApiError): FormErrors {
  const errors: FormErrors = {};
  for (const issue of error.issues) {
    const field = EXPENSE_FIELDS.find((name) => name === issue.field);
    if (field) errors[field] ??= issue.message;
  }
  return errors;
}

/** Drop a field's server error once the user edits that field. */
export function withoutField(errors: FormErrors, field: ExpenseField): FormErrors {
  if (!(field in errors)) return errors;
  const next = { ...errors };
  delete next[field];
  return next;
}
