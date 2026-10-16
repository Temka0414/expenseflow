/**
 * The three demo identities. In AWS these would be Cognito users; the groups are the user pool groups
 * (`employees` is added by the post-confirmation trigger, the others by scripts/add_user_to_group.py).
 */
import { GROUPS } from '../domain';

export const DEMO_ROLE_IDS = ['employee', 'manager', 'admin'] as const;
export type DemoRoleId = (typeof DEMO_ROLE_IDS)[number];

export interface DemoUser {
  roleId: DemoRoleId;
  /** Cognito `sub`. */
  userId: string;
  email: string;
  name: string;
  groups: string[];
  label: string;
  description: string;
}

export const DEMO_USERS: Record<DemoRoleId, DemoUser> = {
  employee: {
    roleId: 'employee',
    userId: '7c2f9a64-1d1e-4f0b-9a55-2f3b8e0c1a01',
    email: 'aiko.tanaka@example.com',
    name: 'Aiko Tanaka',
    groups: [GROUPS.employees],
    label: 'Employee',
    description: 'Creates expenses, uploads receipts and submits them.',
  },
  manager: {
    roleId: 'manager',
    userId: '3b8e41d2-5a07-4c9e-8f21-6d0c9a7b4e02',
    email: 'kenji.sato@example.com',
    name: 'Kenji Sato',
    groups: [GROUPS.employees, GROUPS.managers],
    label: 'Manager',
    description: 'Also approves or rejects other people’s expenses.',
  },
  admin: {
    roleId: 'admin',
    userId: 'e91d0c3a-7f64-4b18-a2c5-9e3f1b6d8c03',
    email: 'yuki.yamamoto@example.com',
    name: 'Yuki Yamamoto',
    groups: [GROUPS.employees, GROUPS.admins],
    label: 'Admin',
    description: 'Approves too, and generates the monthly CSV reports.',
  },
};

export function isDemoRoleId(value: unknown): value is DemoRoleId {
  return typeof value === 'string' && (DEMO_ROLE_IDS as readonly string[]).includes(value);
}
