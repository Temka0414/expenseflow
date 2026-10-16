import { describe, expect, it } from 'vitest';
import type { Expense } from '../api/types';
import { formatRelative } from '../lib/format';
import { changedFields, patchFor, rebaseValues, valuesFromExpense, type FormValues } from './form';
import { verificationHints } from './verification';

const base: FormValues = {
  title: 'Taxi',
  merchant: '',
  amount: '',
  currency: 'JPY',
  category: '',
  expenseDate: '2026-10-13',
  description: '',
};

function expense(overrides: Partial<Expense> = {}): Expense {
  return {
    id: '01JEXAMPLE0000000000000000',
    ownerId: 'u1',
    ownerEmail: 'a@example.com',
    ownerName: 'A',
    title: 'Taxi',
    merchant: 'Sakura Taxi Co., Ltd.',
    amount: 6850,
    currency: 'JPY',
    category: 'TRANSPORT',
    expenseDate: '2026-10-13',
    description: null,
    status: 'PENDING_APPROVAL',
    createdAt: '2026-10-13T01:00:00.000Z',
    updatedAt: '2026-10-13T01:05:00.000Z',
    submittedAt: '2026-10-13T01:05:00.000Z',
    decidedAt: null,
    version: 3,
    receipt: { key: 'receipts/u1/x/y.png', contentType: 'image/png', uploadedAt: '2026-10-13T01:01:00.000Z' },
    pendingUpload: false,
    extraction: {
      status: 'SUCCEEDED',
      result: {
        isReceipt: true,
        merchant: 'Sakura Taxi Co., Ltd.',
        date: '2026-10-13',
        currency: 'JPY',
        total: 6850,
        subtotal: 6228,
        tax: 622,
        paymentMethod: 'Credit card',
        category: 'TRANSPORT',
        confidence: 'high',
        lineItems: [],
        model: 'jp.anthropic.claude-opus-5-5',
        requestedModel: 'jp.anthropic.claude-opus-5-5',
        fallbackUsed: false,
        inputTokens: 1500,
        outputTokens: 300,
      },
      error: null,
      model: 'jp.anthropic.claude-opus-5-5',
      extractedAt: '2026-10-13T01:01:10.000Z',
    },
    approval: null,
    workflowExecutionArn: null,
    ...overrides,
  };
}

describe('form rebase (edits survive server-side changes)', () => {
  it('lets untouched fields follow the server while keeping what the user typed', () => {
    const local = { ...base, title: 'Taxi to the client' };
    const server = { ...base, merchant: 'Sakura Taxi Co., Ltd.', amount: '6850' };
    expect(rebaseValues(local, base, server)).toEqual({ ...server, title: 'Taxi to the client' });
  });

  it('sends only changed fields plus the version, and null to remove an optional field', () => {
    const server = { ...base, description: 'old note' };
    const local = { ...server, amount: '1,280', description: '  ' };
    expect(changedFields(local, server)).toEqual(['amount', 'description']);
    expect(patchFor(local, server, 7)).toEqual({ version: 7, amount: 1280, description: null });
  });

  it('round-trips an API expense into form values', () => {
    expect(valuesFromExpense(expense()).amount).toBe('6850');
    expect(valuesFromExpense(expense({ description: null })).description).toBe('');
  });
});

describe('verificationHints', () => {
  it('confirms a claim that matches what Claude read', () => {
    expect(verificationHints(expense()).every((hint) => hint.ok)).toBe(true);
  });

  it('flags a claimed amount outside the 1% tolerance', () => {
    const hints = verificationHints(expense({ amount: 7000 }));
    expect(hints.find((hint) => hint.text.includes('differs'))?.ok).toBe(false);
  });

  it('asks for caution without a receipt or an AI reading', () => {
    expect(verificationHints(expense({ receipt: null }))).toEqual([{ ok: false, text: 'No receipt was uploaded.' }]);
    expect(verificationHints(expense({ extraction: null }))[0].ok).toBe(false);
  });
});

describe('formatRelative', () => {
  it('never describes a past event as being in the future when the clock lags', () => {
    const now = Date.parse('2026-10-14T00:00:00Z');
    expect(formatRelative('2026-10-14T00:00:11Z', now)).toBe('just now');
    expect(formatRelative('2026-10-13T23:57:00Z', now)).toBe('3 minutes ago');
  });
});
