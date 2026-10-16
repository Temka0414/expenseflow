import { describe, expect, it } from 'vitest';
import { ApiError, describeIssueType, humanizeField, networkError, parseApiError, toApiError } from './errors';

describe('parseApiError', () => {
  it('uses the message of a handled domain error verbatim', () => {
    const error = parseApiError(409, { statusCode: 409, message: 'expense was modified (version mismatch)' }, 'req-1');
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.isConflict).toBe(true);
    expect(error.message).toBe('expense was modified (version mismatch)');
    expect(error.requestId).toBe('req-1');
    expect(error.issues).toEqual([]);
  });

  it('turns a 422 validation body into readable, per-field issues', () => {
    const error = parseApiError(422, {
      statusCode: 422,
      detail: [
        { loc: ['body', 'amount'], type: 'greater_than' },
        { loc: ['body', 'expenseDate'], type: 'date_from_datetime_parsing' },
        { loc: ['body', 'title'], type: 'missing' },
        { loc: ['query', 'limit'], type: 'less_than_equal' },
      ],
    });
    expect(error.status).toBe(422);
    expect(error.issues).toEqual([
      { field: 'amount', label: 'Amount', message: 'Amount is too small' },
      { field: 'expenseDate', label: 'Expense date', message: 'Expense date is not a valid date (YYYY-MM-DD)' },
      { field: 'title', label: 'Title', message: 'Title is required' },
      { field: 'limit', label: 'Limit', message: 'Limit is too large' },
    ]);
    expect(error.issueFor('expenseDate')).toBe('Expense date is not a valid date (YYYY-MM-DD)');
    expect(error.message).toBe(
      'Please check the form: Amount is too small; Expense date is not a valid date (YYYY-MM-DD); Title is required; Limit is too large.',
    );
  });

  it('labels nested locations by their last named segment and prefers a server msg when present', () => {
    const error = parseApiError(422, {
      detail: [
        { loc: ['body', 'lineItems', 0, 'unitPrice'], type: 'float_parsing' },
        { loc: ['body'], type: 'json_invalid' },
        { loc: ['body', 'currency'], type: 'literal_error', msg: "Input should be 'JPY' or 'USD'" },
      ],
    });
    expect(error.issues.map((issue) => [issue.field, issue.message])).toEqual([
      ['lineItems.0.unitPrice', 'Unit price must be a number'],
      ['', 'Request is not valid JSON'],
      ['currency', "Input should be 'JPY' or 'USD'"],
    ]);
  });

  it('explains terse API Gateway responses instead of echoing them', () => {
    expect(parseApiError(401, { message: 'Unauthorized' }).message).toMatch(/sign in again/);
    expect(parseApiError(429, { message: 'Too Many Requests' }).message).toMatch(/throttled/);
    expect(parseApiError(504, { message: 'Service Unavailable' }).message).toMatch(/30 seconds/);
    expect(parseApiError(507, undefined).message).toMatch(/unexpected error/);
  });

  it('falls back to the gateway message, then to the status code', () => {
    expect(parseApiError(418, { message: "I'm a teapot" }).message).toBe("I'm a teapot");
    expect(parseApiError(418, undefined).message).toBe('Request failed (HTTP 418).');
  });
});

describe('helpers', () => {
  it('humanizes camelCase and snake_case field names', () => {
    expect(humanizeField('expenseDate')).toBe('Expense date');
    expect(humanizeField('auto_approve_limit')).toBe('Auto approve limit');
  });

  it('describes unknown pydantic types without hiding them', () => {
    expect(describeIssueType('url_scheme')).toBe('is invalid (url_scheme)');
    expect(describeIssueType('int_type')).toBe('has the wrong type');
  });

  it('wraps network failures and arbitrary throwables', () => {
    const offline = networkError(new TypeError('Failed to fetch'));
    expect(offline.status).toBe(0);
    expect(offline.message).toContain('Failed to fetch');
    expect(offline.message).toContain('CORS');
    expect(toApiError(offline)).toBe(offline);
    expect(toApiError('boom').message).toBe('boom');
  });
});
