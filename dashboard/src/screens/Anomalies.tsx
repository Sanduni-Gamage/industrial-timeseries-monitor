/**
 * Anomalies - a filterable, paginated list of readings outside their expected range.
 *
 * Terminology is deliberate: "flagged reading", "expected range", "how far outside",
 * never "z-score" or "MAD". The method is shown, because which rule fired is a real
 * operator question, but it is labelled in plain words.
 *
 * Every row shows the value and the range it was expected in. A flag without the
 * expectation behind it is not actionable.
 */

import { useState } from "react";

import { api, type Severity } from "../api/client";
import { Panel } from "../components/Panel";
import { SeverityBadge } from "../components/Status";
import { useApi } from "../hooks";

const PAGE_SIZE = 50;

/** Operator-facing names for the detectors, with a one-line explanation each. */
const METHODS: Array<{ value: string; label: string; help: string }> = [
  { value: "", label: "Any rule", help: "" },
  {
    value: "ADAPTIVE_MAD",
    label: "Unusual for recent behaviour",
    help: "Compares each hour against the same machine state over the previous week, so a gradual seasonal change is normal and a sudden shift is not.",
  },
  {
    value: "BASELINE_IQR",
    label: "Drifted from the reference month",
    help: "Compares against February 2020, the reference period. Useful for spotting long-term drift; less useful as an alarm, because normal behaviour changed over the year.",
  },
  {
    value: "SETPOINT",
    label: "Below a manufacturer setpoint",
    help: "Pressure fell below the 7 bar low-pressure threshold published by the equipment owner. The only rule here that is not statistical.",
  },
];

export function Anomalies() {
  const [severity, setSeverity] = useState<"" | Severity>("");
  const [method, setMethod] = useState("");
  const [offset, setOffset] = useState(0);

  const page = useApi(
    (signal) =>
      api.anomalies(
        {
          severity: severity || undefined,
          method: method || undefined,
          limit: PAGE_SIZE,
          offset,
        },
        signal,
      ),
    [severity, method, offset],
  );

  const total = page.data?.total ?? 0;
  const items = page.data?.items ?? [];
  const shownFrom = total === 0 ? 0 : offset + 1;
  const shownTo = Math.min(offset + PAGE_SIZE, total);
  const activeMethod = METHODS.find((m) => m.value === method);

  return (
    <div className="screen">
      <div className="screen-head">
        <h2>Flagged readings</h2>
        <p>
          Readings that fell outside the range expected for that sensor while the machine
          was doing the same job. Each row shows what was measured and what was expected.
        </p>
      </div>

      <Panel
        title="Filters"
        actions={
          <button
            type="button"
            className="btn"
            onClick={() => {
              setSeverity("");
              setMethod("");
              setOffset(0);
            }}
          >
            Clear
          </button>
        }
      >
        <div className="selector-row">
          <label className="field">
            <span>Importance</span>
            <select
              value={severity}
              onChange={(event) => {
                setSeverity(event.target.value as "" | Severity);
                setOffset(0);
              }}
            >
              <option value="">Any</option>
              <option value="CRITICAL">Action required</option>
              <option value="WARNING">Needs attention</option>
            </select>
          </label>

          <label className="field">
            <span>Rule that flagged it</span>
            <select
              value={method}
              onChange={(event) => {
                setMethod(event.target.value);
                setOffset(0);
              }}
            >
              {METHODS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {activeMethod?.help && <p className="callout">{activeMethod.help}</p>}
      </Panel>

      <Panel
        title="Results"
        subtitle={
          total > 0
            ? `${shownFrom.toLocaleString()}-${shownTo.toLocaleString()} of ${total.toLocaleString()}`
            : undefined
        }
        loading={page.initialLoading}
        error={page.error}
        onRetry={page.reload}
        isEmpty={items.length === 0}
        emptyMessage="Nothing matches these filters."
        emptyHint="Try clearing the filters, or widening the importance to include warnings."
        minBodyHeight={320}
      >
        <div className="table-wrap">
          <table className="data">
            <caption className="visually-hidden">
              Flagged readings, most important first
            </caption>
            <thead>
              <tr>
                <th>When</th>
                <th>Sensor</th>
                <th>Machine state</th>
                <th className="num">Reading</th>
                <th className="num">Expected range</th>
                <th className="num">How far outside</th>
                <th>Importance</th>
                <th>Rule</th>
              </tr>
            </thead>
            <tbody>
              {items.map((anomaly) => (
                <tr key={anomaly.anomaly_id}>
                  <td className="tabular">{new Date(anomaly.timestamp).toLocaleString()}</td>
                  <td>
                    <strong>{anomaly.sensor_code}</strong>
                    <span className="muted"> · {anomaly.sensor_name}</span>
                  </td>
                  <td className="muted">{stateLabel(anomaly.operating_state)}</td>
                  <td className="num">
                    {anomaly.value.toFixed(3)}
                    {anomaly.unit ? ` ${anomaly.unit}` : ""}
                  </td>
                  <td className="num muted">
                    {anomaly.expected_low != null && anomaly.expected_high != null
                      ? `${anomaly.expected_low.toFixed(3)} - ${anomaly.expected_high.toFixed(3)}`
                      : anomaly.expected_low != null
                        ? `above ${anomaly.expected_low.toFixed(2)}`
                        : "-"}
                  </td>
                  <td className="num">{anomaly.score.toFixed(1)}×</td>
                  <td>
                    <SeverityBadge severity={anomaly.severity} />
                  </td>
                  <td className="muted">{methodLabel(anomaly.method)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="pager">
          <button
            type="button"
            className="btn"
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0}
          >
            Previous
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={shownTo >= total}
          >
            Next
          </button>
          <span aria-live="polite">
            Showing {shownFrom.toLocaleString()}-{shownTo.toLocaleString()} of{" "}
            {total.toLocaleString()}
          </span>
        </div>
      </Panel>

      <p className="callout">
        <strong>How to read “how far outside”.</strong> It is a multiple, not a percentage:
        1.0× means the reading sat right on the edge of its expected range, 5.0× means it
        was five times further out than that edge. Expected ranges come from the machine’s
        own recorded behaviour, computed separately for each machine state.
      </p>
    </div>
  );
}

function stateLabel(state: string | null | undefined): string {
  if (!state) return "-";
  if (state === "OFF") return "Off";
  if (state === "OFFLOADED") return "Running, unloaded";
  if (state === "LOADED") return "Under load";
  if (state === "STARTING") return "Starting";
  return state;
}

function methodLabel(method: string): string {
  return METHODS.find((m) => m.value === method)?.label ?? method;
}
