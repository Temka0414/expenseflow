import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Notice } from '../components/ui';

interface State {
  error: Error | null;
}

/**
 * Last line of defence for rendering bugs and failed lazy-chunk loads: shows a recoverable message
 * instead of a blank page. (React still needs a class component for this.)
 */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey: string }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(previous: { resetKey: string }): void {
    // Navigating elsewhere clears the error, so one broken page does not take the whole app down.
    if (this.state.error && previous.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Unhandled UI error', error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <Notice tone="danger" title="This page failed to load">
        <span>{this.state.error.message}</span>
        <span>
          <button type="button" className="btn btn-sm" onClick={() => window.location.reload()}>
            Reload the app
          </button>
        </span>
      </Notice>
    );
  }
}
