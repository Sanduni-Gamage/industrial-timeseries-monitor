/**
 * Data-fetching hook.
 *
 * Small and dependency-free rather than a query library: one data source, no mutations,
 * no cache invalidation.
 *
 * Every fetch returns the four states a panel must render - loading, error, empty,
 * loaded. Panels that only handle "loaded" are how a dashboard shows a blank rectangle
 * when the service is down.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api/client";

export interface AsyncState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  /** True only for the first load, so a refresh does not blank out visible content. */
  initialLoading: boolean;
  reload: () => void;
}

export function useApi<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const loadedOnce = useRef(false);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);

    fetcher(controller.signal)
      .then((result) => {
        if (!active) return;
        setData(result);
        setError(null);
        loadedOnce.current = true;
      })
      .catch((cause: unknown) => {
        if (!active || (cause as Error)?.name === "AbortError") return;
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, (cause as Error)?.message ?? "Unexpected error."),
        );
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return {
    data,
    error,
    loading,
    initialLoading: loading && !loadedOnce.current,
    reload,
  };
}

/** Debounce a rapidly-changing value, so dragging a date input does not fire per keystroke. */
export function useDebounced<T>(value: T, delayMs = 350): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

/** Theme preference, persisted. Defaults to following the operating system. */
export type ThemeChoice = "system" | "light" | "dark";

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(() => {
    try {
      const stored = localStorage.getItem("theme");
      if (stored === "light" || stored === "dark" || stored === "system") return stored;
    } catch {
      /* private browsing or blocked storage - fall through to the default */
    }
    return "system";
  });

  useEffect(() => {
    const root = document.documentElement;
    if (choice === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    try {
      localStorage.setItem("theme", choice);
    } catch {
      /* not being able to remember the choice is not worth breaking the page over */
    }
  }, [choice]);

  return [choice, setChoice];
}
