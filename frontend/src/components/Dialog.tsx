import { useEffect, useId, useRef, type ReactNode } from 'react';

/**
 * Modal dialog and side drawer on the native `<dialog>` element: `showModal()` gives focus trapping,
 * Escape-to-close, an inert background and the top layer for free - no portal or focus library needed.
 * Content is only rendered while open, so every opening starts with fresh state.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  variant = 'modal',
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  variant?: 'modal' | 'drawer';
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  const bodyClass = variant === 'drawer' ? 'drawer__body' : 'modal__body';
  const footerClass = variant === 'drawer' ? 'drawer__footer' : 'modal__footer';
  return (
    <dialog
      ref={ref}
      className={variant}
      aria-labelledby={titleId}
      onClose={onClose}
      onClick={(event) => {
        // A click on the dialog element itself (not its content) is a click on the backdrop.
        if (event.target === event.currentTarget) onClose();
      }}
    >
      {open && (
        <>
          <div className={variant === 'drawer' ? 'drawer__header' : 'modal__header'}>
            <h2 id={titleId}>{title}</h2>
            <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
              ×
            </button>
          </div>
          <div className={bodyClass}>{children}</div>
          {footer && <div className={footerClass}>{footer}</div>}
        </>
      )}
    </dialog>
  );
}

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title={title}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="button" className={danger ? 'btn btn-danger' : 'btn btn-primary'} onClick={onConfirm} disabled={busy}>
            {confirmLabel}
          </button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}
