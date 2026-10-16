/** Small presentational building blocks shared by every page. */
import type { ReactNode } from 'react';
import type { ApiError } from '../api/errors';
import { STATUS_LABELS, type ExpenseStatus } from '../domain';

const STATUS_CLASS: Record<ExpenseStatus, string> = {
  DRAFT: 'badge--draft',
  PENDING_APPROVAL: 'badge--pending',
  APPROVED: 'badge--approved',
  REJECTED: 'badge--rejected',
};

export function StatusBadge({ status }: { status: ExpenseStatus }) {
  return <span className={`badge ${STATUS_CLASS[status]}`}>{STATUS_LABELS[status]}</span>;
}

export type Tone = 'info' | 'success' | 'warning' | 'danger';

const TONE_ICON: Record<Tone, string> = { info: 'i', success: '✓', warning: '!', danger: '!' };

export function Notice({ tone = 'info', title, children }: { tone?: Tone; title?: ReactNode; children?: ReactNode }) {
  return (
    <div className={`notice notice--${tone}`} role={tone === 'danger' ? 'alert' : undefined}>
      <span className="notice__icon" aria-hidden="true">
        {TONE_ICON[tone]}
      </span>
      <div className="notice__body">
        {title && <span className="notice__title">{title}</span>}
        {children}
      </div>
    </div>
  );
}

export function Spinner({ small = false, label }: { small?: boolean; label?: string }) {
  return <span className={small ? 'spinner spinner--sm' : 'spinner'} role={label ? 'status' : undefined} aria-label={label} />;
}

export function LoadingBlock({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="loading-block" role="status">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}

/** An API failure with the `apigw-requestid`, so it can be looked up in CloudWatch Logs Insights. */
export function ErrorNotice({ error, onRetry, title = 'Something went wrong' }: { error: ApiError; onRetry?: () => void; title?: string }) {
  return (
    <Notice tone="danger" title={title}>
      <span>{error.message}</span>
      {error.requestId && (
        <span className="subtle">
          Request id <code>{error.requestId}</code> (see the Learn page to trace it in CloudWatch).
        </span>
      )}
      {onRetry && (
        <span>
          <button type="button" className="btn btn-sm" onClick={onRetry}>
            Try again
          </button>
        </span>
      )}
    </Notice>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <span className="empty__title">{title}</span>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function PageHeader({
  title,
  eyebrow,
  description,
  actions,
}: {
  title: ReactNode;
  eyebrow?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="page-header__text">
        {eyebrow && <div className="page-header__eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p className="muted">{description}</p>}
      </div>
      {actions && <div className="page-header__actions">{actions}</div>}
    </header>
  );
}

export function Card({
  title,
  description,
  actions,
  children,
  flush = false,
  footer,
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  footer?: ReactNode;
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card__header">
          <div>
            {title && <h2>{title}</h2>}
            {description && <p>{description}</p>}
          </div>
          {actions && <div className="row">{actions}</div>}
        </div>
      )}
      <div className={flush ? 'card__body card__body--flush' : 'card__body'}>{children}</div>
      {footer && <div className="card__footer">{footer}</div>}
    </section>
  );
}

export function ProgressBar({ value, label }: { value: number; label: string }) {
  const percent = Math.round(Math.min(1, Math.max(0, value)) * 100);
  return (
    <div className="progress" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
      <div className="progress__bar" style={{ width: `${percent}%` }} />
    </div>
  );
}

export function Logo({ className = 'brand__logo' }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="#2f5fd0" />
      <path d="M10 8h12v3H13.5v3.5H21v3h-7.5V21H22v3H10z" fill="#fff" />
      <circle cx="24.5" cy="8.5" r="3" fill="#5acb93" />
    </svg>
  );
}
