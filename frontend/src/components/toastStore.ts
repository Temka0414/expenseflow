/**
 * Toast notifications as a tiny external store (read with `useSyncExternalStore`), so anything - a
 * page, the live-event listener, an upload callback - can raise one without prop drilling.
 */
import { useSyncExternalStore } from 'react';

export type ToastTone = 'info' | 'success' | 'warning' | 'error';

export interface ToastInput {
  tone?: ToastTone;
  title: string;
  text?: string;
  /** In-app route to open, e.g. `/expenses/01J...`. */
  link?: { to: string; label: string };
}

export interface Toast extends ToastInput {
  id: number;
  tone: ToastTone;
}

const VISIBLE_MS: Record<ToastTone, number> = { info: 6_000, success: 6_000, warning: 9_000, error: 10_000 };
const MAX_TOASTS = 4;

class ToastStore {
  #toasts: readonly Toast[] = [];
  #listeners = new Set<() => void>();
  #nextId = 1;

  push(input: ToastInput): number {
    const toast: Toast = { ...input, tone: input.tone ?? 'info', id: this.#nextId++ };
    this.#set([...this.#toasts, toast].slice(-MAX_TOASTS));
    setTimeout(() => this.dismiss(toast.id), VISIBLE_MS[toast.tone]);
    return toast.id;
  }

  dismiss(id: number): void {
    if (this.#toasts.some((toast) => toast.id === id)) this.#set(this.#toasts.filter((toast) => toast.id !== id));
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  readonly getSnapshot = (): readonly Toast[] => this.#toasts;

  #set(toasts: readonly Toast[]): void {
    this.#toasts = toasts;
    for (const listener of this.#listeners) listener();
  }
}

export const toasts = new ToastStore();

export function useToasts(): readonly Toast[] {
  return useSyncExternalStore(toasts.subscribe, toasts.getSnapshot);
}
