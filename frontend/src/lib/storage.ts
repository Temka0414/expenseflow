/**
 * Best-effort localStorage. Every access is wrapped in try/catch: storage can be missing (tests, some
 * embedded browsers), blocked by privacy settings, or full (QuotaExceededError). Callers must work
 * without it - it only remembers conveniences such as demo data and UI preferences.
 */

export function browserStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function readJson<T>(storage: Storage | null, key: string): T | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(key);
    return raw === null ? null : (JSON.parse(raw) as T);
  } catch {
    return null;
  }
}

/** Returns false when the value could not be stored (quota exceeded or storage unavailable). */
export function writeJson(storage: Storage | null, key: string, value: unknown): boolean {
  return writeString(storage, key, JSON.stringify(value));
}

export function readString(storage: Storage | null, key: string): string | null {
  if (!storage) return null;
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

export function writeString(storage: Storage | null, key: string, value: string): boolean {
  if (!storage) return false;
  try {
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function removeKeys(storage: Storage | null, predicate: (key: string) => boolean): void {
  if (!storage) return;
  try {
    const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index)).filter(
      (key): key is string => key !== null && predicate(key),
    );
    for (const key of keys) storage.removeItem(key);
  } catch {
    // Storage became unavailable: nothing left to clean up.
  }
}
