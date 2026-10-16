import { useId, type ReactNode } from 'react';
import { CATEGORIES, CATEGORY_LABELS, CURRENCIES, LIMITS } from '../domain';
import type { FormErrors, FormValues } from '../expenses/form';
import type { ExpenseField } from '../expenses/validation';
import { addDays, todayIso } from '../lib/dates';

interface FieldProps {
  id: string;
  label: string;
  required?: boolean;
  hint?: string;
  error?: string;
  wide?: boolean;
  children: (describedBy: string | undefined) => ReactNode;
}

function Field({ id, label, required = false, hint, error, wide = false, children }: FieldProps) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [errorId, hintId].filter(Boolean).join(' ') || undefined;
  return (
    <div className={wide ? 'field span-2' : 'field'}>
      <label className="field__label" htmlFor={id}>
        {label} {required ? <span className="field__required">(required to submit)</span> : null}
      </label>
      {children(describedBy)}
      {error && (
        <span className="field__error" id={errorId}>
          {error}
        </span>
      )}
      {hint && !error && (
        <span className="field__hint" id={hintId}>
          {hint}
        </span>
      )}
    </div>
  );
}

/**
 * The editable expense fields. Validation messages come from `expenses/validation.ts` (the same rules
 * the API enforces) merged with any 422 field issues the server returned.
 */
export function ExpenseForm({
  values,
  errors,
  onChange,
  disabled = false,
}: {
  values: FormValues;
  errors: FormErrors;
  onChange: (field: ExpenseField, value: string) => void;
  disabled?: boolean;
}) {
  const prefix = useId();
  const id = (field: ExpenseField) => `${prefix}-${field}`;
  const today = todayIso();

  return (
    <fieldset className="form-grid form-fieldset" disabled={disabled}>
      <Field id={id('title')} label="Title" hint={`What was it for? Up to ${LIMITS.titleMax} characters.`} error={errors.title} wide>
        {(describedBy) => (
          <input
            id={id('title')}
            className="input"
            value={values.title}
            onChange={(event) => onChange('title', event.target.value)}
            aria-invalid={errors.title ? true : undefined}
            aria-describedby={describedBy}
            placeholder="e.g. Taxi to the client office"
            autoComplete="off"
          />
        )}
      </Field>
      <Field id={id('merchant')} label="Merchant" required error={errors.merchant}>
        {(describedBy) => (
          <input
            id={id('merchant')}
            className="input"
            value={values.merchant}
            onChange={(event) => onChange('merchant', event.target.value)}
            aria-invalid={errors.merchant ? true : undefined}
            aria-describedby={describedBy}
            autoComplete="off"
          />
        )}
      </Field>
      <Field id={id('amount')} label="Amount" required error={errors.amount ?? errors.currency}>
        {(describedBy) => (
          <div className="input-group">
            <input
              id={id('amount')}
              className="input num"
              inputMode="decimal"
              value={values.amount}
              onChange={(event) => onChange('amount', event.target.value)}
              aria-invalid={errors.amount ? true : undefined}
              aria-describedby={describedBy}
              placeholder="0"
              autoComplete="off"
            />
            <select
              className="select"
              aria-label="Currency"
              value={values.currency}
              onChange={(event) => onChange('currency', event.target.value)}
              aria-invalid={errors.currency ? true : undefined}
            >
              {CURRENCIES.map((currency) => (
                <option key={currency} value={currency}>
                  {currency}
                </option>
              ))}
            </select>
          </div>
        )}
      </Field>
      <Field id={id('category')} label="Category" required error={errors.category}>
        {(describedBy) => (
          <select
            id={id('category')}
            className="select"
            value={values.category}
            onChange={(event) => onChange('category', event.target.value)}
            aria-invalid={errors.category ? true : undefined}
            aria-describedby={describedBy}
          >
            <option value="">Choose a category…</option>
            {CATEGORIES.map((category) => (
              <option key={category} value={category}>
                {CATEGORY_LABELS[category]}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field id={id('expenseDate')} label="Date" required error={errors.expenseDate}>
        {(describedBy) => (
          <input
            id={id('expenseDate')}
            className="input"
            type="date"
            min={LIMITS.minExpenseDate}
            max={addDays(today, 1)}
            value={values.expenseDate}
            onChange={(event) => onChange('expenseDate', event.target.value)}
            aria-invalid={errors.expenseDate ? true : undefined}
            aria-describedby={describedBy}
          />
        )}
      </Field>
      <Field
        id={id('description')}
        label="Description"
        hint={`Optional notes for your approver, up to ${LIMITS.descriptionMax} characters.`}
        error={errors.description}
        wide
      >
        {(describedBy) => (
          <textarea
            id={id('description')}
            className="textarea"
            value={values.description}
            onChange={(event) => onChange('description', event.target.value)}
            aria-invalid={errors.description ? true : undefined}
            aria-describedby={describedBy}
          />
        )}
      </Field>
    </fieldset>
  );
}
