import { createRoot } from 'react-dom/client';
import './styles/index.css';

/**
 * Entry point. `import.meta.env.VITE_MOCK` is replaced by a string literal at build time
 * (vite.config.ts), so the bundler keeps exactly one of the two dynamic imports below:
 * production builds never contain the demo backend, demo builds never contain Amplify.
 */
async function boot(): Promise<void> {
  const container = document.getElementById('root');
  if (!container) throw new Error('index.html has no #root element');
  const root = createRoot(container);
  if (import.meta.env.VITE_MOCK === 'true') {
    const { startDemo } = await import('./demo/start');
    startDemo(root);
  } else {
    const { startAws } = await import('./aws/start');
    await startAws(root);
  }
}

void boot();
