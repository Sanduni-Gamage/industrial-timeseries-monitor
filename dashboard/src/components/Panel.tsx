/**
 * Panel - a card that can render every state its data can be in.
 *
 * Loading, error, empty and loaded are all handled here, once, so no screen can forget
 * one. A dashboard that only renders the loaded state shows a blank rectangle when the
 * service is down, which tells an operator nothing and looks like a broken page rather
 * than an unavailable dependency.
 */

import type { ReactNode } from "react";

import { ApiError } from "../api/client";
import "./panel.css";

interface PanelProps {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  loading?: boolean;
  error?: ApiError | null;
  isEmpty?: boolean;
  emptyMessage?: string;
  emptyHint?: string;
  onRetry?: () => void;
  children: ReactNode;
  /** Fixed body height, so a panel does not jump between states. */
  minBodyHeight?: number;
}

export function Panel({
  title,
  subtitle,
  actions,
  loading = false,
  error = null,
  isEmpty = false,
  emptyMessage = "No data for this selection.",
  emptyHint,
  onRetry,
  children,
  minBodyHeight,
}: PanelProps) {
  const body = () => {
    if (error) {
      return (
        <div className="panel-state" role="alert">
          <span className="panel-state-glyph" aria-hidden="true">
            !
          </span>
          <p className="panel-state-title">{errorTitle(error)}</p>
          {/* The server's own detail, not a generic message. It is written for an
              operator and usually says what to do next. */}
          <p className="panel-state-detail">{error.message}</p>
          {onRetry && error.isTransient && (
            <button type="button" className="btn" onClick={onRetry}>
              Try again
            </button>
          )}
        </div>
      );
    }

    if (loading) {
      return (
        <div className="panel-state" aria-busy="true">
          <span className="spinner" aria-hidden="true" />
          <p className="panel-state-detail">Loading…</p>
        </div>
      );
    }

    if (isEmpty) {
      return (
        <div className="panel-state">
          <p className="panel-state-title">{emptyMessage}</p>
          {emptyHint && <p className="panel-state-detail">{emptyHint}</p>}
        </div>
      );
    }

    return children;
  };

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p className="panel-subtitle">{subtitle}</p>}
        </div>
        {actions && <div className="panel-actions">{actions}</div>}
      </header>
      <div className="panel-body" style={minBodyHeight ? { minHeight: minBodyHeight } : undefined}>
        {body()}
      </div>
    </section>
  );
}

function errorTitle(error: ApiError): string {
  if (error.status === 0) return "Cannot reach the service";
  if (error.status === 404) return "Not found";
  if (error.status === 422) return "That request cannot be answered";
  if (error.status === 503) return "Service unavailable";
  return "Something went wrong";
}
