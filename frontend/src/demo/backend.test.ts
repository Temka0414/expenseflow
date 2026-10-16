import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApi, type Api } from '../api/client';
import type { ApiError } from '../api/errors';
import type { Expense, ExpenseInput } from '../api/types';
import type { LiveEvent } from '../live/events';
import { previousMonth, todayIso } from '../lib/dates';
import { DemoBackend } from './backend';
import { DEMO_CONFIG } from './config';
import type { TemplateId } from './receipts';
import { DEMO_USERS, type DemoUser } from './users';

/** Long enough for every simulated hop (stream, workflow, extraction, decision, report). */
const SETTLE_MS = 30_000;

let user: DemoUser;
let backend: DemoBackend;
let api: Api;

beforeEach(() => {
  vi.useFakeTimers();
  user = DEMO_USERS.employee;
  backend = new DemoBackend({ config: DEMO_CONFIG, storage: null, currentUser: () => user, timing: { latency: [0, 0], upload: 0 } });
  api = createApi(backend.transport);
});

afterEach(() => {
  vi.useRealTimers();
});

const settle = () => vi.advanceTimersByTimeAsync(SETTLE_MS);

function as(role: keyof typeof DEMO_USERS): void {
  user = DEMO_USERS[role];
}

/** Collect the live events one user would receive over "their WebSocket". */
function listen(role: keyof typeof DEMO_USERS): LiveEvent[] {
  const events: LiveEvent[] = [];
  backend.liveSource(DEMO_USERS[role]).connect({ onEvent: (event) => events.push(event), onStatus: () => undefined });
  return events;
}

async function expectApiError(promise: Promise<unknown>, status: number): Promise<ApiError> {
  const error = (await promise.then(
    () => null,
    (e: unknown) => e,
  )) as ApiError | null;
  expect(error?.status).toBe(status);
  return error as ApiError;
}

function draft(overrides: Partial<ExpenseInput> = {}): Promise<Expense> {
  return api.createExpense({
    title: 'Team lunch',
    merchant: 'Hinode Coffee Shinjuku',
    amount: 1700,
    currency: 'JPY',
    category: 'MEALS',
    expenseDate: todayIso(),
    ...overrides,
  });
}

/** Presigned POST + "S3" upload of a receipt the demo can read exactly. */
async function uploadSample(expenseId: string, templateId: TemplateId): Promise<void> {
  const file = new Blob(['fake image bytes'], { type: 'image/png' });
  backend.registerSample(file, { templateId, date: todayIso() });
  const presigned = await api.requestReceiptUpload(expenseId, { contentType: 'image/png', size: file.size });
  expect(presigned.key).toMatch(new RegExp(`^receipts/${user.userId}/${expenseId}/[0-9A-Z]{26}\\.png$`));
  expect(presigned.fields['Content-Type']).toBe('image/png');
  await backend.uploadObject(presigned, file, () => undefined);
  await settle();
}

describe('demo backend: expenses', () => {
  it('creates drafts and answers invalid input with the API 422 shape', async () => {
    const error = await expectApiError(api.createExpense({ title: ' ', amount: 0, currency: 'JPY' }), 422);
    expect(error.issues.map((issue) => issue.field).sort()).toEqual(['amount', 'title']);

    const created = await draft();
    expect(created).toMatchObject({ status: 'DRAFT', version: 1, ownerEmail: user.email, receipt: null, pendingUpload: false });
    const detail = await api.getExpense(created.id);
    expect(detail.history).toEqual([expect.objectContaining({ kind: 'STATUS', from: null, to: 'DRAFT' })]);
  });

  it('rejects stale writes with 409 (optimistic concurrency on version)', async () => {
    const created = await draft();
    const saved = await api.updateExpense(created.id, { title: 'Team lunch (3 people)', version: created.version });
    expect(saved.version).toBe(created.version + 1);
    const conflict = await expectApiError(api.updateExpense(created.id, { title: 'Stale edit', version: created.version }), 409);
    expect(conflict.isConflict).toBe(true);
  });

  it('pages through my expenses newest first with an opaque cursor', async () => {
    as('manager');
    const ids = [];
    for (const title of ['One', 'Two', 'Three']) {
      ids.push((await draft({ title })).id);
      await vi.advanceTimersByTimeAsync(5); // distinct createdAt: GSI1 sorts by `{createdAt}#{id}`
    }
    const all: string[] = [];
    let cursor: string | null = null;
    do {
      const page = await api.listExpenses({ limit: 2, cursor });
      expect(page.items.length).toBeLessThanOrEqual(2);
      all.push(...page.items.map((e) => e.id));
      cursor = page.nextCursor;
    } while (cursor);
    expect(new Set(all).size).toBe(all.length);
    expect(all).toEqual(expect.arrayContaining(ids));
    expect(all.slice(0, 3)).toEqual([...ids].reverse());
    await expectApiError(api.listExpenses({ limit: 0 }), 422);
  });
});

describe('demo backend: receipt pipeline and approval workflow', () => {
  it('reads the receipt, then auto-approves a small JPY expense whose AI total matches', async () => {
    const events = listen('employee');
    const expense = await draft();
    await uploadSample(expense.id, 'coffee');

    const read = (await api.getExpense(expense.id)).expense;
    expect(read.extraction).toMatchObject({ status: 'SUCCEEDED', result: { total: 1700, merchant: 'Hinode Coffee Shinjuku' } });
    expect(read.receipt?.contentType).toBe('image/png');

    expect((await api.submitExpense(expense.id)).status).toBe('PENDING_APPROVAL');
    await settle();

    const approved = (await api.getExpense(expense.id)).expense;
    expect(approved.status).toBe('APPROVED');
    expect(approved.approval).toMatchObject({ autoApproved: true, approverEmail: 'auto-approval' });
    expect(approved.workflowExecutionArn).toMatch(/^arn:aws:states:ap-northeast-1:\d{12}:execution:/);
    expect(events.map((e) => e.detailType)).toEqual(['Receipt Processed', 'Expense Submitted', 'Expense Approved']);
    expect(events[2].detail).toMatchObject({ expenseId: expense.id, status: 'APPROVED', previousStatus: 'PENDING_APPROVAL', autoApproved: true });
  });

  it('sends the expense to a manager when the receipt total does not match the amount', async () => {
    const expense = await draft({ amount: 1500 });
    await uploadSample(expense.id, 'coffee');
    await api.submitExpense(expense.id);
    await settle();
    const pending = (await api.getExpense(expense.id)).expense;
    expect(pending.status).toBe('PENDING_APPROVAL');
    expect(pending.approval?.requestedAt).not.toBeNull();
  });

  it('waits for a manager above the limit: not ready -> approve, with the right permissions', async () => {
    const managerEvents = listen('manager');
    const employeeEvents = listen('employee');
    const expense = await draft({ title: 'Airport taxi', amount: 6850, category: 'TRANSPORT' });
    await api.submitExpense(expense.id);

    as('manager');
    const early = await expectApiError(api.decide(expense.id, 'APPROVED', null), 409);
    expect(early.message).toBe('approval request not ready yet');
    await settle();
    expect((await api.listApprovals()).items.map((e) => e.id)).toContain(expense.id);

    as('employee');
    await expectApiError(api.listApprovals(), 403);
    await expectApiError(api.decide(expense.id, 'APPROVED', null), 403);

    as('manager');
    expect(await api.decide(expense.id, 'APPROVED', 'Looks good')).toEqual({ accepted: true });
    await settle();
    const approved = (await api.getExpense(expense.id)).expense;
    expect(approved).toMatchObject({ status: 'APPROVED', approval: { approverEmail: DEMO_USERS.manager.email, autoApproved: false } });
    expect((await api.listApprovals()).items.map((e) => e.id)).not.toContain(expense.id);

    expect(managerEvents.map((e) => e.detailType)).toEqual(['Expense Submitted']);
    expect(employeeEvents.map((e) => e.detailType)).toEqual(['Expense Submitted', 'Expense Approved']);
  });

  it('never lets managers decide on their own expenses', async () => {
    as('manager');
    const own = await draft({ amount: 9000 });
    await api.submitExpense(own.id);
    await settle();
    await expectApiError(api.decide(own.id, 'APPROVED', null), 403);
    expect((await api.listApprovals()).items.map((e) => e.id)).not.toContain(own.id);
  });

  it('requires a comment to reject, and editing a rejected expense re-opens it as a draft', async () => {
    const expense = await draft({ amount: 12000 });
    await api.submitExpense(expense.id);
    await settle();

    as('admin');
    await expectApiError(api.decide(expense.id, 'REJECTED', '   '), 400);
    await api.decide(expense.id, 'REJECTED', 'Missing the itemised invoice');
    await settle();

    as('employee');
    const rejected = (await api.getExpense(expense.id)).expense;
    expect(rejected).toMatchObject({ status: 'REJECTED', approval: { comment: 'Missing the itemised invoice' } });
    const reopened = await api.updateExpense(expense.id, { amount: 11800, version: rejected.version });
    expect(reopened).toMatchObject({ status: 'DRAFT', approval: null, decidedAt: null, amount: 11800 });
    const { history } = await api.getExpense(expense.id);
    expect(history.map((h) => `${h.from}->${h.to}`)).toEqual([
      'null->DRAFT',
      'DRAFT->PENDING_APPROVAL',
      'PENDING_APPROVAL->REJECTED',
      'REJECTED->DRAFT',
    ]);
  });

  it('withdrawing stops the workflow: a decision still in flight is never applied', async () => {
    const expense = await draft({ amount: 8000 });
    await api.submitExpense(expense.id);
    await settle();
    as('manager');
    await api.decide(expense.id, 'APPROVED', null);
    as('employee');
    expect((await api.withdrawExpense(expense.id)).status).toBe('DRAFT');
    await settle();
    expect((await api.getExpense(expense.id)).expense.status).toBe('DRAFT');
  });

  it('refuses to submit incomplete drafts and to delete submitted expenses', async () => {
    const incomplete = await api.createExpense({ title: 'Just a title', currency: 'JPY' });
    const error = await expectApiError(api.submitExpense(incomplete.id), 400);
    expect(error.message).toContain('merchant');

    const complete = await draft({ amount: 9000 });
    await api.submitExpense(complete.id);
    await expectApiError(api.deleteExpense(complete.id), 409);
    await api.deleteExpense(incomplete.id);
    await expectApiError(api.getExpense(incomplete.id), 404);
  });
});

describe('demo backend: stats and reports', () => {
  it('reports org totals only to managers and admins', async () => {
    const year = new Date().getFullYear();
    expect((await api.stats(year)).org).toBeNull();
    as('admin');
    const stats = await api.stats(year);
    expect(stats.org).not.toBeNull();
    expect(stats.mine.every((total) => total.currency === 'JPY' && total.approvedCount > 0)).toBe(true);
  });

  it('generates a UTF-8 CSV with BOM for admins and tells only admins about it', async () => {
    const adminEvents = listen('admin');
    const employeeEvents = listen('employee');
    const month = previousMonth();
    await expectApiError(api.generateReport(month), 403);

    as('admin');
    await expectApiError(api.generateReport('2026-13'), 422);
    expect(await api.generateReport(month)).toEqual({ accepted: true, month });
    await settle();

    const [report] = await api.listReports();
    expect(report).toMatchObject({ month, key: `reports/${month}.csv` });
    const { url } = await api.getReportUrl(month);
    const csv = decodeURIComponent(url.slice(url.indexOf(',') + 1));
    expect(csv.startsWith('﻿expense_id,owner_name,owner_email,title,merchant,category,expense_date,amount')).toBe(true);
    expect(csv.trim().split('\r\n').length).toBeGreaterThan(1);

    expect(adminEvents.map((e) => e.detailType)).toEqual(['Report Generated']);
    expect(employeeEvents).toEqual([]);
  });
});
