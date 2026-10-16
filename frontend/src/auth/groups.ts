/**
 * Cognito group membership arrives in the `cognito:groups` claim, in different shapes depending on who
 * hands it to you:
 *  - decoded JWT in the browser (Amplify):          ["managers", "employees"]
 *  - API Gateway HTTP API claims (payload v2.0):    "[managers employees]"  (every claim is flattened to a string)
 *  - hand-written test data / other tooling:        '["managers","employees"]', "managers,employees", "admins"
 * The claim is *absent* when the user belongs to no group.
 */
import { GROUPS } from '../domain';

export function parseGroups(raw: unknown): string[] {
  if (raw === null || raw === undefined) return [];
  if (Array.isArray(raw)) return unique(raw.filter((g): g is string => typeof g === 'string' && g.trim() !== ''));
  if (typeof raw !== 'string') return [];

  const text = raw.trim();
  if (text.startsWith('[') && text.includes('"')) {
    try {
      return parseGroups(JSON.parse(text));
    } catch {
      // Not JSON after all: fall through to the bracketed, space/comma separated form.
    }
  }
  const inner = text.startsWith('[') && text.endsWith(']') ? text.slice(1, -1) : text;
  return unique(
    inner
      .split(/[\s,]+/)
      .map((g) => g.replace(/^["']|["']$/g, ''))
      .filter((g) => g !== ''),
  );
}

function unique(values: string[]): string[] {
  return [...new Set(values.map((v) => v.trim()))];
}

export interface Roles {
  isManager: boolean;
  isAdmin: boolean;
  /** Approvals are open to managers and admins (SPEC §5 `/approvals`). */
  canApprove: boolean;
}

export function rolesFor(groups: readonly string[]): Roles {
  const isManager = groups.includes(GROUPS.managers);
  const isAdmin = groups.includes(GROUPS.admins);
  return { isManager, isAdmin, canApprove: isManager || isAdmin };
}
