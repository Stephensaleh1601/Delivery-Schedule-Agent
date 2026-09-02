"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface Resource<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** True only on the first load, so a refresh does not blank out the screen. */
  initialising: boolean;
  reload: () => void;
  setData: (value: T) => void;
}

/**
 * Fetch-on-mount with an explicit reload.
 *
 * Kept deliberately small -- no cache, no dedupe. Every screen in this console reads live
 * dispatch state, and showing a stale plan after a replan would be worse than a re-fetch.
 *
 * `setData` exists so a screen can fold in a response it already has (an acceptance returns the
 * new state) rather than round-tripping again. It never invents a result the backend has not
 * confirmed.
 */
export function useResource<T>(fetcher: () => Promise<T>, deps: unknown[] = []): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const loadedOnce = useRef(false);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(fetcher, deps);

  const load = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    run()
      .then((value) => {
        if (cancelled) return;
        setData(value);
        setError(null);
        loadedOnce.current = true;
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [run]);

  useEffect(() => load(), [load]);

  return {
    data,
    error,
    loading,
    initialising: loading && !loadedOnce.current,
    reload: load,
    setData,
  };
}
