import { useCallback, useEffect, useEffectEvent, useState } from 'react';
import type { Page } from '../api/types';
import { toApiError, type ApiError } from '../api/errors';

/**
 * Minimal data loading for this app (no cache library needed at this size).
 *
 * `key` identifies what is loaded (e.g. `expense:01J...`): when it changes the old data is hidden
 * immediately and a new request starts. `reload()` refetches in the background while the current data
 * stays on screen - that is how live WebSocket events refresh pages without flicker.
 * Responses that arrive after the component moved on (key changed / unmounted) are dropped.
 */
export interface Resource<T> {
  data: T | undefined;
  error: ApiError | null;
  /** No data for the current key yet. */
  loading: boolean;
  /** A request (first load or background refresh) is in flight. */
  refreshing: boolean;
  reload: () => void;
  /** Replace the data locally, e.g. with the expense a PATCH returned. */
  mutate: (update: (current: T | undefined) => T | undefined) => void;
}

interface ResourceState<T> {
  key: string | null;
  nonce: number;
  data: T | undefined;
  error: ApiError | null;
}

export function useResource<T>(key: string | null, load: () => Promise<T>): Resource<T> {
  const [nonce, setNonce] = useState(0);
  const [state, setState] = useState<ResourceState<T>>({ key: null, nonce: -1, data: undefined, error: null });
  const run = useEffectEvent(load);

  useEffect(() => {
    if (key === null) return;
    let cancelled = false;
    run().then(
      (data) => {
        if (!cancelled) setState({ key, nonce, data, error: null });
      },
      (error: unknown) => {
        if (cancelled) return;
        // Keep showing what we had: a failed background refresh should not blank the page.
        setState((previous) => ({
          key,
          nonce,
          data: previous.key === key ? previous.data : undefined,
          error: toApiError(error),
        }));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [key, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  const mutate = useCallback(
    (update: (current: T | undefined) => T | undefined) =>
      setState((previous) => (previous.key === key ? { ...previous, data: update(previous.data), error: null } : previous)),
    [key],
  );

  const current = key !== null && state.key === key;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : null,
    loading: key !== null && !current,
    refreshing: key !== null && (!current || state.nonce !== nonce),
    reload,
    mutate,
  };
}

/**
 * Cursor pagination over `{items, nextCursor}` responses (DynamoDB `LastEvaluatedKey` behind the scenes):
 * the first page loads with the key, `loadMore()` appends the next one. A refresh refetches as many items
 * as are currently shown (max 100 = the API's page limit) so "Load more" progress survives live updates.
 */
export interface PagedList<T> {
  items: T[];
  error: ApiError | null;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
  /** Remove items locally (optimistic update), e.g. after deciding on an approval. */
  remove: (predicate: (item: T) => boolean) => void;
}

interface PagedState<T> {
  key: string | null;
  nonce: number;
  items: T[];
  nextCursor: string | null;
  error: ApiError | null;
  loadingMore: boolean;
}

const MAX_PAGE = 100;

export function usePagedList<T>(
  key: string,
  loadPage: (cursor: string | null, limit: number) => Promise<Page<T>>,
  pageSize = 25,
): PagedList<T> {
  const [nonce, setNonce] = useState(0);
  const [state, setState] = useState<PagedState<T>>({
    key: null,
    nonce: -1,
    items: [],
    nextCursor: null,
    error: null,
    loadingMore: false,
  });
  const fetchPage = useEffectEvent(loadPage);
  const refreshLimit = useEffectEvent(() =>
    state.key === key ? Math.min(MAX_PAGE, Math.max(pageSize, state.items.length)) : pageSize,
  );

  useEffect(() => {
    let cancelled = false;
    fetchPage(null, refreshLimit()).then(
      (page) => {
        if (!cancelled) {
          setState({ key, nonce, items: page.items, nextCursor: page.nextCursor, error: null, loadingMore: false });
        }
      },
      (error: unknown) => {
        if (cancelled) return;
        setState((previous) => ({
          ...(previous.key === key ? previous : { items: [], nextCursor: null }),
          key,
          nonce,
          error: toApiError(error),
          loadingMore: false,
        }));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [key, nonce]);

  const current = state.key === key;

  const loadMore = useCallback(() => {
    if (!current || state.loadingMore || state.nextCursor === null) return;
    const cursor = state.nextCursor;
    setState((previous) => ({ ...previous, loadingMore: true }));
    loadPage(cursor, pageSize).then(
      (page) =>
        setState((previous) =>
          previous.key === key && previous.nextCursor === cursor
            ? { ...previous, items: [...previous.items, ...page.items], nextCursor: page.nextCursor, loadingMore: false }
            : previous,
        ),
      (error: unknown) => setState((previous) => ({ ...previous, error: toApiError(error), loadingMore: false })),
    );
  }, [current, key, loadPage, pageSize, state.loadingMore, state.nextCursor]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  const remove = useCallback(
    (predicate: (item: T) => boolean) =>
      setState((previous) => ({ ...previous, items: previous.items.filter((item) => !predicate(item)) })),
    [],
  );

  return {
    items: current ? state.items : [],
    error: current ? state.error : null,
    loading: !current,
    loadingMore: current && state.loadingMore,
    hasMore: current && state.nextCursor !== null,
    loadMore,
    reload,
    remove,
  };
}
