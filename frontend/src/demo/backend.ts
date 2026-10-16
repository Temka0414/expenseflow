/**
 * Demo mode's stand-in for the whole AWS backend, running inside the browser tab.
 *
 * It answers the SAME HTTP contract as the real API (SPEC §5: routes, status codes, the 422 validation
 * shape and `{statusCode, message}` errors), so every page, the API client and the request inspector run
 * unchanged. Behind the API it plays the asynchronous parts with timers, in the same order AWS does:
 *
 *   PATCH/POST -> (DynamoDB stream) -> stream_processor -> EventBridge -> realtime_push -> WebSocket
 *   S3 upload  -> (Object Created)  -> ReceiptPipeline: MarkProcessing -> ExtractReceipt (Claude) -> Save
 *   submit     -> Expense Submitted -> ApprovalWorkflow: Claim -> policy -> AutoApprove | wait for a manager
 *
 * Every step re-checks its precondition before writing, like the conditional writes in the real state
 * machines: a withdrawn expense is never approved by a late timer.
 */
import type { Transport, TransportRequest } from '../api/client';
import type { Decision, Expense, ExpenseInput, MonthlyTotal, PresignedPost, ReportInfo } from '../api/types';
import type { AppConfig } from '../config';
import { EXPENSE_STATUSES, GROUPS, isReceiptContentType, LIMITS, type ExpenseStatus } from '../domain';
import { checkAutoApproval } from '../expenses/autoApproval';
import { missingForSubmit } from '../expenses/submit';
import { commentIssue, EXPENSE_FIELDS, validateExpenseInput } from '../expenses/validation';
import { parseLiveMessage } from '../live/events';
import type { LiveSource } from '../live/source';
import { currentMonth, isValidIsoMonth, todayIso } from '../lib/dates';
import { readString, removeKeys, writeString } from '../lib/storage';
import type { ObjectUploader } from '../platform/platform';
import { DEMO_ACCOUNT, DEMO_MODEL } from './config';
import { apiRequestId, ulid, uuid } from './ids';
import {
  actorOf,
  expenseEventDetail,
  newExpense,
  receiptEntry,
  setApproval,
  statusEntry,
  SYSTEM_ACTOR,
  touch,
  type Actor,
} from './model';
import { buildSample, simulatedExtraction, templateForSeed } from './receipts';
import { seedState } from './seed';
import { FILE_KEY_PREFIX, loadState, saveState, type DemoState, type SampleRef, type StoredExpense } from './state';
import type { DemoUser } from './users';

/** How long each simulated hop takes, in milliseconds. Tests set them to 0 and drive fake timers. */
export interface DemoTiming {
  /** API Gateway + Lambda round trip: [min, max]. */
  latency: readonly [number, number];
  /** DynamoDB stream -> stream_processor -> EventBridge -> realtime_push. */
  stream: number;
  /** Browser -> S3 transfer of the receipt. */
  upload: number;
  /** S3 "Object Created" -> EventBridge rule -> ReceiptPipeline MarkProcessing. */
  s3Event: number;
  /** Claude on Bedrock reading the receipt. */
  extraction: number;
  /** "Expense Submitted" -> EventBridge rule -> ApprovalWorkflow ClaimExpense / policy check. */
  workflow: number;
  /** SendTaskSuccess -> RecordDecision. */
  decision: number;
  /** report_generator querying GSI2 and writing the CSV. */
  report: number;
}

export const DEFAULT_TIMING: DemoTiming = {
  latency: [90, 320],
  stream: 450,
  upload: 1100,
  s3Event: 900,
  extraction: 3800,
  workflow: 1200,
  decision: 800,
  report: 2000,
};

export interface DemoBackendOptions {
  config: AppConfig;
  /** Where state is persisted; null keeps everything in memory (tests). */
  storage: Storage | null;
  /** The signed-in demo user: the identity the "JWT authorizer" would have verified. */
  currentUser: () => DemoUser;
  /** Draws a demo receipt and returns a URL for it (browser only; absent in tests). */
  renderSample?: (sample: SampleRef) => Promise<string>;
  timing?: Partial<DemoTiming>;
  random?: () => number;
}

interface Reply {
  status: number;
  body?: unknown;
}

interface RequestContext {
  user: DemoUser;
  params: string[];
  query: URLSearchParams;
  body: Record<string, unknown>;
}

type Route = [method: string, pattern: RegExp, handler: (context: RequestContext) => Reply | Promise<Reply>];

/** An HTTP error response in the API's own format. */
class HttpError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/** Handled domain error: `{"statusCode": 409, "message": "..."}` (Powertools ServiceError). */
function fail(status: number, message: string): HttpError {
  return new HttpError(status, { statusCode: status, message }, message);
}

/** Request validation error: 422 `{"statusCode": 422, "detail": [{"loc": [...], "type": "..."}]}`. */
function invalid(issues: Array<{ loc: Array<string | number>; type: string }>): HttpError {
  return new HttpError(422, { statusCode: 422, detail: issues }, 'validation failed');
}

/** JSON round trip: what crosses the "network" is a copy, never a reference into the backend state. */
function wire<T>(value: T): T {
  return value === undefined ? value : (JSON.parse(JSON.stringify(value)) as T);
}

const EXTENSIONS: Record<string, string> = {
  'image/jpeg': '.jpg',
  'image/png': '.png',
  'image/webp': '.webp',
  'image/gif': '.gif',
  'application/pdf': '.pdf',
};

const PRESIGN_SECONDS = 300;
const PERSISTED_FILE_MAX_BYTES = 1_500_000;

function base64Url(text: string): string {
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_');
}

function fromBase64Url(text: string): string {
  return atob(text.replace(/-/g, '+').replace(/_/g, '/'));
}

async function blobToDataUrl(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return `data:${blob.type || 'application/octet-stream'};base64,${btoa(binary)}`;
}

function csvCell(value: string | number | boolean | null | undefined): string {
  const text = value === null || value === undefined ? '' : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** Byte order mark: Excel only detects UTF-8 in a CSV when the file starts with it. */
const UTF8_BOM = String.fromCharCode(0xfeff);
const CRLF = '\r\n';

const REPORT_HEADER = [
  'expense_id',
  'owner_name',
  'owner_email',
  'title',
  'merchant',
  'category',
  'expense_date',
  'amount',
  'currency',
  'approved_at',
  'approver_email',
  'auto_approved',
];

export class DemoBackend {
  readonly config: AppConfig;
  readonly #storage: Storage | null;
  readonly #currentUser: () => DemoUser;
  readonly #renderSample: ((sample: SampleRef) => Promise<string>) | undefined;
  readonly #timing: DemoTiming;
  readonly #random: () => number;
  readonly #routes: Route[];
  readonly #timers = new Set<ReturnType<typeof setTimeout>>();
  readonly #listeners = new Set<(message: string, audiences: readonly string[]) => void>();
  /** Uploaded bytes (S3 objects) and object URLs handed out for them. */
  readonly #blobs = new Map<string, Blob>();
  readonly #urls = new Map<string, string>();
  /** Files the demo drew itself, so it can "read" them exactly. */
  readonly #samples = new WeakMap<Blob, SampleRef>();
  #state: DemoState;

  constructor(options: DemoBackendOptions) {
    this.config = options.config;
    this.#storage = options.storage;
    this.#currentUser = options.currentUser;
    this.#renderSample = options.renderSample;
    this.#timing = { ...DEFAULT_TIMING, ...options.timing };
    this.#random = options.random ?? Math.random;
    this.#routes = this.#buildRoutes();
    this.#state = loadState(this.#storage) ?? seedState(new Date(), this.#random);
    this.#save();
    this.#resume();
  }

  // ---------------------------------------------------------------------------------------------
  // Public surface: what the platform wires into the app
  // ---------------------------------------------------------------------------------------------

  /** The HTTP API, as a Transport for the shared API client. */
  readonly transport: Transport = async (request) => {
    const [min, max] = this.#timing.latency;
    await this.#sleep(min + Math.round(this.#random() * (max - min)));
    const requestId = apiRequestId(this.#random);
    try {
      const reply = await this.#dispatch(request);
      return { status: reply.status, body: wire(reply.body), requestId };
    } catch (error) {
      if (error instanceof HttpError) return { status: error.status, body: error.body, requestId };
      const message = error instanceof Error ? error.message : String(error);
      return { status: 500, body: { statusCode: 500, message: `Demo backend error: ${message}` }, requestId };
    }
  };

  /** "S3": accepts the presigned POST, enforcing the same policy conditions S3 would. */
  readonly uploadObject: ObjectUploader = (presigned, file, onProgress, signal) =>
    new Promise((resolve, reject) => {
      if (file.size < 1 || file.size > presigned.maxBytes) {
        reject(new Error('S3 rejected the file: it is larger than the signed policy allows.'));
        return;
      }
      const total = this.#timing.upload;
      let elapsed = 0;
      const finish = () => {
        void this.#storeObject(presigned.key, presigned.fields['Content-Type'], file);
        resolve({ status: 204, requestId: apiRequestId(this.#random).slice(0, 16).toUpperCase() });
      };
      if (total <= 0) {
        onProgress(1);
        finish();
        return;
      }
      const ticker = setInterval(() => {
        elapsed += 100;
        onProgress(Math.min(1, elapsed / total));
        if (elapsed >= total) {
          clearInterval(ticker);
          finish();
        }
      }, 100);
      signal?.addEventListener(
        'abort',
        () => {
          clearInterval(ticker);
          reject(new DOMException('Upload cancelled', 'AbortError'));
        },
        { once: true },
      );
    });

  /** The "WebSocket API" for one signed-in user: only events for their audiences are delivered. */
  liveSource(user: DemoUser): LiveSource {
    const audiences = new Set([`USER#${user.userId}`, ...user.groups.filter((g) => g !== GROUPS.employees).map((g) => `GROUP#${g}`)]);
    return {
      description: `In-tab demo event bus (as ${user.email}; audiences ${[...audiences].join(', ')})`,
      connect: (handlers) => {
        handlers.onStatus('connecting');
        const opened = setTimeout(() => handlers.onStatus('open'), 300);
        const listener = (message: string, targets: readonly string[]) => {
          if (!targets.some((target) => audiences.has(target))) return;
          const event = parseLiveMessage(message);
          if (event) handlers.onEvent(event);
        };
        this.#listeners.add(listener);
        return () => {
          clearTimeout(opened);
          this.#listeners.delete(listener);
          handlers.onStatus('stopped');
        };
      },
    };
  }

  /** Mark a file as one the demo drew, so the "AI" can read it exactly. */
  registerSample(file: Blob, sample: SampleRef): void {
    this.#samples.set(file, sample);
  }

  /** Throw everything away and start again from the seed data. */
  reset(): void {
    for (const timer of this.#timers) clearTimeout(timer);
    this.#timers.clear();
    for (const url of this.#urls.values()) if (url.startsWith('blob:')) URL.revokeObjectURL(url);
    this.#urls.clear();
    this.#blobs.clear();
    removeKeys(this.#storage, (key) => key.startsWith(FILE_KEY_PREFIX));
    this.#state = seedState(new Date(), this.#random);
    this.#save();
  }

  // ---------------------------------------------------------------------------------------------
  // Routing
  // ---------------------------------------------------------------------------------------------

  #buildRoutes(): Route[] {
    const id = '([^/]+)';
    return [
      ['GET', /^\/me$/, ({ user }) => this.#me(user)],
      ['GET', /^\/expenses$/, (c) => this.#listExpenses(c)],
      ['POST', /^\/expenses$/, (c) => this.#createExpense(c)],
      ['GET', new RegExp(`^/expenses/${id}$`), (c) => this.#getExpense(c)],
      ['PATCH', new RegExp(`^/expenses/${id}$`), (c) => this.#updateExpense(c)],
      ['DELETE', new RegExp(`^/expenses/${id}$`), (c) => this.#deleteExpense(c)],
      ['POST', new RegExp(`^/expenses/${id}/receipt-upload$`), (c) => this.#presignUpload(c)],
      ['GET', new RegExp(`^/expenses/${id}/receipt$`), (c) => this.#receiptUrl(c)],
      ['POST', new RegExp(`^/expenses/${id}/submit$`), (c) => this.#submit(c)],
      ['POST', new RegExp(`^/expenses/${id}/withdraw$`), (c) => this.#withdraw(c)],
      ['GET', /^\/approvals$/, (c) => this.#approvalQueue(c)],
      ['POST', new RegExp(`^/approvals/${id}/decision$`), (c) => this.#decide(c)],
      ['GET', /^\/stats$/, (c) => this.#stats(c)],
      ['GET', /^\/reports$/, ({ user }) => this.#listReports(user)],
      ['POST', /^\/reports$/, (c) => this.#generateReport(c)],
      ['GET', new RegExp(`^/reports/${id}$`), (c) => this.#reportUrl(c)],
    ];
  }

  async #dispatch(request: TransportRequest): Promise<Reply> {
    const url = new URL(request.path, 'https://api.demo.invalid');
    for (const [method, pattern, handler] of this.#routes) {
      const match = pattern.exec(url.pathname);
      if (!match || method !== request.method) continue;
      const body = request.body === undefined ? {} : wire(request.body);
      if (typeof body !== 'object' || body === null || Array.isArray(body)) {
        throw invalid([{ loc: ['body'], type: 'model_attributes_type' }]);
      }
      return handler({
        user: this.#currentUser(),
        params: match.slice(1).map(decodeURIComponent),
        query: url.searchParams,
        body: body as Record<string, unknown>,
      });
    }
    // API Gateway answers unknown routes itself, before any Lambda runs.
    throw new HttpError(404, { message: 'Not Found' }, 'route not found');
  }

  // ---------------------------------------------------------------------------------------------
  // Expenses
  // ---------------------------------------------------------------------------------------------

  #me(user: DemoUser): Reply {
    const isManager = user.groups.includes(GROUPS.managers);
    const isAdmin = user.groups.includes(GROUPS.admins);
    return { status: 200, body: { userId: user.userId, email: user.email, name: user.name, groups: user.groups, isManager, isAdmin } };
  }

  #listExpenses({ user, query }: RequestContext): Reply {
    const status = query.get('status');
    if (status !== null && status !== '' && !(EXPENSE_STATUSES as readonly string[]).includes(status)) {
      throw invalid([{ loc: ['query', 'status'], type: 'literal_error' }]);
    }
    const mine = this.#all()
      .filter((s) => s.expense.ownerId === user.userId && (!status || s.expense.status === status))
      .sort((a, b) => b.expense.createdAt.localeCompare(a.expense.createdAt) || b.expense.id.localeCompare(a.expense.id));
    // GSI1: PK = OWNER#{ownerId}, SK = {createdAt}#{id}, queried newest first.
    return this.#page(mine, query, (s) => ({
      PK: `EXPENSE#${s.expense.id}`,
      SK: 'META',
      GSI1PK: `OWNER#${s.expense.ownerId}`,
      GSI1SK: `${s.expense.createdAt}#${s.expense.id}`,
    }));
  }

  #createExpense({ user, body }: RequestContext): Reply {
    const fields = this.#validatedFields(body, false);
    const now = this.#now();
    const id = ulid(Date.now(), this.#random);
    // Validation guarantees a title on create (partial = false).
    const expense = newExpense(user, fields as ExpenseInput, id, now, this.config.defaultCurrency);
    this.#state.expenses[id] = {
      expense,
      history: [statusEntry(now, null, 'DRAFT', actorOf(user))],
      taskToken: null,
      queuedDecision: null,
      pendingReceipt: null,
    };
    this.#save();
    return { status: 201, body: this.#view(this.#state.expenses[id]) };
  }

  #getExpense({ user, params }: RequestContext): Reply {
    const stored = this.#visible(params[0], user);
    const history = [...stored.history].sort((a, b) => a.at.localeCompare(b.at));
    return { status: 200, body: { expense: this.#view(stored), history } };
  }

  #updateExpense({ user, params, body }: RequestContext): Reply {
    const stored = this.#owned(params[0], user);
    const { expense } = stored;
    if (expense.status !== 'DRAFT' && expense.status !== 'REJECTED') {
      throw fail(409, `only DRAFT or REJECTED expenses can be edited (this one is ${expense.status})`);
    }
    const { version, ...changes } = body;
    if (version !== undefined && version !== null) {
      if (typeof version !== 'number' || !Number.isInteger(version)) throw invalid([{ loc: ['body', 'version'], type: 'int_type' }]);
      // The real API puts `version = :v` into the UpdateItem ConditionExpression.
      if (version !== expense.version) throw fail(409, 'expense was modified in the meantime (version mismatch): reload and try again');
    }
    const fields = this.#validatedFields(changes, true);
    Object.assign(expense, fields);
    const now = this.#now();
    if (expense.status === 'REJECTED') {
      // Editing a rejected expense re-opens it as a draft and clears the old decision.
      expense.status = 'DRAFT';
      expense.decidedAt = null;
      expense.approval = null;
      stored.history.push(statusEntry(now, 'REJECTED', 'DRAFT', actorOf(user)));
    }
    touch(expense, now);
    this.#save();
    return { status: 200, body: this.#view(stored) };
  }

  #deleteExpense({ user, params }: RequestContext): Reply {
    const stored = this.#owned(params[0], user);
    if (stored.expense.status !== 'DRAFT') throw fail(409, 'only DRAFT expenses can be deleted');
    for (const key of [stored.expense.receipt?.key, stored.pendingReceipt?.key]) if (key) this.#deleteObject(key);
    delete this.#state.expenses[stored.expense.id];
    this.#save();
    return { status: 204 };
  }

  /**
   * Validate and normalise editable fields: trimmed text, and `null`/`""` for optional fields means
   * "remove the attribute" (stored as absent, rendered as null).
   */
  #validatedFields(body: Record<string, unknown>, partial: boolean): Partial<Expense> {
    const extra = Object.keys(body).filter((key) => !(EXPENSE_FIELDS as readonly string[]).includes(key));
    const issues = validateExpenseInput(body, { partial, today: todayIso() }).map((issue) => ({
      loc: ['body', issue.field],
      type: issue.type,
    }));
    issues.push(...extra.map((key) => ({ loc: ['body', key], type: 'extra_forbidden' })));
    if (issues.length > 0) throw invalid(issues);

    const fields: Record<string, unknown> = {};
    for (const field of EXPENSE_FIELDS) {
      if (!(field in body)) continue;
      const value = body[field];
      if (typeof value === 'string') fields[field] = value.trim() === '' ? null : value.trim();
      else fields[field] = value ?? null;
    }
    return fields;
  }

  // ---------------------------------------------------------------------------------------------
  // Receipts: presigned POST, "S3", and the ReceiptPipeline state machine
  // ---------------------------------------------------------------------------------------------

  #presignUpload({ user, params, body }: RequestContext): Reply {
    const stored = this.#owned(params[0], user);
    if (stored.expense.status !== 'DRAFT') throw fail(409, 'receipts can only be uploaded to DRAFT expenses');
    const { contentType, size } = body;
    const issues: Array<{ loc: string[]; type: string }> = [];
    if (!isReceiptContentType(contentType)) issues.push({ loc: ['body', 'contentType'], type: 'literal_error' });
    if (typeof size !== 'number' || !Number.isInteger(size)) issues.push({ loc: ['body', 'size'], type: 'int_type' });
    else if (size < 1) issues.push({ loc: ['body', 'size'], type: 'greater_than_equal' });
    else if (size > this.config.maxReceiptBytes) issues.push({ loc: ['body', 'size'], type: 'less_than_equal' });
    if (issues.length > 0 || !isReceiptContentType(contentType)) throw invalid(issues);

    const key = `receipts/${user.userId}/${stored.expense.id}/${ulid(Date.now(), this.#random)}${EXTENSIONS[contentType]}`;
    stored.pendingReceipt = { key, contentType };
    this.#save();
    const presigned: PresignedPost = {
      url: `https://${this.config.console.receiptsBucket}.s3.${this.config.region}.amazonaws.com/`,
      fields: this.#postPolicyFields(key, contentType),
      key,
      expiresIn: PRESIGN_SECONDS,
      maxBytes: this.config.maxReceiptBytes,
    };
    return { status: 200, body: presigned };
  }

  /** Fields shaped like boto3's `generate_presigned_post` output (SigV4), with a readable policy. */
  #postPolicyFields(key: string, contentType: string): Record<string, string> {
    const now = new Date();
    const stamp = now.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
    const credential = `ASIADEMO0000EXAMPLE0/${stamp.slice(0, 8)}/${this.config.region}/s3/aws4_request`;
    const policy = {
      expiration: new Date(now.getTime() + PRESIGN_SECONDS * 1000).toISOString(),
      conditions: [
        { bucket: this.config.console.receiptsBucket },
        { key },
        { 'Content-Type': contentType },
        ['content-length-range', 1, this.config.maxReceiptBytes],
        { 'x-amz-algorithm': 'AWS4-HMAC-SHA256' },
        { 'x-amz-credential': credential },
        { 'x-amz-date': stamp },
      ],
    };
    return {
      'Content-Type': contentType,
      key,
      'x-amz-algorithm': 'AWS4-HMAC-SHA256',
      'x-amz-credential': credential,
      'x-amz-date': stamp,
      policy: btoa(JSON.stringify(policy)),
      'x-amz-signature': Array.from({ length: 64 }, () => Math.floor(this.#random() * 16).toString(16)).join(''),
    };
  }

  async #receiptUrl({ user, params }: RequestContext): Promise<Reply> {
    const stored = this.#visible(params[0], user);
    const receipt = stored.expense.receipt;
    if (!receipt) throw fail(404, 'this expense has no receipt');
    const url = await this.#objectUrl(receipt.key);
    if (!url) throw fail(404, 'the receipt file is no longer available in this browser (demo data)');
    return { status: 200, body: { url, expiresIn: PRESIGN_SECONDS } };
  }

  async #storeObject(key: string, contentType: string, file: Blob): Promise<void> {
    const sample = this.#samples.get(file) ?? null;
    this.#state.files[key] = { contentType, sample };
    if (!sample) {
      this.#blobs.set(key, file);
      // Best effort: keep small uploads across reloads. Large ones live only as long as the tab.
      if (file.size <= PERSISTED_FILE_MAX_BYTES) writeString(this.#storage, FILE_KEY_PREFIX + key, await blobToDataUrl(file));
    }
    this.#save();
    // S3 -> EventBridge (default bus) "Object Created" -> rule -> ReceiptPipeline.
    this.#later(this.#timing.s3Event, () => this.#markProcessing(key));
  }

  async #objectUrl(key: string): Promise<string | null> {
    const file = this.#state.files[key];
    if (!file) return null;
    const cached = this.#urls.get(key);
    if (cached) return cached;
    let url: string | null;
    if (file.sample) url = this.#renderSample ? await this.#renderSample(file.sample) : null;
    else {
      const blob = this.#blobs.get(key);
      url = blob ? URL.createObjectURL(blob) : readString(this.#storage, FILE_KEY_PREFIX + key);
    }
    if (url) this.#urls.set(key, url);
    return url;
  }

  #deleteObject(key: string): void {
    const url = this.#urls.get(key);
    if (url?.startsWith('blob:')) URL.revokeObjectURL(url);
    this.#urls.delete(key);
    this.#blobs.delete(key);
    delete this.#state.files[key];
    removeKeys(this.#storage, (storageKey) => storageKey === FILE_KEY_PREFIX + key);
  }

  /** ReceiptPipeline `ParseKey` + `MarkProcessing` (condition: pendingReceiptKey = :key) + `DeleteOldReceipt`. */
  #markProcessing(key: string): void {
    const expenseId = key.split('/')[2];
    const stored = this.#state.expenses[expenseId];
    if (!stored || stored.pendingReceipt?.key !== key) return; // IgnoreUpload: superseded or deleted
    const { expense } = stored;
    const oldKey = expense.receipt?.key;
    expense.receipt = { key, contentType: stored.pendingReceipt.contentType, uploadedAt: this.#now() };
    expense.extraction = { status: 'PROCESSING', result: null, error: null, model: null, extractedAt: null };
    stored.pendingReceipt = null;
    if (oldKey && oldKey !== key) this.#deleteObject(oldKey);
    if (!this.config.extractionEnabled) {
      expense.extraction = { ...expense.extraction, status: 'SKIPPED', extractedAt: this.#now() };
      this.#receiptProcessed(stored);
      return;
    }
    this.#save();
    this.#later(this.#timing.extraction, () => this.#saveExtraction(expenseId, key));
  }

  /** `ExtractReceipt` (Claude) + `SaveExtraction` (condition: receiptKey = :key, else Superseded). */
  #saveExtraction(expenseId: string, key: string): void {
    const stored = this.#state.expenses[expenseId];
    if (!stored || stored.expense.receipt?.key !== key) return;
    const { expense } = stored;
    const file = this.#state.files[key];
    const sample = file?.sample
      ? buildSample(file.sample.templateId, file.sample.date)
      : buildSample(templateForSeed(this.#blobs.get(key)?.size ?? key.length), expense.expenseDate ?? todayIso());
    const now = this.#now();
    expense.extraction = {
      status: 'SUCCEEDED',
      result: simulatedExtraction(sample, { model: DEMO_MODEL, confidence: file?.sample ? 'high' : 'medium' }),
      error: null,
      model: DEMO_MODEL,
      extractedAt: now,
    };
    touch(expense, now);
    this.#receiptProcessed(stored);
  }

  #receiptProcessed(stored: StoredExpense): void {
    const { expense } = stored;
    stored.history.push(receiptEntry(expense.extraction?.extractedAt ?? this.#now(), expense.extraction?.status ?? 'FAILED'));
    this.#save();
    this.#publishExpenseEvent('Receipt Processed', stored, expense.status, SYSTEM_ACTOR, 'receipt');
  }

  // ---------------------------------------------------------------------------------------------
  // Submit, withdraw and the ApprovalWorkflow state machine
  // ---------------------------------------------------------------------------------------------

  #submit({ user, params }: RequestContext): Reply {
    const stored = this.#owned(params[0], user);
    const { expense } = stored;
    if (expense.status !== 'DRAFT') throw fail(409, `only DRAFT expenses can be submitted (this one is ${expense.status})`);
    const missing = missingForSubmit(expense);
    if (missing.length > 0) throw fail(400, `cannot submit: missing ${missing.join(', ')}`);
    const now = this.#now();
    expense.status = 'PENDING_APPROVAL';
    expense.submittedAt = now;
    expense.decidedAt = null;
    expense.approval = null;
    expense.workflowExecutionArn = null;
    stored.taskToken = null;
    touch(expense, now);
    stored.history.push(statusEntry(now, 'DRAFT', 'PENDING_APPROVAL', actorOf(user)));
    this.#save();
    this.#publishExpenseEvent('Expense Submitted', stored, 'DRAFT', actorOf(user), 'status');
    // The "Expense Submitted" rule on the custom bus starts the ApprovalWorkflow.
    this.#later(this.#timing.stream + this.#timing.workflow, () => this.#claimExpense(expense.id));
    return { status: 200, body: this.#view(stored) };
  }

  #withdraw({ user, params }: RequestContext): Reply {
    const stored = this.#owned(params[0], user);
    const { expense } = stored;
    if (expense.status !== 'PENDING_APPROVAL') throw fail(409, 'only expenses that are pending approval can be withdrawn');
    const now = this.#now();
    expense.status = 'DRAFT';
    expense.workflowExecutionArn = null; // + StopExecution(error="Withdrawn") on the running workflow
    if (expense.approval) expense.approval = { ...expense.approval, escalated: null };
    stored.taskToken = null;
    stored.queuedDecision = null;
    touch(expense, now);
    stored.history.push(statusEntry(now, 'PENDING_APPROVAL', 'DRAFT', actorOf(user)));
    this.#save();
    this.#publishExpenseEvent('Expense Withdrawn', stored, 'PENDING_APPROVAL', actorOf(user), 'status');
    return { status: 200, body: this.#view(stored) };
  }

  /** `ClaimExpense`: condition status = PENDING_APPROVAL AND attribute_not_exists(workflowExecutionArn). */
  #claimExpense(expenseId: string): void {
    const stored = this.#state.expenses[expenseId];
    if (!stored || stored.expense.status !== 'PENDING_APPROVAL' || stored.expense.workflowExecutionArn) return;
    const arn = `arn:aws:states:${this.config.region}:${DEMO_ACCOUNT}:execution:ApprovalWorkflowDEMO0000:${uuid(this.#random)}`;
    stored.expense.workflowExecutionArn = arn;
    this.#save();
    this.#later(this.#timing.workflow / 2, () => this.#applyPolicy(expenseId, arn));
  }

  /** `LoadPolicy` + `CanAutoApprove` -> `AutoApprove` or `RequestApproval` (waitForTaskToken). */
  #applyPolicy(expenseId: string, arn: string): void {
    const stored = this.#current(expenseId, arn);
    if (!stored || stored.taskToken) return;
    const { expense } = stored;
    const policy = { defaultCurrency: this.config.defaultCurrency, limit: this.config.autoApproveLimit };
    if (checkAutoApproval(expense, policy).eligible) {
      const now = this.#now();
      const comment = `Auto-approved by policy: ≤ ${policy.limit} ${policy.defaultCurrency} and receipt total verified by AI`;
      expense.status = 'APPROVED';
      expense.decidedAt = now;
      setApproval(expense, { approverId: null, approverEmail: 'auto-approval', comment, autoApproved: true, escalated: false });
      touch(expense, now);
      stored.history.push(statusEntry(now, 'PENDING_APPROVAL', 'APPROVED', SYSTEM_ACTOR, comment));
      this.#save();
      this.#publishExpenseEvent('Expense Approved', stored, 'PENDING_APPROVAL', SYSTEM_ACTOR, 'status');
      return;
    }
    // approval_request stores the task token; the execution now waits until SendTaskSuccess.
    stored.taskToken = `demo-task-token-${uuid(this.#random)}`;
    setApproval(expense, { requestedAt: this.#now(), escalated: false });
    this.#save();
  }

  #decide({ user, params, body }: RequestContext): Reply {
    if (!this.#canApprove(user)) throw fail(403, 'only managers and admins can decide on expenses');
    const { decision, comment } = body;
    const issues: Array<{ loc: string[]; type: string }> = [];
    if (decision !== 'APPROVED' && decision !== 'REJECTED') issues.push({ loc: ['body', 'decision'], type: 'literal_error' });
    if (comment !== undefined && comment !== null && typeof comment !== 'string') issues.push({ loc: ['body', 'comment'], type: 'string_type' });
    else if (typeof comment === 'string' && comment.trim().length > LIMITS.commentMax) {
      issues.push({ loc: ['body', 'comment'], type: 'string_too_long' });
    }
    if (issues.length > 0) throw invalid(issues);
    const text = typeof comment === 'string' ? comment.trim() : '';
    const problem = commentIssue(decision as Decision, text);
    if (problem) throw fail(400, problem);

    const stored = this.#find(params[0]);
    if (stored.expense.ownerId === user.userId) throw fail(403, 'you cannot approve or reject your own expense');
    if (stored.queuedDecision) throw fail(409, 'a decision for this expense was already recorded');
    if (stored.expense.status !== 'PENDING_APPROVAL' || !stored.taskToken) {
      throw fail(409, 'approval request not ready yet');
    }
    // SendTaskSuccess: the waiting execution resumes and RecordDecision writes the result.
    stored.queuedDecision = { decision: decision as Decision, approverId: user.userId, approverEmail: user.email, comment: text || null };
    this.#save();
    this.#later(this.#timing.decision, () => this.#recordDecision(stored.expense.id));
    return { status: 202, body: { accepted: true } };
  }

  /** `RecordDecision`: condition status = PENDING_APPROVAL; REMOVE approvalTaskToken. */
  #recordDecision(expenseId: string): void {
    const stored = this.#state.expenses[expenseId];
    const queued = stored?.queuedDecision;
    if (!stored || !queued) return;
    stored.queuedDecision = null;
    stored.taskToken = null;
    const { expense } = stored;
    if (expense.status !== 'PENDING_APPROVAL') {
      this.#save();
      return;
    }
    const now = this.#now();
    const approver: Actor = { id: queued.approverId, email: queued.approverEmail };
    expense.status = queued.decision;
    expense.decidedAt = now;
    setApproval(expense, {
      approverId: queued.approverId,
      approverEmail: queued.approverEmail,
      comment: queued.comment,
      autoApproved: false,
    });
    touch(expense, now);
    stored.history.push(statusEntry(now, 'PENDING_APPROVAL', queued.decision, approver, queued.comment));
    this.#save();
    this.#publishExpenseEvent(queued.decision === 'APPROVED' ? 'Expense Approved' : 'Expense Rejected', stored, 'PENDING_APPROVAL', approver, 'status');
  }

  #approvalQueue({ user, query }: RequestContext): Reply {
    if (!this.#canApprove(user)) throw fail(403, 'only managers and admins can see the approval queue');
    // GSI2: PK = STATUS#PENDING_APPROVAL, SK = {updatedAt}#{id}, oldest first; the caller's own are skipped.
    const queue = this.#all()
      .filter((s) => s.expense.status === 'PENDING_APPROVAL' && s.expense.ownerId !== user.userId)
      .sort((a, b) => a.expense.updatedAt.localeCompare(b.expense.updatedAt) || a.expense.id.localeCompare(b.expense.id));
    return this.#page(queue, query, (s) => ({
      PK: `EXPENSE#${s.expense.id}`,
      SK: 'META',
      GSI2PK: 'STATUS#PENDING_APPROVAL',
      GSI2SK: `${s.expense.updatedAt}#${s.expense.id}`,
    }));
  }

  // ---------------------------------------------------------------------------------------------
  // Stats and reports
  // ---------------------------------------------------------------------------------------------

  #stats({ user, query }: RequestContext): Reply {
    const raw = query.get('year');
    const year = raw === null || raw === '' ? new Date().getFullYear() : Number(raw);
    if (!Number.isInteger(year) || year < 2000 || year > 2100) throw invalid([{ loc: ['query', 'year'], type: 'int_parsing' }]);
    const approved = this.#all()
      .map((s) => s.expense)
      .filter((e) => e.status === 'APPROVED' && e.decidedAt?.startsWith(`${year}-`));
    return {
      status: 200,
      body: {
        year,
        mine: monthlyTotals(approved.filter((e) => e.ownerId === user.userId)),
        org: this.#canApprove(user) ? monthlyTotals(approved) : null,
      },
    };
  }

  #listReports(user: DemoUser): Reply {
    this.#requireAdmin(user);
    const reports: ReportInfo[] = [...this.#state.reports]
      .sort((a, b) => b.month.localeCompare(a.month))
      .map(({ month, key, size, lastModified }) => ({ month, key, size, lastModified }));
    return { status: 200, body: reports };
  }

  #generateReport({ user, body }: RequestContext): Reply {
    this.#requireAdmin(user);
    const { month } = body;
    if (typeof month !== 'string' || !isValidIsoMonth(month)) throw invalid([{ loc: ['body', 'month'], type: 'string_pattern_mismatch' }]);
    if (month > currentMonth()) throw fail(400, 'month must not be in the future');
    // The API invokes report_generator asynchronously (InvocationType=Event) and answers 202 at once.
    this.#later(this.#timing.report, () => this.#writeReport(month, user.email));
    return { status: 202, body: { accepted: true, month } };
  }

  #writeReport(month: string, requestedBy: string): void {
    const rows = this.#all()
      .map((s) => s.expense)
      .filter((e) => e.status === 'APPROVED' && e.decidedAt?.startsWith(month))
      .sort((a, b) => (a.decidedAt ?? '').localeCompare(b.decidedAt ?? ''));
    const totals: Record<string, number> = {};
    for (const e of rows) totals[e.currency] = Math.round(((totals[e.currency] ?? 0) + (e.amount ?? 0)) * 100) / 100;
    const lines = [
      REPORT_HEADER.join(','),
      ...rows.map((e) =>
        [
          e.id,
          e.ownerName,
          e.ownerEmail,
          e.title,
          e.merchant,
          e.category,
          e.expenseDate,
          e.amount,
          e.currency,
          e.decidedAt,
          e.approval?.approverEmail,
          e.approval?.autoApproved ? 'true' : 'false',
        ]
          .map(csvCell)
          .join(','),
      ),
    ];
    // UTF-8 with BOM and CRLF line endings, so Excel (common in Japan) opens Japanese text correctly.
    const csv = UTF8_BOM + lines.join(CRLF) + CRLF;
    const key = `reports/${month}.csv`;
    const generatedAt = this.#now();
    const report = { month, key, size: new TextEncoder().encode(csv).length, lastModified: generatedAt, csv };
    this.#state.reports = [...this.#state.reports.filter((r) => r.month !== month), report];
    this.#save();
    this.#publish(
      'expenseflow.reports',
      'Report Generated',
      { month, key, rowCount: rows.length, totals, requestedBy, idempotencyKey: `${month}:REPORT:${generatedAt}` },
      ['GROUP#admins'],
      0,
    );
  }

  #reportUrl({ user, params }: RequestContext): Reply {
    this.#requireAdmin(user);
    const report = this.#state.reports.find((r) => r.month === params[0]);
    if (!report) throw fail(404, `no report for ${params[0]} yet`);
    return { status: 200, body: { url: `data:text/csv;charset=utf-8,${encodeURIComponent(report.csv)}`, expiresIn: PRESIGN_SECONDS } };
  }

  // ---------------------------------------------------------------------------------------------
  // Events ("stream_processor -> EventBridge -> realtime_push")
  // ---------------------------------------------------------------------------------------------

  #publishExpenseEvent(
    detailType: string,
    stored: StoredExpense,
    previousStatus: ExpenseStatus | null,
    actor: Actor,
    kind: 'status' | 'receipt',
  ): void {
    const detail = expenseEventDetail(stored.expense, previousStatus, actor, kind);
    // realtime_push audiences (SPEC §6): the owner, plus managers for submissions and withdrawals.
    const audiences = [`USER#${stored.expense.ownerId}`];
    if (detailType === 'Expense Submitted' || detailType === 'Expense Withdrawn') audiences.push('GROUP#managers');
    this.#publish('expenseflow.expenses', detailType, detail, audiences, this.#timing.stream);
  }

  #publish(source: string, detailType: string, detail: Record<string, unknown>, audiences: string[], delay: number): void {
    const message = JSON.stringify({ type: 'event', source, detailType, time: this.#now(), detail });
    this.#later(delay, () => {
      for (const listener of this.#listeners) listener(message, audiences);
    });
  }

  // ---------------------------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------------------------

  /** Continue work a reload interrupted: Step Functions executions are durable, timers are not. */
  #resume(): void {
    for (const stored of this.#all()) {
      const { expense } = stored;
      if (stored.pendingReceipt && this.#state.files[stored.pendingReceipt.key]) {
        const key = stored.pendingReceipt.key;
        this.#later(this.#timing.s3Event, () => this.#markProcessing(key));
      }
      if (expense.extraction?.status === 'PROCESSING' && expense.receipt) {
        const key = expense.receipt.key;
        this.#later(this.#timing.extraction, () => this.#saveExtraction(expense.id, key));
      }
      if (stored.queuedDecision) this.#later(this.#timing.decision, () => this.#recordDecision(expense.id));
      else if (expense.status === 'PENDING_APPROVAL' && !stored.taskToken) {
        const arn = expense.workflowExecutionArn;
        if (arn) this.#later(this.#timing.workflow, () => this.#applyPolicy(expense.id, arn));
        else this.#later(this.#timing.workflow, () => this.#claimExpense(expense.id));
      }
    }
  }

  #page(items: StoredExpense[], query: URLSearchParams, keyOf: (s: StoredExpense) => Record<string, string>): Reply {
    const rawLimit = query.get('limit');
    const limit = rawLimit === null || rawLimit === '' ? 25 : Number(rawLimit);
    if (!Number.isInteger(limit) || limit < 1 || limit > LIMITS.pageSizeMax) {
      throw invalid([{ loc: ['query', 'limit'], type: Number.isInteger(limit) ? 'less_than_equal' : 'int_parsing' }]);
    }
    let start = 0;
    const cursor = query.get('cursor');
    if (cursor) {
      let lastKey: Record<string, string>;
      try {
        lastKey = JSON.parse(fromBase64Url(cursor)) as Record<string, string>;
      } catch {
        throw fail(400, 'invalid cursor');
      }
      start = items.findIndex((s) => JSON.stringify(keyOf(s)) === JSON.stringify(lastKey)) + 1;
    }
    const page = items.slice(start, start + limit);
    const more = start + limit < items.length;
    // nextCursor = urlsafe-base64(JSON(LastEvaluatedKey)), opaque to the client.
    const nextCursor = more && page.length > 0 ? base64Url(JSON.stringify(keyOf(page[page.length - 1]))) : null;
    return { status: 200, body: { items: page.map((s) => this.#view(s)), nextCursor } };
  }

  #view(stored: StoredExpense): Expense {
    return { ...stored.expense, pendingUpload: stored.pendingReceipt !== null };
  }

  #all(): StoredExpense[] {
    return Object.values(this.#state.expenses);
  }

  #find(id: string): StoredExpense {
    const stored = this.#state.expenses[id];
    if (!stored) throw fail(404, 'expense not found');
    return stored;
  }

  #visible(id: string, user: DemoUser): StoredExpense {
    const stored = this.#find(id);
    if (stored.expense.ownerId !== user.userId && !this.#canApprove(user)) throw fail(403, 'you cannot view this expense');
    return stored;
  }

  #owned(id: string, user: DemoUser): StoredExpense {
    const stored = this.#find(id);
    if (stored.expense.ownerId !== user.userId) throw fail(403, 'only the owner can do this');
    return stored;
  }

  /** The expense, if it is still pending in the given workflow execution (the "condition expression"). */
  #current(expenseId: string, arn: string): StoredExpense | null {
    const stored = this.#state.expenses[expenseId];
    return stored?.expense.status === 'PENDING_APPROVAL' && stored.expense.workflowExecutionArn === arn ? stored : null;
  }

  #canApprove(user: DemoUser): boolean {
    return user.groups.includes(GROUPS.managers) || user.groups.includes(GROUPS.admins);
  }

  #requireAdmin(user: DemoUser): void {
    if (!user.groups.includes(GROUPS.admins)) throw fail(403, 'only admins can manage reports');
  }

  #now(): string {
    return new Date().toISOString();
  }

  #later(ms: number, task: () => void): void {
    const timer = setTimeout(() => {
      this.#timers.delete(timer);
      task();
    }, ms);
    this.#timers.add(timer);
  }

  #sleep(ms: number): Promise<void> {
    return ms <= 0 ? Promise.resolve() : new Promise((resolve) => setTimeout(resolve, ms));
  }

  #save(): void {
    saveState(this.#storage, this.#state);
  }
}

function monthlyTotals(expenses: Expense[]): MonthlyTotal[] {
  const totals = new Map<string, MonthlyTotal>();
  for (const expense of expenses) {
    const month = (expense.decidedAt ?? '').slice(0, 7);
    const key = `${month}#${expense.currency}`;
    const total = totals.get(key) ?? { month, currency: expense.currency, approvedAmount: 0, approvedCount: 0 };
    total.approvedAmount = Math.round((total.approvedAmount + (expense.amount ?? 0)) * 100) / 100;
    total.approvedCount += 1;
    totals.set(key, total);
  }
  return [...totals.values()].sort((a, b) => a.month.localeCompare(b.month) || a.currency.localeCompare(b.currency));
}
