/**
 * Domain vocabulary shared by the UI, the client-side validation and the demo backend.
 * Mirrors SPEC §3/§5 exactly: the server validates the same rules, the client repeats them
 * only to give instant feedback (never trust the client for enforcement).
 */

export const EXPENSE_STATUSES = ['DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'REJECTED'] as const;
export type ExpenseStatus = (typeof EXPENSE_STATUSES)[number];

export const STATUS_LABELS: Record<ExpenseStatus, string> = {
  DRAFT: 'Draft',
  PENDING_APPROVAL: 'Pending approval',
  APPROVED: 'Approved',
  REJECTED: 'Rejected',
};

export const CATEGORIES = [
  'TRAVEL',
  'MEALS',
  'LODGING',
  'TRANSPORT',
  'SUPPLIES',
  'SOFTWARE',
  'TRAINING',
  'OTHER',
] as const;
export type Category = (typeof CATEGORIES)[number];

export const CATEGORY_LABELS: Record<Category, string> = {
  TRAVEL: 'Travel',
  MEALS: 'Meals',
  LODGING: 'Lodging',
  TRANSPORT: 'Transport',
  SUPPLIES: 'Supplies',
  SOFTWARE: 'Software',
  TRAINING: 'Training',
  OTHER: 'Other',
};

export const CURRENCIES = [
  'JPY',
  'USD',
  'EUR',
  'GBP',
  'CNY',
  'KRW',
  'TWD',
  'HKD',
  'SGD',
  'AUD',
  'CAD',
  'CHF',
  'THB',
  'INR',
  'MNT',
] as const;
export type Currency = (typeof CURRENCIES)[number];

export const EXTRACTION_STATUSES = ['PROCESSING', 'SUCCEEDED', 'FAILED', 'SKIPPED'] as const;
export type ExtractionStatus = (typeof EXTRACTION_STATUSES)[number];

/** Content types the receipt-upload endpoint will sign a POST policy for. */
export const RECEIPT_CONTENT_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'application/pdf'] as const;
export type ReceiptContentType = (typeof RECEIPT_CONTENT_TYPES)[number];

export const LIMITS = {
  titleMax: 120,
  merchantMax: 120,
  descriptionMax: 1000,
  amountMax: 100_000_000,
  amountDecimals: 2,
  minExpenseDate: '2000-01-01',
  commentMax: 500,
  pageSizeMax: 100,
} as const;

/** Cognito user pool groups (SPEC §1, Auth stack). */
export const GROUPS = { employees: 'employees', managers: 'managers', admins: 'admins' } as const;

export function isCategory(value: unknown): value is Category {
  return typeof value === 'string' && (CATEGORIES as readonly string[]).includes(value);
}

export function isCurrency(value: unknown): value is Currency {
  return typeof value === 'string' && (CURRENCIES as readonly string[]).includes(value);
}

export function isReceiptContentType(value: unknown): value is ReceiptContentType {
  return typeof value === 'string' && (RECEIPT_CONTENT_TYPES as readonly string[]).includes(value);
}
