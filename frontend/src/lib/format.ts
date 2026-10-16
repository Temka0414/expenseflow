/**
 * Display formatting. The UI is English, so formats are pinned to `en` instead of the browser locale
 * (a Japanese browser would otherwise mix "2026年10月14日" into English sentences). Times are shown in
 * the viewer's own time zone; the API always speaks ISO-8601 UTC (`2026-10-13T05:01:02.345Z`).
 */

const moneyFormatters = new Map<string, Intl.NumberFormat>();

/** `1280, "JPY"` -> `¥1,280`; `19.9, "USD"` -> `$19.90`. Intl knows each currency's minor units. */
export function formatMoney(amount: number | null | undefined, currency: string | null | undefined): string {
  if (amount === null || amount === undefined) return '—';
  const code = currency ?? 'JPY';
  let formatter = moneyFormatters.get(code);
  if (!formatter) {
    try {
      formatter = new Intl.NumberFormat('en', { style: 'currency', currency: code, currencyDisplay: 'narrowSymbol' });
    } catch {
      formatter = new Intl.NumberFormat('en', { maximumFractionDigits: 2 });
    }
    moneyFormatters.set(code, formatter);
  }
  return formatter.format(amount);
}

const numberFormatter = new Intl.NumberFormat('en', { maximumFractionDigits: 2 });

export function formatNumber(value: number): string {
  return numberFormatter.format(value);
}

const dateFormatter = new Intl.DateTimeFormat('en', { dateStyle: 'medium' });
const dateTimeFormatter = new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short' });
const timeFormatter = new Intl.DateTimeFormat('en', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const monthFormatter = new Intl.DateTimeFormat('en', { month: 'long', year: 'numeric', timeZone: 'UTC' });

/** A calendar date (`YYYY-MM-DD`, no time zone) -> `Oct 14, 2026`. */
export function formatCalendarDate(isoDate: string | null | undefined): string {
  if (!isoDate) return '—';
  const [year, month, day] = isoDate.split('-').map(Number);
  if (!year || !month || !day) return isoDate;
  return dateFormatter.format(new Date(year, month - 1, day));
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : dateTimeFormatter.format(date);
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : timeFormatter.format(date);
}

/** `2026-09` -> `September 2026`. */
export function formatMonth(month: string): string {
  const [year, monthIndex] = month.split('-').map(Number);
  if (!year || !monthIndex) return month;
  return monthFormatter.format(new Date(Date.UTC(year, monthIndex - 1, 1)));
}

const relativeFormatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
const RELATIVE_STEPS: Array<[unit: Intl.RelativeTimeFormatUnit, seconds: number]> = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['week', 7 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
];

/**
 * `3 minutes ago`, `yesterday`, `just now`. `now` is passed in so callers control re-rendering; it can
 * lag behind a freshly pushed event (or the server clock can run ahead), so the past is never shown as "in 5 seconds".
 */
export function formatRelative(iso: string | null | undefined, now: number): string {
  if (!iso) return '—';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return iso;
  const seconds = Math.round((Math.min(time, now) - now) / 1000);
  for (const [unit, size] of RELATIVE_STEPS) {
    if (Math.abs(seconds) >= size) return relativeFormatter.format(Math.round(seconds / size), unit);
  }
  return Math.abs(seconds) < 10 ? 'just now' : relativeFormatter.format(seconds, 'second');
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

/** Initials for an avatar: "Aiko Tanaka" -> "AT", "kenji@example.com" -> "K". */
export function initials(name: string): string {
  const words = name.replace(/@.*$/, '').split(/[\s._-]+/).filter(Boolean);
  return words
    .slice(0, 2)
    .map((word) => word.charAt(0).toUpperCase())
    .join('');
}
