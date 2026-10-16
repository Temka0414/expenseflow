/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Demo mode = `vite --mode mock` (npm run dev:mock) or VITE_MOCK=true in the environment.
  // Defining the flag as a literal lets the bundler drop the `VITE_MOCK === 'true'` branch, so the
  // in-memory demo backend never reaches the production bundle (and Amplify never reaches the demo one).
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const mock = mode === 'mock' || env.VITE_MOCK === 'true';

  return {
    // Only the GitHub Pages demo lives below a sub-path (https://<user>.github.io/<repo>/): the
    // pages workflow sets VITE_BASE=/<repo>/ (npm run build:pages). Deployed AWS builds keep "/".
    base: env.VITE_BASE || '/',
    plugins: [react()],
    define: {
      'import.meta.env.VITE_MOCK': JSON.stringify(mock ? 'true' : 'false'),
    },
    server: {
      // The HTTP API's CORS allow-list contains exactly http://localhost:5173 (SPEC §5):
      // fail fast instead of silently moving to 5174 and getting CORS errors.
      port: 5173,
      strictPort: true,
    },
    preview: { port: 4173, strictPort: true },
    build: {
      // Maps are generated for local debugging but not referenced from the JS, and the Frontend
      // stack excludes *.map from the S3 deployment, so source code is not published.
      sourcemap: 'hidden',
      rolldownOptions: {
        output: {
          // Rolldown (Vite 8) replacement for Rollup's manualChunks: long-lived vendor chunks that
          // survive app-code deploys, so CloudFront/browser caches stay warm.
          // Higher priority captures first, and a group also pulls in its captured modules'
          // dependencies (includeDependenciesRecursively defaults to true), so order = dependency order.
          // `[\\/]` matches both separators: module ids use backslashes on Windows.
          codeSplitting: {
            groups: [
              { name: 'react', test: /node_modules[\\/](react|react-dom|scheduler|react-router)[\\/]/, priority: 30 },
              {
                name: 'amplify',
                test: /node_modules[\\/](aws-amplify|@aws-amplify[\\/](?!ui)[^\\/]+|@aws-crypto|@smithy|@aws-sdk)[\\/]/,
                priority: 20,
              },
              { name: 'amplify-ui', test: /node_modules[\\/]/, priority: 10 },
            ],
          },
        },
      },
    },
    test: {
      environment: 'node',
      include: ['src/**/*.test.ts'],
    },
  };
});
