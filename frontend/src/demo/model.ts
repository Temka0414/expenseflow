/**
 * Item-level rules of the single table (SPEC §3), shared by the demo seed data and the demo backend.
 */
import type { ApprovalInfo, Expense, ExpenseInput, HistoryEntry } from '../api/types';
import type { ExpenseStatus, ExtractionStatus } from '../domain';
import type { DemoUser } from './users';

/** `lastActorId` / `lastActorEmail` of workflow updates (SPEC §3). */
export const SYSTEM_ACTOR = { id: 'system', email: 'system' } as const;

export interface Actor {
  id: string;
  email: string;
}

export function actorOf(user: DemoUser): Actor {
  return { id: user.userId, email: user.email };
}

export function newExpense(owner: DemoUser, input: ExpenseInput, id: string, at: string, defaultCurrency: string): Expense {
  return {
    id,
    ownerId: owner.userId,
    ownerEmail: owner.email,
    ownerName: owner.name,
    title: input.title,
    merchant: input.merchant ?? null,
    amount: input.amount ?? null,
    currency: input.currency ?? defaultCurrency,
    category: input.category ?? null,
    expenseDate: input.expenseDate ?? null,
    description: input.description ?? null,
    status: 'DRAFT',
    createdAt: at,
    updatedAt: at,
    submittedAt: null,
    decidedAt: null,
    version: 1,
    receipt: null,
    pendingUpload: false,
    extraction: null,
    approval: null,
    workflowExecutionArn: null,
  };
}

/**
 * "Whoever sets updatedAt also sets GSI2SK and increments version" (SPEC §3). The version bump is what
 * makes optimistic concurrency work: a client holding version N can no longer overwrite version N+1.
 */
export function touch(expense: Expense, at: string): void {
  expense.updatedAt = at;
  expense.version += 1;
}

/** Merge into the API's `approval` object, creating it on first use. */
export function setApproval(expense: Expense, patch: Partial<ApprovalInfo>): void {
  const current: ApprovalInfo = expense.approval ?? {
    approverId: null,
    approverEmail: null,
    comment: null,
    autoApproved: null,
    escalated: null,
    requestedAt: null,
  };
  expense.approval = { ...current, ...patch };
}

/** The HISTORY item stream_processor writes when the status changes (`HISTORY#{at}#STATUS#{to}`). */
export function statusEntry(
  at: string,
  from: ExpenseStatus | null,
  to: ExpenseStatus,
  actor: Actor,
  comment: string | null = null,
): HistoryEntry {
  return { at, kind: 'STATUS', from, to, actorEmail: actor.email, comment, extractionStatus: null };
}

/** The HISTORY item written when receipt extraction finishes (`HISTORY#{extractedAt}#RECEIPT`). */
export function receiptEntry(at: string, extractionStatus: ExtractionStatus): HistoryEntry {
  return { at, kind: 'RECEIPT', from: null, to: null, actorEmail: null, comment: null, extractionStatus };
}

/**
 * `detail` of an `expenseflow.expenses` event (SPEC §4): every key present, null when unknown. The
 * idempotency key lets consumers (the notifier's Powertools idempotency) drop EventBridge's
 * at-least-once duplicates.
 */
export function expenseEventDetail(
  expense: Expense,
  previousStatus: ExpenseStatus | null,
  actor: Actor,
  kind: 'status' | 'receipt',
): Record<string, unknown> {
  const extractionStatus = expense.extraction?.status ?? null;
  const idempotencyKey =
    kind === 'receipt'
      ? `${expense.id}:RECEIPT-${extractionStatus}:${expense.extraction?.extractedAt ?? expense.updatedAt}`
      : `${expense.id}:${expense.status}:${expense.updatedAt}`;
  return {
    expenseId: expense.id,
    ownerId: expense.ownerId,
    ownerEmail: expense.ownerEmail,
    ownerName: expense.ownerName,
    title: expense.title,
    amount: expense.amount,
    currency: expense.currency,
    status: expense.status,
    previousStatus,
    actorId: actor.id,
    actorEmail: actor.email,
    comment: expense.approval?.comment ?? null,
    autoApproved: expense.approval?.autoApproved ?? null,
    extractionStatus,
    occurredAt: kind === 'receipt' ? (expense.extraction?.extractedAt ?? expense.updatedAt) : expense.updatedAt,
    idempotencyKey,
  };
}
