/**
 * Starting data for the demo: a few expenses in every state, so each page has something to show and
 * the interesting moments (applying AI suggestions, approving, a rejection to fix) are one click away.
 * Dates are relative to "now", so the dashboard and the "previous month" report always have data.
 */
import type { Expense, HistoryEntry } from '../api/types';
import type { ExpenseStatus } from '../domain';
import { isoDate, previousMonth } from '../lib/dates';
import { DEMO_CONFIG, DEMO_MODEL } from './config';
import { ulid } from './ids';
import { actorOf, newExpense, receiptEntry, setApproval, statusEntry, SYSTEM_ACTOR } from './model';
import { buildSample, simulatedExtraction, type TemplateId } from './receipts';
import { emptyState, type DemoState, type StoredExpense } from './state';
import { DEMO_USERS, type DemoRoleId } from './users';

interface SeedSpec {
  owner: DemoRoleId;
  template: TemplateId;
  title: string;
  description?: string;
  /** Expense date: days before today, or a day of the previous month. */
  date: { daysAgo: number } | { previousMonthDay: number };
  status: ExpenseStatus;
  /** Leave the AI-readable fields empty so "Apply suggestions" has something to do. */
  partial?: boolean;
  decision?: { by: DemoRoleId | 'auto'; comment?: string };
}

const MINUTE = 60_000;

const SEEDS: SeedSpec[] = [
  {
    owner: 'employee',
    template: 'books',
    title: 'AWS certification study guide',
    description: 'Preparing for the Solutions Architect – Associate exam.',
    date: { daysAgo: 0 },
    status: 'DRAFT',
    partial: true,
  },
  {
    owner: 'employee',
    template: 'taxi',
    title: 'Taxi from Haneda to the client office',
    date: { daysAgo: 1 },
    status: 'PENDING_APPROVAL',
  },
  {
    owner: 'employee',
    template: 'hotel',
    title: 'Hotel for the Sendai customer workshop',
    date: { daysAgo: 6 },
    status: 'REJECTED',
    decision: { by: 'manager', comment: 'Please upload the itemised hotel invoice instead of the card slip.' },
  },
  {
    owner: 'employee',
    template: 'ramen',
    title: 'Team lunch with the new hire',
    date: { daysAgo: 9 },
    status: 'APPROVED',
    decision: { by: 'auto' },
  },
  {
    owner: 'employee',
    template: 'shinkansen',
    title: 'Shinkansen to the Osaka office',
    description: 'Round trip for the quarterly planning meeting.',
    date: { previousMonthDay: 18 },
    status: 'APPROVED',
    decision: { by: 'manager', comment: 'Thanks, approved.' },
  },
  {
    owner: 'manager',
    template: 'electronics',
    title: 'USB-C hub and HDMI cable for the meeting room',
    date: { daysAgo: 2 },
    status: 'PENDING_APPROVAL',
  },
  {
    owner: 'admin',
    template: 'coffee',
    title: 'Coffee with the AWS account team',
    date: { previousMonthDay: 9 },
    status: 'APPROVED',
    decision: { by: 'auto' },
  },
];

function expenseDateFor(spec: SeedSpec, now: Date): string {
  if ('daysAgo' in spec.date) return isoDate(new Date(now.getFullYear(), now.getMonth(), now.getDate() - spec.date.daysAgo));
  return `${previousMonth(now)}-${String(spec.date.previousMonthDay).padStart(2, '0')}`;
}

/** Morning of the expense date (local time), but never in the future. */
function startOf(date: string, now: Date): number {
  const [year, month, day] = date.split('-').map(Number);
  return Math.min(new Date(year, month - 1, day, 9, 12).getTime(), now.getTime() - 3 * 60 * MINUTE);
}

function seedExpense(spec: SeedSpec, now: Date, random: () => number): StoredExpense {
  const owner = DEMO_USERS[spec.owner];
  const date = expenseDateFor(spec, now);
  const sample = buildSample(spec.template, date);
  const start = startOf(date, now);
  const iso = (offsetMinutes: number) => new Date(start + offsetMinutes * MINUTE).toISOString();
  const id = ulid(start, random);

  const expense: Expense = newExpense(
    owner,
    spec.partial
      ? { title: spec.title, expenseDate: date, description: spec.description }
      : {
          title: spec.title,
          merchant: sample.merchant,
          amount: sample.total,
          currency: 'JPY',
          category: sample.category,
          expenseDate: date,
          description: spec.description,
        },
    id,
    iso(0),
    DEMO_CONFIG.defaultCurrency,
  );
  const history: HistoryEntry[] = [statusEntry(iso(0), null, 'DRAFT', actorOf(owner))];

  const receiptKey = `receipts/${owner.userId}/${id}/${ulid(start + MINUTE, random)}.png`;
  expense.receipt = { key: receiptKey, contentType: 'image/png', uploadedAt: iso(1) };
  expense.extraction = {
    status: 'SUCCEEDED',
    result: simulatedExtraction(sample, { model: DEMO_MODEL, confidence: 'high' }),
    error: null,
    model: DEMO_MODEL,
    extractedAt: iso(1.2),
  };
  expense.updatedAt = iso(1.2);
  expense.version = 2;
  history.push(receiptEntry(iso(1.2), 'SUCCEEDED'));

  let taskToken: string | null = null;
  if (spec.status !== 'DRAFT') {
    expense.status = 'PENDING_APPROVAL';
    expense.submittedAt = iso(4);
    expense.updatedAt = iso(4);
    expense.version += 1;
    expense.workflowExecutionArn = `arn:aws:states:${DEMO_CONFIG.region}:123456789012:execution:ApprovalWorkflowDEMO0000:${id.toLowerCase()}`;
    history.push(statusEntry(iso(4), 'DRAFT', 'PENDING_APPROVAL', actorOf(owner)));
  }
  if (spec.status === 'PENDING_APPROVAL') {
    taskToken = `demo-task-token-${id}`;
    setApproval(expense, { requestedAt: iso(4.1), escalated: false });
  }
  if (spec.decision && (spec.status === 'APPROVED' || spec.status === 'REJECTED')) {
    const decidedAt = spec.decision.by === 'auto' ? iso(4.1) : iso(95);
    const approver = spec.decision.by === 'auto' ? null : DEMO_USERS[spec.decision.by];
    const comment =
      spec.decision.comment ??
      `Auto-approved by policy: ≤ ${DEMO_CONFIG.autoApproveLimit} ${DEMO_CONFIG.defaultCurrency} and receipt total verified by AI`;
    expense.status = spec.status;
    expense.decidedAt = decidedAt;
    expense.updatedAt = decidedAt;
    expense.version += 1;
    setApproval(expense, {
      approverId: approver?.userId ?? null,
      approverEmail: approver?.email ?? 'auto-approval',
      comment,
      autoApproved: approver === null,
      escalated: false,
      requestedAt: approver ? iso(4.1) : null,
    });
    history.push(statusEntry(decidedAt, 'PENDING_APPROVAL', spec.status, approver ? actorOf(approver) : SYSTEM_ACTOR, comment));
  }

  return { expense, history, taskToken, queuedDecision: null, pendingReceipt: null };
}

export function seedState(now: Date, random: () => number): DemoState {
  const state = emptyState();
  for (const spec of SEEDS) {
    const stored = seedExpense(spec, now, random);
    state.expenses[stored.expense.id] = stored;
    const receipt = stored.expense.receipt;
    if (receipt) {
      state.files[receipt.key] = {
        contentType: receipt.contentType,
        sample: { templateId: spec.template, date: stored.expense.expenseDate ?? isoDate(now) },
      };
    }
  }
  return state;
}
