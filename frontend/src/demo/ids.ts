/**
 * Identifiers shaped like the real ones, so the demo teaches what to expect in AWS:
 *  - ULIDs (26 chars, Crockford base32, time-sortable) for expense ids, like `expenseflow/ids.py`;
 *  - `apigw-requestid` values (16 base64 chars) like API Gateway's.
 */

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const BASE64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

export function ulid(time: number, random: () => number): string {
  let timePart = '';
  let remaining = time;
  for (let index = 0; index < 10; index += 1) {
    timePart = CROCKFORD[remaining % 32] + timePart;
    remaining = Math.floor(remaining / 32);
  }
  let randomPart = '';
  for (let index = 0; index < 16; index += 1) randomPart += CROCKFORD[Math.floor(random() * 32)];
  return timePart + randomPart;
}

export function apiRequestId(random: () => number): string {
  let id = '';
  for (let index = 0; index < 15; index += 1) id += BASE64[Math.floor(random() * 64)];
  return `${id}=`;
}

export function uuid(random: () => number): string {
  const hex = Array.from({ length: 32 }, () => Math.floor(random() * 16).toString(16));
  hex[12] = '4';
  return `${hex.slice(0, 8).join('')}-${hex.slice(8, 12).join('')}-${hex.slice(12, 16).join('')}-${hex.slice(16, 20).join('')}-${hex.slice(20).join('')}`;
}
