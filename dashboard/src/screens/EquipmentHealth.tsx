/**
 * Equipment health - the status, what it is based on, and the failure history.
 *
 * The screen exists to make a status defensible. Anywhere it says CRITICAL it also says
 * why, over what window, and out of how much data. A status with no explanation gets
 * argued with; a status with its reasoning attached gets acted on.
 */

import { api } from "../api/client";
import { Panel } from "../components/Panel";
import { StatTile, StatusBadge } from "../components/Status";
import { useApi } from "../hooks";

export function EquipmentHealthScreen() {
  const summary = useApi((signal) => api.summary(signal), []);
  const failures = useApi((signal) => api.failures(signal), []);

  const equipment = summary.data?.equipment ?? [];

  return (
    <div className="screen">
      <div className="screen-head">
        <h2>Equipment health</h2>
        <p>
          Status is based on flagged readings in the last 24 hours of the archive. It is a
          statement about the end of the recorded period, not a running total - a status
          that only accumulates could never return to healthy.
        </p>
      </div>

      {equipment.map((item) => (
        <Panel
          key={item.equipment_id}
          title={item.equipment_name}
          subtitle={item.equipment_code}
          loading={summary.initialLoading}
          error={summary.error}
          onRetry={summary.reload}
          actions={<StatusBadge status={item.status} />}
        >
          <p className="status-reason">{item.status_reason}</p>

          <div className="tile-grid" style={{ marginTop: 16 }}>
            <StatTile label="Sensors monitored" value={item.sensor_count} />
            <StatTile
              label="Readings stored"
              value={item.total_readings.toLocaleString()}
              note="whole archive"
            />
            <StatTile
              label="Action required"
              value={item.active_critical_anomalies}
              tone={item.active_critical_anomalies > 0 ? "critical" : "good"}
              note={`last ${item.active_window_hours} h of archive`}
            />
            <StatTile
              label="Needs attention"
              value={item.active_warning_anomalies}
              tone={item.active_warning_anomalies > 0 ? "warning" : "good"}
              note={`last ${item.active_window_hours} h of archive`}
            />
            <StatTile
              label="Recorded failures"
              value={item.recorded_failures}
              note="over the whole archive"
            />
            <StatTile
              label="Last reading"
              value={
                item.last_reading_at
                  ? new Date(item.last_reading_at).toLocaleDateString()
                  : "never"
              }
              note={
                item.last_reading_at
                  ? new Date(item.last_reading_at).toLocaleTimeString()
                  : undefined
              }
            />
          </div>
        </Panel>
      ))}

      <Panel
        title="Failure history"
        subtitle="Maintenance reports published with the dataset, reproduced exactly - including their inconsistencies."
        loading={failures.initialLoading}
        error={failures.error}
        onRetry={failures.reload}
        isEmpty={(failures.data?.length ?? 0) === 0}
        emptyMessage="No failures recorded."
      >
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Ref</th>
                <th>Started</th>
                <th>Ended</th>
                <th className="num">Duration</th>
                <th>Type</th>
                <th>Severity</th>
                <th>Data before the failure</th>
              </tr>
            </thead>
            <tbody>
              {(failures.data ?? []).map((failure) => (
                <tr key={failure.failure_event_id}>
                  <td className="tabular">{failure.source_reference ?? "-"}</td>
                  <td className="tabular">{new Date(failure.start).toLocaleString()}</td>
                  <td className="tabular">{new Date(failure.end).toLocaleString()}</td>
                  <td className="num">{formatDuration(failure.duration_minutes)}</td>
                  <td>{failure.failure_type}</td>
                  <td>{failure.severity}</td>
                  <td>
                    {failure.lead_up_usability === "USABLE" ? (
                      <span className="muted">
                        Usable ({failure.lead_up_24h_scans?.toLocaleString()} scans)
                      </span>
                    ) : (
                      <span className="warn-text">
                        {failure.lead_up_24h_stale_pct?.toFixed(0)}% repeated values
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {(failures.data ?? []).some((f) => f.data_quality_note) && (
        <Panel
          title="Notes on the source records"
          subtitle="Problems found in the published failure table. Recorded, not corrected."
        >
          <ul className="note-list">
            {(failures.data ?? [])
              .filter((f) => f.data_quality_note)
              .map((f) => (
                <li key={f.failure_event_id}>
                  <strong>{f.source_reference}</strong> ({new Date(f.start).toLocaleDateString()}):{" "}
                  {f.data_quality_note}
                </li>
              ))}
          </ul>
        </Panel>
      )}

      <p className="callout">
        <strong>What this cannot tell you.</strong> The dataset documents four failures, all
        the same type, and one of them has almost no usable data in the hours beforehand.
        That is enough to describe what happened; it is not enough to predict the next one.
      </p>
    </div>
  );
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const hours = minutes / 60;
  return hours < 48 ? `${hours.toFixed(1)} h` : `${(hours / 24).toFixed(1)} d`;
}
