/**
 * Runtime configuration (SPEC §8.5).
 *
 * The SPA is built once and deployed as static files; environment-specific values (user pool, API URLs,
 * resource names) are written next to it as /config.json by the Frontend stack at deploy time. That keeps
 * the build artifact identical across stages, and no AWS identifiers are baked into the JS bundle.
 * Locally, `scripts/write_frontend_config.py` writes frontend/public/config.json from stack outputs.
 */

export interface ConsoleResources {
  tableName: string;
  receiptsBucket: string;
  reportsBucket: string;
  eventBusName: string;
  approvalStateMachineArn: string;
  receiptStateMachineArn: string;
  dashboardName: string;
  httpApiId: string;
  webSocketApiId: string;
  userPoolId: string;
  distributionId: string;
}

export interface AppConfig {
  region: string;
  stage: string;
  userPoolId: string;
  userPoolClientId: string;
  /** No trailing slash. */
  apiUrl: string;
  wsUrl: string;
  maxReceiptBytes: number;
  defaultCurrency: string;
  autoApproveLimit: number;
  extractionEnabled: boolean;
  console: ConsoleResources;
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigError';
  }
}

const STRING_KEYS = ['region', 'stage', 'userPoolId', 'userPoolClientId', 'apiUrl', 'wsUrl', 'defaultCurrency'] as const;
const NUMBER_KEYS = ['maxReceiptBytes', 'autoApproveLimit'] as const;
const CONSOLE_KEYS = [
  'tableName',
  'receiptsBucket',
  'reportsBucket',
  'eventBusName',
  'approvalStateMachineArn',
  'receiptStateMachineArn',
  'dashboardName',
  'httpApiId',
  'webSocketApiId',
  'userPoolId',
  'distributionId',
] as const;

/** Validate the parsed JSON so a broken deployment fails loudly at startup, not on some later click. */
export function parseConfig(raw: unknown): AppConfig {
  if (typeof raw !== 'object' || raw === null) throw new ConfigError('config.json is not a JSON object.');
  const record = raw as Record<string, unknown>;
  const problems: string[] = [];
  for (const key of STRING_KEYS) {
    if (typeof record[key] !== 'string' || record[key] === '') problems.push(`"${key}" must be a non-empty string`);
  }
  for (const key of NUMBER_KEYS) {
    if (typeof record[key] !== 'number' || !Number.isFinite(record[key])) problems.push(`"${key}" must be a number`);
  }
  if (typeof record.extractionEnabled !== 'boolean') problems.push('"extractionEnabled" must be true or false');
  const consoleSection = record.console;
  if (typeof consoleSection !== 'object' || consoleSection === null) {
    problems.push('"console" must be an object');
  } else {
    const resources = consoleSection as Record<string, unknown>;
    for (const key of CONSOLE_KEYS) {
      if (typeof resources[key] !== 'string') problems.push(`"console.${key}" must be a string`);
    }
  }
  if (problems.length > 0) throw new ConfigError(`config.json is incomplete: ${problems.join('; ')}.`);

  const config = record as unknown as AppConfig;
  return { ...config, apiUrl: config.apiUrl.replace(/\/+$/, '') };
}

/** Fetch /config.json once, before React renders. */
export async function loadConfig(): Promise<AppConfig> {
  // no-store: after a redeploy the browser must never reuse a cached config that points at old resources.
  const response = await fetch('/config.json', { cache: 'no-store' });
  // The Vite dev server answers unknown paths with index.html (SPA fallback), so a missing file shows up
  // as 200 text/html rather than 404: check the content type, not just the status.
  const contentType = response.headers.get('content-type') ?? '';
  if (!response.ok || !contentType.includes('json')) {
    throw new ConfigError(`/config.json was not found (HTTP ${response.status}, ${contentType || 'no content type'}).`);
  }
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    throw new ConfigError('/config.json is not valid JSON.');
  }
  return parseConfig(raw);
}
