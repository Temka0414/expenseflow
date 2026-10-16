/**
 * Calendar helpers. Expense dates are plain calendar dates (`YYYY-MM-DD`) in the user's local time
 * zone; report months are `YYYY-MM`. Kept free of React so the demo backend can share them.
 */

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

export function isoDate(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function isoMonth(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}`;
}

export function todayIso(now: Date = new Date()): string {
  return isoDate(now);
}

/** `YYYY-MM-DD` shifted by whole days (calendar arithmetic, immune to DST). */
export function addDays(date: string, days: number): string {
  const [year, month, day] = date.split('-').map(Number);
  return isoDate(new Date(year, month - 1, day + days));
}

export function currentMonth(now: Date = new Date()): string {
  return isoMonth(now);
}

/** The month before `now` - what the scheduled report covers on the 1st. */
export function previousMonth(now: Date = new Date()): string {
  return isoMonth(new Date(now.getFullYear(), now.getMonth() - 1, 1));
}

/** True when `value` is a real calendar date in `YYYY-MM-DD` form (rejects 2026-02-30). */
export function isValidIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(year, month - 1, day);
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day;
}

export function isValidIsoMonth(value: string): boolean {
  return /^\d{4}-(0[1-9]|1[0-2])$/.test(value);
}
