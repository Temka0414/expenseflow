import { Logo } from '../components/ui';
import { ConfigError } from '../config';

/** Shown instead of the app when /config.json is missing or incomplete (nothing else can work without it). */
export function BootError({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  const isConfigProblem = error instanceof ConfigError;
  return (
    <div className="boot-error">
      <div className="card stack">
        <div className="row">
          <Logo />
          <h1>ExpenseFlow can&apos;t start</h1>
        </div>
        <p className="muted">{message}</p>
        {isConfigProblem && (
          <>
            <p>
              The app reads its settings (Cognito user pool, API and WebSocket URLs, resource names) from{' '}
              <code>/config.json</code> at startup. The Frontend stack writes that file when it deploys; for local
              development you create it yourself:
            </p>
            <ol>
              <li>
                Deploy the stacks (<code>npx cdk deploy --all</code> in <code>infra/</code>).
              </li>
              <li>
                Run <code>python scripts/write_frontend_config.py --stage dev</code>: it reads the stack outputs and
                writes <code>frontend/public/config.json</code>.
              </li>
              <li>Reload this page.</li>
            </ol>
            <p>
              No AWS account yet? Try everything in <strong>demo mode</strong> with an in-browser backend:{' '}
              <code>npm run dev:mock</code>.
            </p>
          </>
        )}
        <div>
          <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    </div>
  );
}
