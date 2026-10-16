import { describe, expect, it } from 'vitest';
import { parseGroups, rolesFor } from './groups';

describe('parseGroups', () => {
  it.each([
    ['decoded JWT array', ['managers', 'employees'], ['managers', 'employees']],
    ['API Gateway flattened string', '[managers employees]', ['managers', 'employees']],
    ['JSON list string', '["admins","employees"]', ['admins', 'employees']],
    ['comma separated string', 'managers, employees', ['managers', 'employees']],
    ['single group', 'admins', ['admins']],
    ['empty brackets', '[]', []],
    ['missing claim', undefined, []],
    ['null claim', null, []],
    ['unexpected type', 42, []],
    ['duplicates and blanks', ['admins', ' ', 'admins', 7], ['admins']],
    ['malformed JSON falls back to splitting', '["managers" employees]', ['managers', 'employees']],
  ])('%s', (_label, raw, expected) => {
    expect(parseGroups(raw)).toEqual(expected);
  });
});

describe('rolesFor', () => {
  it('lets managers and admins approve, but only admins administer', () => {
    expect(rolesFor(['employees'])).toEqual({ isManager: false, isAdmin: false, canApprove: false });
    expect(rolesFor(['employees', 'managers'])).toEqual({ isManager: true, isAdmin: false, canApprove: true });
    expect(rolesFor(['admins'])).toEqual({ isManager: false, isAdmin: true, canApprove: true });
  });
});
