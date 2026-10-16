import { useState } from 'react';
import { ConfirmDialog } from '../components/Dialog';
import type { DemoControls } from '../platform/platform';

/** Always-visible banner in demo mode: what is simulated, the role switcher and the reset action. */
export function DemoBar({ controls }: { controls: DemoControls }) {
  const [confirming, setConfirming] = useState(false);
  const current = controls.roles.find((role) => role.id === controls.currentRoleId);

  return (
    <div className="demo-bar" role="region" aria-label="Demo mode">
      <div className="demo-bar__inner">
        <span className="demo-bar__label">
          <span className="badge badge--violet">Demo mode</span>
        </span>
        <span className="demo-bar__text">
          Everything runs in this browser tab: an in-memory backend speaks the real API contract, and Claude&apos;s
          receipt reading is simulated. {current?.description}
        </span>
        <div className="tabs" role="group" aria-label="Act as">
          {controls.roles.map((role) => (
            <button
              key={role.id}
              type="button"
              className="tab"
              aria-pressed={role.id === controls.currentRoleId}
              onClick={() => controls.switchRole(role.id)}
            >
              {role.label}
            </button>
          ))}
        </div>
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => setConfirming(true)}>
          Reset demo data
        </button>
      </div>
      <ConfirmDialog
        open={confirming}
        title="Reset the demo?"
        confirmLabel="Reset demo data"
        danger
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          controls.reset();
        }}
      >
        <p>All expenses, receipts and reports you created in demo mode are deleted and the sample data is restored.</p>
      </ConfirmDialog>
    </div>
  );
}
