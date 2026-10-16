/**
 * What the demo backend keeps instead of DynamoDB and S3, and how it survives a page reload.
 *
 * One `StoredExpense` plays the part of an item collection (`PK = EXPENSE#{id}`: the META item plus its
 * HISTORY items). The fields that the real API never returns - the Step Functions task token, the
 * pending upload key - are kept next to the API-shaped expense, just as the real table keeps
 * `approvalTaskToken` without ever exposing it.
 */
import type { Decision, Expense, HistoryEntry } from '../api/types';
import type { ReceiptContentType } from '../domain';
import { readJson, writeJson } from '../lib/storage';
import type { TemplateId } from './receipts';

/** A receipt the demo drew itself: it can be re-drawn instead of storing the image bytes. */
export interface SampleRef {
  templateId: TemplateId;
  date: string;
}

/** A decision accepted by `POST /approvals/{id}/decision` (SendTaskSuccess) but not recorded yet. */
export interface QueuedDecision {
  decision: Decision;
  approverId: string;
  approverEmail: string;
  comment: string | null;
}

export interface StoredExpense {
  expense: Expense;
  history: HistoryEntry[];
  /** `approvalTaskToken`: present while the ApprovalWorkflow waits for a manager. */
  taskToken: string | null;
  queuedDecision: QueuedDecision | null;
  /** `pendingReceiptKey` / `pendingReceiptContentType`: a presigned POST was issued for this key. */
  pendingReceipt: { key: string; contentType: ReceiptContentType } | null;
}

/** An object in the "receipts bucket". Uploaded bytes live in memory (and localStorage when they fit). */
export interface StoredFile {
  contentType: string;
  sample: SampleRef | null;
}

export interface StoredReport {
  month: string;
  key: string;
  size: number;
  lastModified: string;
  csv: string;
}

export interface DemoState {
  schema: typeof STATE_SCHEMA;
  expenses: Record<string, StoredExpense>;
  files: Record<string, StoredFile>;
  reports: StoredReport[];
}

const STATE_SCHEMA = 1;
export const STATE_KEY = 'expenseflow.demo.state.v1';
export const FILE_KEY_PREFIX = 'expenseflow.demo.file:';

export function emptyState(): DemoState {
  return { schema: STATE_SCHEMA, expenses: {}, files: {}, reports: [] };
}

/** The persisted state, or null when there is none (first visit) or it is from an older demo version. */
export function loadState(storage: Storage | null): DemoState | null {
  const state = readJson<DemoState>(storage, STATE_KEY);
  if (!state || state.schema !== STATE_SCHEMA || typeof state.expenses !== 'object') return null;
  return state;
}

export function saveState(storage: Storage | null, state: DemoState): void {
  writeJson(storage, STATE_KEY, state);
}
