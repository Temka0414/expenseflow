/**
 * Wire types of the HTTP API (SPEC §5). JSON `null` means "unknown / not set": the backend stores
 * optional attributes as *absent* in DynamoDB and renders them as null in responses.
 */
import type { Category, Currency, ExpenseStatus, ExtractionStatus } from '../domain';

export interface ReceiptInfo {
  key: string;
  contentType: string;
  uploadedAt: string;
}

export interface ExtractedLineItem {
  description: string | null;
  quantity: number | null;
  unitPrice: number | null;
  amount: number | null;
}

/** What Claude read from the receipt (tool-use output of extract_receipt, SPEC §7.4). */
export interface ExtractionResult {
  isReceipt: boolean;
  merchant: string | null;
  date: string | null;
  currency: string | null;
  total: number | null;
  subtotal: number | null;
  tax: number | null;
  paymentMethod: string | null;
  category: Category | null;
  confidence: 'high' | 'medium' | 'low';
  lineItems: ExtractedLineItem[];
  model: string;
  requestedModel: string;
  fallbackUsed: boolean;
  inputTokens: number;
  outputTokens: number;
}

export interface Extraction {
  status: ExtractionStatus;
  result: ExtractionResult | null;
  error: string | null;
  model: string | null;
  extractedAt: string | null;
}

export interface ApprovalInfo {
  approverId: string | null;
  approverEmail: string | null;
  comment: string | null;
  autoApproved: boolean | null;
  escalated: boolean | null;
  requestedAt: string | null;
}

export interface Expense {
  id: string;
  ownerId: string;
  ownerEmail: string;
  ownerName: string;
  title: string;
  merchant: string | null;
  amount: number | null;
  currency: string;
  category: Category | null;
  expenseDate: string | null;
  description: string | null;
  status: ExpenseStatus;
  createdAt: string;
  updatedAt: string;
  submittedAt: string | null;
  decidedAt: string | null;
  /** Optimistic-concurrency token: send it back on PATCH, the server rejects stale writes with 409. */
  version: number;
  receipt: ReceiptInfo | null;
  /** A presigned POST was issued but S3 has not reported the object yet. */
  pendingUpload: boolean;
  extraction: Extraction | null;
  approval: ApprovalInfo | null;
  workflowExecutionArn: string | null;
}

export interface HistoryEntry {
  at: string;
  kind: 'STATUS' | 'RECEIPT';
  from: ExpenseStatus | null;
  to: ExpenseStatus | null;
  actorEmail: string | null;
  comment: string | null;
  extractionStatus: ExtractionStatus | null;
}

export interface ExpenseDetail {
  expense: Expense;
  history: HistoryEntry[];
}

export interface Page<T> {
  items: T[];
  /** Opaque cursor (urlsafe-base64 of DynamoDB's LastEvaluatedKey); null on the last page. */
  nextCursor: string | null;
}

export interface Me {
  userId: string;
  email: string;
  name: string;
  groups: string[];
  isManager: boolean;
  isAdmin: boolean;
}

export interface MonthlyTotal {
  month: string;
  currency: string;
  approvedAmount: number;
  approvedCount: number;
}

export interface Stats {
  year: number;
  mine: MonthlyTotal[];
  /** Organisation-wide totals; only returned to managers and admins. */
  org: MonthlyTotal[] | null;
}

export interface ReportInfo {
  month: string;
  key: string;
  size: number;
  lastModified: string;
}

/** S3 presigned POST policy: the browser uploads straight to S3, no Lambda in the data path. */
export interface PresignedPost {
  url: string;
  fields: Record<string, string>;
  key: string;
  expiresIn: number;
  maxBytes: number;
}

export interface PresignedGet {
  url: string;
  expiresIn: number;
}

export interface Accepted {
  accepted: true;
}

export interface ReportAccepted extends Accepted {
  month: string;
}

export interface ExpenseInput {
  title: string;
  merchant?: string | null;
  amount?: number | null;
  currency?: Currency;
  category?: Category | null;
  expenseDate?: string | null;
  description?: string | null;
}

/** PATCH body: absent = unchanged, null = remove the optional attribute. */
export type ExpensePatch = Partial<ExpenseInput> & { version?: number };

export type Decision = 'APPROVED' | 'REJECTED';
