/**
 * Reconnect delays: exponential backoff with jitter.
 *
 * The ceiling doubles per failed attempt (1s, 2s, 4s ... capped at 60s) and the actual delay is drawn
 * uniformly between `minMs` and that ceiling ("full jitter", see the AWS Architecture Blog post
 * "Exponential Backoff And Jitter"). Without jitter, every open tab reconnects at the same instant after
 * an outage or a deploy and they all hit the $connect authorizer Lambda together.
 */

export interface BackoffOptions {
  baseMs: number;
  maxMs: number;
  minMs: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = { baseMs: 1_000, maxMs: 60_000, minMs: 250 };

/** Upper bound for the delay before reconnect attempt number `attempt` (0-based). */
export function backoffCeiling(attempt: number, options: BackoffOptions = DEFAULT_BACKOFF): number {
  const exponent = Math.min(Math.max(attempt, 0), 30); // 2 ** 30 is plenty and avoids Infinity
  return Math.min(options.maxMs, options.baseMs * 2 ** exponent);
}

export function backoffDelay(
  attempt: number,
  options: BackoffOptions = DEFAULT_BACKOFF,
  random: () => number = Math.random,
): number {
  const ceiling = backoffCeiling(attempt, options);
  const floor = Math.min(options.minMs, ceiling);
  return Math.round(floor + random() * (ceiling - floor));
}
