import { Suspense } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router';
import { LoadingBlock, Logo } from '../components/ui';
import { usePlatform, useSession } from '../platform/context';
import { DemoBar } from './DemoBar';
import { ErrorBoundary } from './ErrorBoundary';
import { LiveIndicator } from './LiveIndicator';
import { UserMenu } from './UserMenu';

export function AppShell() {
  const { config, mode } = usePlatform();
  const { roles, demo } = useSession();
  const location = useLocation();

  return (
    <div className="app">
      {demo && <DemoBar controls={demo} />}
      <header className="topbar">
        <div className="topbar__inner">
          <Link to="/" className="brand" aria-label="ExpenseFlow home">
            <Logo />
            <span>ExpenseFlow</span>
            <span className="brand__stage">{mode === 'demo' ? 'demo' : config.stage}</span>
          </Link>
          <nav className="nav" aria-label="Main">
            <NavLink to="/" end>
              Dashboard
            </NavLink>
            <NavLink to="/expenses">Expenses</NavLink>
            {roles.canApprove && <NavLink to="/approvals">Approvals</NavLink>}
            {roles.isAdmin && <NavLink to="/reports">Reports</NavLink>}
            <NavLink to="/learn">Under the hood</NavLink>
          </nav>
          <div className="topbar__right">
            <LiveIndicator />
            <UserMenu />
          </div>
        </div>
      </header>
      <main className="main" id="main">
        <ErrorBoundary resetKey={location.pathname}>
          <Suspense fallback={<LoadingBlock />}>
            <Outlet />
          </Suspense>
        </ErrorBoundary>
      </main>
      <footer className="footer">
        <div className="footer__inner">
          <span>ExpenseFlow: a serverless AWS app for learning AWS.</span>
          <span>
            {mode === 'demo' ? 'Demo mode, no AWS calls' : `Region ${config.region}`} · Stage {config.stage}
          </span>
        </div>
      </footer>
    </div>
  );
}
