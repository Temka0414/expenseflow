/**
 * Server -> browser messages pushed by `realtime_push` over the WebSocket API (SPEC §6):
 * `{"type": "event", "source", "detailType", "time", "detail": {...}}` - an EventBridge event, re-wrapped.
 */
import type { ExpenseStatus, ExtractionStatus } from '../domain';

export interface LiveEvent {
  type: 'event';
  source: string;
  detailType: string;
  time: string;
  detail: Record<string, unknown>;
}

export const DETAIL_TYPES = {
  submitted: 'Expense Submitted',
  approved: 'Expense Approved',
  rejected: 'Expense Rejected',
  withdrawn: 'Expense Withdrawn',
  receiptProcessed: 'Receipt Processed',
  escalated: 'Approval Escalated',
  reportGenerated: 'Report Generated',
} as const;

/** `detail` of every `expenseflow.expenses` event (all keys present, null when unknown). */
export interface ExpenseEventDetail {
  expenseId: string;
  ownerId: string | null;
  ownerEmail: string | null;
  ownerName: string | null;
  title: string | null;
  amount: number | null;
  currency: string | null;
  status: ExpenseStatus | null;
  previousStatus: ExpenseStatus | null;
  actorId: string | null;
  actorEmail: string | null;
  comment: string | null;
  autoApproved: boolean | null;
  extractionStatus: ExtractionStatus | null;
  occurredAt: string | null;
  idempotencyKey: string;
}

export interface EscalationDetail {
  expenseId: string;
  ownerId: string;
  title: string;
  amount: number;
  currency: string;
  requestedAt: string;
  idempotencyKey: string;
}

export interface ReportDetail {
  month: string;
  key: string;
  rowCount: number;
  totals: Record<string, number>;
  requestedBy: string;
  idempotencyKey: string;
}

/** Parse one WebSocket frame; anything that is not an event envelope (e.g. a pong) is ignored. */
export function parseLiveMessage(data: unknown): LiveEvent | null {
  if (typeof data !== 'string') return null;
  let message: unknown;
  try {
    message = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof message !== 'object' || message === null) return null;
  const { type, source, detailType, time, detail } = message as Record<string, unknown>;
  if (type !== 'event' || typeof source !== 'string' || typeof detailType !== 'string') return null;
  return {
    type: 'event',
    source,
    detailType,
    time: typeof time === 'string' ? time : new Date().toISOString(),
    detail: typeof detail === 'object' && detail !== null ? (detail as Record<string, unknown>) : {},
  };
}

/** The expense an event is about, if any (expense and escalation events carry `expenseId`). */
export function expenseIdOf(event: LiveEvent): string | null {
  const id = event.detail.expenseId;
  return typeof id === 'string' ? id : null;
}

export function isExpenseEvent(event: LiveEvent): boolean {
  return expenseIdOf(event) !== null;
}

export function asExpenseDetail(event: LiveEvent): ExpenseEventDetail | null {
  return event.source === 'expenseflow.expenses' ? (event.detail as unknown as ExpenseEventDetail) : null;
}

export function asEscalationDetail(event: LiveEvent): EscalationDetail | null {
  return event.detailType === DETAIL_TYPES.escalated ? (event.detail as unknown as EscalationDetail) : null;
}

export function asReportDetail(event: LiveEvent): ReportDetail | null {
  return event.detailType === DETAIL_TYPES.reportGenerated ? (event.detail as unknown as ReportDetail) : null;
}
