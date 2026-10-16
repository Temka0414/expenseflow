import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router';
import { toApiError, type ApiError } from '../api/errors';
import { ExpenseForm } from '../components/ExpenseForm';
import { toasts } from '../components/toastStore';
import { Card, ErrorNotice, PageHeader } from '../components/ui';
import { createInputFor, emptyValues, validateForm, type FormErrors, type FormValues } from '../expenses/form';
import { serverFieldErrors, withoutField } from '../expenses/serverErrors';
import type { ExpenseField } from '../expenses/validation';
import { todayIso } from '../lib/dates';
import { useApi, usePlatform } from '../platform/context';

export default function NewExpensePage() {
  const api = useApi();
  const { config } = usePlatform();
  const navigate = useNavigate();
  const [values, setValues] = useState<FormValues>(() => emptyValues(config.defaultCurrency, todayIso()));
  const [showErrors, setShowErrors] = useState(false);
  const [serverErrors, setServerErrors] = useState<FormErrors>({});
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [saving, setSaving] = useState(false);

  // Format checks only: blank optional fields are fine for a draft (they are required at submit time).
  const clientErrors = validateForm(values, todayIso());
  const errors = { ...serverErrors, ...(showErrors ? clientErrors : {}) };

  function change(field: ExpenseField, value: string) {
    setValues((current) => ({ ...current, [field]: value }));
    setServerErrors((current) => withoutField(current, field));
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    setShowErrors(true);
    if (Object.keys(clientErrors).length > 0) return;
    setSaving(true);
    setFailure(null);
    try {
      const expense = await api.createExpense(createInputFor(values));
      toasts.push({ tone: 'success', title: 'Draft created', text: 'Now add the receipt: Claude will suggest the missing fields.' });
      void navigate(`/expenses/${expense.id}`);
    } catch (error) {
      const apiError = toApiError(error);
      setServerErrors(serverFieldErrors(apiError));
      setFailure(apiError);
      setSaving(false);
    }
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow={<Link to="/expenses">← My expenses</Link>}
        title="New expense"
        description="Only the title is needed to start a draft. Upload the receipt on the next screen and Claude will suggest the merchant, amount, date and category."
      />
      <Card>
        <form className="stack" onSubmit={(event) => void create(event)} noValidate>
          {failure && <ErrorNotice error={failure} title="Could not create the expense" />}
          <ExpenseForm values={values} errors={errors} onChange={change} disabled={saving} />
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? 'Creating…' : 'Create draft'}
            </button>
            <Link to="/expenses" className="btn btn-ghost">
              Cancel
            </Link>
          </div>
        </form>
      </Card>
    </div>
  );
}
