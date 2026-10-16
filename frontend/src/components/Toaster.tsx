import { Link } from 'react-router';
import { toasts, useToasts } from './toastStore';

/** Renders the toast stack; `aria-live` makes screen readers announce new toasts politely. */
export function Toaster() {
  const items = useToasts();
  return (
    <div className="toaster" aria-live="polite" aria-relevant="additions">
      {items.map((toast) => (
        <div key={toast.id} className={`toast toast--${toast.tone}`} role={toast.tone === 'error' ? 'alert' : 'status'}>
          <div className="toast__body">
            <span className="toast__title">{toast.title}</span>
            {toast.text && <span className="toast__text">{toast.text}</span>}
            {toast.link && (
              <Link to={toast.link.to} onClick={() => toasts.dismiss(toast.id)}>
                {toast.link.label}
              </Link>
            )}
          </div>
          <button type="button" className="icon-button" aria-label="Dismiss notification" onClick={() => toasts.dismiss(toast.id)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
