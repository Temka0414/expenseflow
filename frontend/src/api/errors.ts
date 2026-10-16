/**
 * One error type for everything the API can throw at the UI.
 *
 * The backend speaks three dialects (SPEC §5):
 *  - handled domain errors from the Powertools resolver: `{"statusCode": 409, "message": "..."}`
 *  - request validation errors (pydantic):               `{"statusCode": 422, "detail": [{"loc": [...], "type": "..."}]}`
 *  - API Gateway's own responses, before Lambda runs:     `{"message": "Unauthorized"}` (401 from the JWT
 *    authorizer, 429 throttling, 503/504 timeouts) - their text is terse, so we explain them instead.
 * Powertools deliberately omits pydantic's `msg`/`input` from 422 responses (they can echo user data), so
 * the readable text is derived from the error `type`.
 */

export interface FieldIssue {
  /** Dotted path inside the request, e.g. `amount` or `lineItems.0.amount`. */
  field: string;
  label: string;
  message: string;
}

export class ApiError extends Error {
  /** HTTP status; 0 = the request never got a response (offline, DNS, CORS rejection). */
  readonly status: number;
  readonly issues: FieldIssue[];
  /** `apigw-requestid` response header - search for it in CloudWatch Logs Insights. */
  readonly requestId: string | null;

  constructor(status: number, message: string, options: { issues?: FieldIssue[]; requestId?: string | null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.issues = options.issues ?? [];
    this.requestId = options.requestId ?? null;
  }

  get isConflict(): boolean {
    return this.status === 409;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** Message for one form field, if the server rejected it. */
  issueFor(field: string): string | undefined {
    return this.issues.find((issue) => issue.field === field)?.message;
  }
}

const STATUS_HINTS: Record<number, string> = {
  400: 'The request was not accepted.',
  401: 'Not authorized (401). Your sign-in may have expired: sign out and sign in again.',
  403: 'You do not have permission to do that (403).',
  404: 'Not found (404).',
  409: 'The item changed in the meantime (409). Reload and try again.',
  413: 'The request is too large (413).',
  429: 'Too many requests: API Gateway throttled this call (429). Try again in a moment.',
  500: 'The server hit an unexpected error (500).',
  502: 'The backend returned an invalid response (502).',
  503: 'The service is temporarily unavailable (503).',
  504: 'The backend took too long to answer (504). API Gateway waits at most 30 seconds.',
};

const LOCATION_PREFIXES = new Set(['body', 'query', 'path', 'header']);

/** Readable phrase for a pydantic v2 error type (the `type` field of a 422 detail item). */
export function describeIssueType(type: string): string {
  switch (type) {
    case 'missing':
      return 'is required';
    case 'string_too_short':
      return 'is too short';
    case 'string_too_long':
      return 'is too long';
    case 'too_short':
      return 'has too few items';
    case 'too_long':
      return 'has too many items';
    case 'greater_than':
    case 'greater_than_equal':
      return 'is too small';
    case 'less_than':
    case 'less_than_equal':
      return 'is too large';
    case 'decimal_max_places':
      return 'has too many decimal places';
    case 'decimal_max_digits':
    case 'decimal_whole_digits':
      return 'has too many digits';
    case 'literal_error':
    case 'enum':
      return 'is not one of the allowed values';
    case 'string_pattern_mismatch':
      return 'has an invalid format';
    case 'extra_forbidden':
      return 'is not an allowed field';
    case 'json_invalid':
      return 'is not valid JSON';
    case 'value_error':
    case 'assertion_error':
      return 'is invalid';
  }
  if (type.startsWith('date') || type.startsWith('datetime')) return 'is not a valid date (YYYY-MM-DD)';
  if (type.endsWith('_parsing')) return 'must be a number';
  if (type.endsWith('_type')) return 'has the wrong type';
  return `is invalid (${type})`;
}

/** `expenseDate` -> `Expense date`. */
export function humanizeField(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .trim()
    .toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function issueFromDetail(item: unknown): FieldIssue | null {
  if (typeof item !== 'object' || item === null) return null;
  const { loc, type, msg } = item as { loc?: unknown; type?: unknown; msg?: unknown };
  const path = Array.isArray(loc) ? loc.map(String) : [];
  const segments = path.length > 0 && LOCATION_PREFIXES.has(path[0]) ? path.slice(1) : path;
  const field = segments.join('.');
  const lastName = [...segments].reverse().find((segment) => !/^\d+$/.test(segment));
  const label = lastName ? humanizeField(lastName) : 'Request';
  const message =
    typeof msg === 'string' && msg !== '' ? msg : `${label} ${describeIssueType(typeof type === 'string' ? type : '')}`;
  return { field, label, message };
}

function bodyMessage(body: unknown): string | null {
  if (typeof body === 'object' && body !== null && 'message' in body) {
    const { message } = body;
    if (typeof message === 'string' && message.trim() !== '') return message;
  }
  return null;
}

/** Build an ApiError from an HTTP error response (status >= 400). */
export function parseApiError(status: number, body: unknown, requestId: string | null = null): ApiError {
  const detail = typeof body === 'object' && body !== null ? (body as { detail?: unknown }).detail : undefined;
  if (Array.isArray(detail)) {
    const issues = detail.map(issueFromDetail).filter((issue): issue is FieldIssue => issue !== null);
    if (issues.length > 0) {
      const message = `Please check the form: ${issues.map((issue) => issue.message).join('; ')}.`;
      return new ApiError(status, message, { issues, requestId });
    }
  }

  const message = bodyMessage(body);
  // Domain errors carry `statusCode` next to `message`: their text was written for humans.
  const isDomainError = typeof body === 'object' && body !== null && 'statusCode' in body;
  if (message && isDomainError) return new ApiError(status, message, { requestId });

  // API Gateway's own errors ("Unauthorized", "Not Found") are terse: prefer our explanation.
  const hint = STATUS_HINTS[status] ?? (status >= 500 ? STATUS_HINTS[500] : undefined);
  return new ApiError(status, hint ?? message ?? `Request failed (HTTP ${status}).`, { requestId });
}

/** The fetch never completed: offline, DNS failure, or the browser blocked the response (CORS). */
export function networkError(cause: unknown): ApiError {
  const reason = cause instanceof Error ? cause.message : String(cause);
  return new ApiError(
    0,
    `Could not reach the API (${reason}). Check your connection; on a new deployment also check the HTTP API CORS origins.`,
  );
}

/** Normalise anything thrown into an ApiError so components only handle one type. */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  if (error instanceof Error) return new ApiError(0, error.message);
  return new ApiError(0, String(error));
}
