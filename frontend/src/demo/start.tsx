/**
 * Demo-mode bootstrap (`npm run dev:mock`). Only reachable through the `VITE_MOCK` branch in main.tsx,
 * so neither this module nor the in-memory backend ends up in the production bundle - and Amplify is
 * never loaded here: there is no Cognito, no config.json and no network traffic to AWS.
 */
import { StrictMode } from 'react';
import type { Root } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { createApi } from '../api/client';
import { browserStorage, readString, writeString } from '../lib/storage';
import type { Platform } from '../platform/platform';
import { DemoBackend } from './backend';
import { DEMO_CONFIG } from './config';
import { DemoApp, type DemoIdentity } from './DemoApp';
import { drawReceipt } from './draw';
import { buildSample, randomTemplate } from './receipts';
import { DEMO_USERS, isDemoRoleId } from './users';

const ROLE_KEY = 'expenseflow.demo.role';

function createIdentity(storage: Storage | null): DemoIdentity {
  const saved = readString(storage, ROLE_KEY);
  let user = DEMO_USERS[isDemoRoleId(saved) ? saved : 'employee'];
  return {
    get: () => user,
    set: (roleId) => {
      user = DEMO_USERS[roleId];
      writeString(storage, ROLE_KEY, roleId);
    },
  };
}

export function startDemo(root: Root): void {
  const storage = browserStorage();
  const identity = createIdentity(storage);
  const backend = new DemoBackend({
    config: DEMO_CONFIG,
    storage,
    currentUser: identity.get,
    renderSample: async (sample) => URL.createObjectURL(await drawReceipt(buildSample(sample.templateId, sample.date))),
  });

  const platform: Omit<Platform, 'live'> = {
    mode: 'demo',
    config: DEMO_CONFIG,
    api: createApi(backend.transport),
    uploadObject: backend.uploadObject,
    sampleReceipt: async (date) => {
      const templateId = randomTemplate(Math.random);
      const blob = await drawReceipt(buildSample(templateId, date));
      const file = new File([blob], `sample-receipt-${templateId}.png`, { type: 'image/png' });
      // The demo "model" can read receipts it drew itself exactly; anything else gets a plausible guess.
      backend.registerSample(file, { templateId, date });
      return file;
    },
  };

  // BASE_URL is "/" except on GitHub Pages, where the site lives under /<repo>/ (see vite.config.ts).
  root.render(
    <StrictMode>
      <BrowserRouter basename={import.meta.env.BASE_URL}>
        <DemoApp backend={backend} identity={identity} platform={platform} />
      </BrowserRouter>
    </StrictMode>,
  );
}
