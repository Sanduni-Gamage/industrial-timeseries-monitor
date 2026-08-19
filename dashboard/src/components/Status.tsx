/**
 * Status indicators and stat tiles.
 *
 * The rule these enforce: status is never colour alone. Every badge carries a glyph
 * and a word as well as a colour, so it survives colour-vision deficiency, greyscale
 * printing and forced-colours mode. Two of the four status colours sit below 3:1 contrast
 * on the light surface by design - the icon-plus-label pairing is what makes that safe.
 */

import type { ReactNode } from "react";

import "./status.css";

export type StatusKind = "NORMAL" | "WARNING" | "CRITICAL" | "NO DATA" | "UNKNOWN";

const GLYPH: Record<StatusKind, string> = {
  NORMAL: "✓",
  WARNING: "!",
  CRITICAL: "✕",
  "NO DATA": "-",
  UNKNOWN: "?",
};

/** Operator-facing wording. "Healthy" reads better than "NORMAL" on a status line. */
const LABEL: Record<StatusKind, string> = {
  NORMAL: "Healthy",
  WARNING: "Needs attention",
  CRITICAL: "Action required",
  "NO DATA": "No data",
  UNKNOWN: "Unknown",
};

const CLASS: Record<StatusKind, string> = {
  NORMAL: "good",
  WARNING: "warning",
  CRITICAL: "critical",
  "NO DATA": "neutral",
  UNKNOWN: "neutral",
};

function normalise(value: string | null | undefined): StatusKind {
  if (!value) return "UNKNOWN";
  const upper = value.toUpperCase();
  if (upper === "NORMAL" || upper === "WARNING" || upper === "CRITICAL") return upper;
  if (upper === "NO DATA") return "NO DATA";
  return "UNKNOWN";
}

export function StatusBadge({
  status,
  label,
  size = "md",
}: {
  status: string | null | undefined;
  /** Override the operator-facing wording, e.g. to keep severity terms in a table. */
  label?: string;
  size?: "sm" | "md";
}) {
  const kind = normalise(status);
  return (
    <span className={`status status-${CLASS[kind]} status-${size}`}>
      <span className="status-glyph" aria-hidden="true">
        {GLYPH[kind]}
      </span>
      {label ?? LABEL[kind]}
    </span>
  );
}

/** Severity as it appears in an anomaly table - keeps the raw term, still icon + label. */
export function SeverityBadge({ severity }: { severity: string }) {
  const kind = normalise(severity);
  return (
    <span className={`status status-${CLASS[kind]} status-sm`}>
      <span className="status-glyph" aria-hidden="true">
        {GLYPH[kind]}
      </span>
      {severity.charAt(0) + severity.slice(1).toLowerCase()}
    </span>
  );
}

export function StatTile({
  label,
  value,
  note,
  tone = "neutral",
  href,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  tone?: "neutral" | "good" | "warning" | "critical";
  href?: string;
}) {
  const content = (
    <>
      <span className="tile-label">{label}</span>
      {/* Proportional figures on a standalone number; tabular is for aligned columns. */}
      <span className={`tile-value tone-${tone}`}>{value}</span>
      {note && <span className="tile-note">{note}</span>}
    </>
  );

  return href ? (
    <a className="tile tile-link" href={href}>
      {content}
    </a>
  ) : (
    <div className="tile">{content}</div>
  );
}

/**
 * A labelled bar showing one share of a whole.
 *
 * Used instead of a pie or donut: this is one proportion, and a one-value chart is a
 * stat tile with a bar, not a pie chart.
 */
export function ShareBar({
  value,
  tone = "neutral",
  label,
}: {
  value: number;
  tone?: "neutral" | "good" | "warning" | "critical";
  label: string;
}) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div
      className="sharebar"
      role="img"
      aria-label={`${label}: ${clamped.toFixed(1)} percent`}
      title={`${clamped.toFixed(1)}%`}
    >
      <div className={`sharebar-fill tone-${tone}`} style={{ width: `${clamped}%` }} />
    </div>
  );
}
