/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "true" in demo mode (`npm run dev:mock`); replaced at build time by vite.config.ts. */
  readonly VITE_MOCK: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
