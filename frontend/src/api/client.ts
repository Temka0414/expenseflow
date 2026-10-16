/**
 * Typed client for the HTTP API (SPEC §5).
 *
 * The client does not know *how* bytes travel: a `Transport` does. In AWS mode it is `fetch` against API
 * Gateway with a Cognito ID token; in demo mode it is an in-memory backend. Every call goes through
 * `call()`, which times it, records it in the request inspector and turns error responses into ApiError.
 */
import type {
  Accepted,
  Decision,
  Expense,
  ExpenseDetail,
  ExpenseInput,
  ExpensePatch,
  Me,
  Page,
  PresignedGet,
  PresignedPost,
  ReportAccepted,
  ReportInfo,
  Stats,
} from './types';
import type { ExpenseStatus, ReceiptContentType } from '../domain';
import { networkError, parseApiError } from './errors';
import { requestLog } from './inspector';

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE';

export interface TransportRequest {
  method: HttpMethod;
  /** Path + query string relative to the API base URL, e.g. `/expenses?limit=25`. */
  path: string;
  body?: unknown;
}

export interface TransportResponse {
  status: number;
  /** Parsed JSON body (undefined for 204 No Content). */
  body: unknown;
  requestId: string | null;
}

/** Sends one request; resolves for every HTTP status, rejects only when no response arrived. */
export type Transport = (request: TransportRequest) => Promise<TransportResponse>;

type QueryValue = string | number | null | undefined;

export function withQuery(path: string, query: Record<string, QueryValue>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== '') params.set(key, String(value));
  }
  const search = params.toString();
  return search ? `${path}?${search}` : path;
}

/** Path segment encoding; ids are ULIDs (already URL-safe), months are YYYY-MM. */
const seg = encodeURIComponent;

export function createApi(transport: Transport) {
  async function call<T>(method: HttpMethod, path: string, body?: unknown): Promise<T> {
    const startedAt = new Date().toISOString();
    const started = performance.now();
    let response: TransportResponse;
    try {
      response = await transport({ method, path, body });
    } catch (cause) {
      const error = networkError(cause);
      requestLog.record({
        target: 'api',
        method,
        path,
        status: 0,
        durationMs: Math.round(performance.now() - started),
        requestId: null,
        startedAt,
        error: error.message,
      });
      throw error;
    }
    const failed = response.status >= 400 ? parseApiError(response.status, response.body, response.requestId) : null;
    requestLog.record({
      target: 'api',
      method,
      path,
      status: response.status,
      durationMs: Math.round(performance.now() - started),
      requestId: response.requestId,
      startedAt,
      error: failed?.message ?? null,
    });
    if (failed) throw failed;
    return response.body as T;
  }

  return {
    me: () => call<Me>('GET', '/me'),

    listExpenses: (query: { status?: ExpenseStatus | null; limit?: number; cursor?: string | null } = {}) =>
      call<Page<Expense>>('GET', withQuery('/expenses', query)),
    createExpense: (input: ExpenseInput) => call<Expense>('POST', '/expenses', input),
    getExpense: (id: string) => call<ExpenseDetail>('GET', `/expenses/${seg(id)}`),
    updateExpense: (id: string, patch: ExpensePatch) => call<Expense>('PATCH', `/expenses/${seg(id)}`, patch),
    deleteExpense: (id: string) => call<undefined>('DELETE', `/expenses/${seg(id)}`),
    submitExpense: (id: string) => call<Expense>('POST', `/expenses/${seg(id)}/submit`),
    withdrawExpense: (id: string) => call<Expense>('POST', `/expenses/${seg(id)}/withdraw`),

    requestReceiptUpload: (id: string, file: { contentType: ReceiptContentType; size: number }) =>
      call<PresignedPost>('POST', `/expenses/${seg(id)}/receipt-upload`, file),
    getReceiptUrl: (id: string) => call<PresignedGet>('GET', `/expenses/${seg(id)}/receipt`),

    listApprovals: (query: { limit?: number; cursor?: string | null } = {}) =>
      call<Page<Expense>>('GET', withQuery('/approvals', query)),
    decide: (id: string, decision: Decision, comment: string | null) =>
      call<Accepted>('POST', `/approvals/${seg(id)}/decision`, comment ? { decision, comment } : { decision }),

    stats: (year: number) => call<Stats>('GET', withQuery('/stats', { year })),

    listReports: () => call<ReportInfo[]>('GET', '/reports'),
    generateReport: (month: string) => call<ReportAccepted>('POST', '/reports', { month }),
    getReportUrl: (month: string) => call<PresignedGet>('GET', `/reports/${seg(month)}`),
  };
}

export type Api = ReturnType<typeof createApi>;
