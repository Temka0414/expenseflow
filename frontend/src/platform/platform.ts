/**
 * The seam between the UI and "the cloud". The same components run against real AWS (Amplify + API
 * Gateway + S3 + WebSocket API) or against the in-memory demo backend; only this object differs.
 */
import type { Api } from '../api/client';
import type { PresignedPost } from '../api/types';
import type { Roles } from '../auth/groups';
import type { AppConfig } from '../config';
import type { LiveSource } from '../live/source';

export interface ObjectUploadResult {
  status: number;
  /** `x-amz-request-id`, when S3's CORS rules expose it. */
  requestId: string | null;
}

/** Upload a file with an S3 presigned POST, reporting progress as a fraction 0..1. */
export type ObjectUploader = (
  presigned: PresignedPost,
  file: Blob,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
) => Promise<ObjectUploadResult>;

export interface Platform {
  mode: 'aws' | 'demo';
  config: AppConfig;
  api: Api;
  live: LiveSource;
  uploadObject: ObjectUploader;
  /** Demo mode: draws a sample receipt dated `date`, so the AI flow can be tried without a real one. */
  sampleReceipt?: (date: string) => Promise<File>;
}

export interface Viewer {
  userId: string;
  email: string;
  name: string;
  groups: string[];
}

export interface DemoRole {
  id: string;
  label: string;
  description: string;
}

export interface DemoControls {
  roles: readonly DemoRole[];
  currentRoleId: string;
  switchRole: (roleId: string) => void;
  /** Throw away all demo data and start again from the seed data. */
  reset: () => void;
}

export interface Session {
  viewer: Viewer;
  roles: Roles;
  /** AWS mode: Amplify sign-out (clears the tokens kept in localStorage). */
  signOut?: () => void;
  /** AWS mode: force a token refresh so new group memberships show up without signing out. */
  refreshGroups?: () => Promise<void>;
  /** Demo mode: role switcher. */
  demo?: DemoControls;
}
