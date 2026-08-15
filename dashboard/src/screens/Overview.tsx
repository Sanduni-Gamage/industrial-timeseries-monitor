/**
 * Overview - the screen that must answer "is this machine healthy, and what changed?"
 * without the reader knowing what a z-score is.
 *
 * Everything here comes from a single `/summary` call. A dashboard that fires eight
 * requests to draw one screen is slower and harder to reason about than one endpoint
 * answering the question the screen actually asks.
 *
 * Ordering is deliberate: status first, then what is wrong, then what the machine is
 * doing, then how much the data can be trusted. An operator reads top-left first.
 */

import { Link } from "react-router-dom";

import { api } from "../api/client";
import { Panel } from "../components/Panel";
import { SeverityBadge, ShareBar, StatTile, StatusBadge } from "../components/Status";
import { useApi } from "../hooks";

const timeFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function Overview() {
  const { data, error, initialLoading, reload } = useApi((signal) => api.summary(signal), []);

  const equipment = data?.equipment?.[0];
  const quality = data?.data_quality;
  const anomalies = data?.active_anomalies ?? [];
  const criticalCount = anomalies.filter((a) => a.severity === "CRITICAL").length;

  return (
    <div className="screen">
      <div className="screen-head">
        <h2>Overview</h2>
        <p>
          Condition of the air production unit at the end of the recorded archive
          {data?.archive_end ? ` (${new Date(data.archive_end).toLocaleString()})` : ""}.
        </p>
      </div>

      <Panel
        title="Equipment status"
        subtitle="Based on flagged readings in the last 24 hours of the archive, not its whole history."
        loading={initialLoading}
        error={error}
        onRetry={reload}
        isEmpty={!equipment}
        emptyMessage="No equipment configured."
        emptyHint="Run the database seed script to register the air production unit."
      >
        {equipment && (
          <div className="status-row">
            <div className="status-headline">
              <StatusBadge status={equipment.status} />
              <div>
                <p className="status-name">{equipment.equipment_name}</p>
                {/* The reason is the point. A status an operator cannot act on is not a status. */}
                <p className="status-reason">{equipment.status_reason}</p>
              </div>
            </div>

            <div className="tile-grid">
              <StatTile
                label="Sensors"
                value={equipment.sensor_count}
                note="7 analogue, 8 digital"
              />
              <StatTile
                label="Readings stored"
                value={equipment.total_readings.toLocaleString()}
                note="whole archive"
              />
              <StatTile
                label="Needs action now"
                value={equipment.active_critical_anomalies}
                tone={equipment.active_critical_anomalies > 0 ? "critical" : "good"}
                note="critical, last 24 h"
                href="#/anomalies"
              />
              <StatTile
                label="Worth a look"
                value={equipment.active_warning_anomalies}
                tone={equipment.active_warning_anomalies > 0 ? "warning" : "good"}
                note="warnings, last 24 h"
              />
            </div>
          </div>
        )}
      </Panel>

      <div className="grid-2">
        <Panel
          title="What needs attention"
          subtitle={
            criticalCount > 0
              ? `${criticalCount} reading${criticalCount === 1 ? "" : "s"} outside the expected range.`
              : "Readings outside their expected range in the last 24 hours."
          }
          loading={initialLoading}
          error={error}
          onRetry={reload}
          isEmpty={anomalies.length === 0}
          emptyMessage="Nothing flagged in the last 24 hours."
          emptyHint="Sensor values stayed inside their expected ranges."
          actions={
            <Link className="btn" to="/anomalies">
              View all
            </Link>
          }
          minBodyHeight={220}
        >
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Sensor</th>
                  <th className="num">Reading</th>
                  <th className="num">Expected</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {anomalies.slice(0, 8).map((anomaly) => (
                  <tr key={anomaly.anomaly_id}>
                    <td className="tabular">{timeFormat.format(new Date(anomaly.timestamp))}</td>
                    <td>{anomaly.sensor_code}</td>
                    <td className="num">
                      {anomaly.value.toFixed(3)}
                      {anomaly.unit ? ` ${anomaly.unit}` : ""}
                    </td>
                    <td className="num muted">
                      {anomaly.expected_low != null && anomaly.expected_high != null
                        ? `${anomaly.expected_low.toFixed(2)} - ${anomaly.expected_high.toFixed(2)}`
                        : "-"}
                    </td>
                    <td>
                      <SeverityBadge severity={anomaly.severity} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="Current sensor values"
          subtitle="The last reading recorded for each sensor."
          loading={initialLoading}
          error={error}
          onRetry={reload}
          isEmpty={(data?.latest_readings?.length ?? 0) === 0}
          emptyMessage="No readings stored."
          emptyHint="Run the ingestion pipeline to load the archive."
          minBodyHeight={220}
        >
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Sensor</th>
                  <th className="num">Value</th>
                  <th>Quality</th>
                </tr>
              </thead>
              <tbody>
                {(data?.latest_readings ?? []).map((reading) => (
                  <tr key={reading.sensor_id}>
                    <td>
                      <Link to={`/sensors?sensor=${reading.sensor_code}`}>
                        {reading.sensor_code}
                      </Link>
                      <span className="muted"> · {reading.sensor_name}</span>
                    </td>
                    <td className="num">
                      {reading.sensor_class === "Digital"
                        ? reading.value === 1
                          ? "On"
                          : "Off"
                        : `${reading.value.toFixed(3)}${reading.unit ? ` ${reading.unit}` : ""}`}
                    </td>
                    <td>
                      <span className={reading.quality_usable ? "muted" : "warn-text"}>
                        {reading.quality === "GOOD" ? "Good" : reading.quality.replace(/_/g, " ")}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>

      <div className="grid-2">
        <Panel
          title="Data you can trust"
          subtitle="How complete and how fresh the stored archive is."
          loading={initialLoading}
          error={error}
          onRetry={reload}
          isEmpty={!quality}
          actions={
            <Link className="btn" to="/quality">
              Details
            </Link>
          }
        >
          {quality && (
            <div className="quality-rows">
              <QualityRow
                label="Trusted readings"
                value={`${quality.good_pct.toFixed(2)}%`}
                share={quality.good_pct}
                tone="good"
                note={`${quality.good_readings.toLocaleString()} of ${quality.total_readings.toLocaleString()}`}
              />
              <QualityRow
                label="Repeated (held) readings"
                value={`${quality.held_pct.toFixed(2)}%`}
                share={quality.held_pct}
                tone="warning"
                note="Recorded while the logger was repeating its last value - plausible numbers, not fresh measurements."
              />
              <QualityRow
                label="Timeline coverage"
                value={`${quality.coverage_pct.toFixed(1)}%`}
                share={quality.coverage_pct}
                tone={quality.coverage_pct > 90 ? "good" : "warning"}
                note={`${quality.gap_count.toLocaleString()} periods with no data at all`}
              />
              <div className="quality-foot">
                <span className="muted">Last successful load</span>
                <span className="tabular">
                  {quality.last_successful_ingestion
                    ? new Date(quality.last_successful_ingestion).toLocaleString()
                    : "never"}
                </span>
              </div>
            </div>
          )}
        </Panel>

        <Panel
          title="Recorded failures"
          subtitle="Maintenance reports published with the dataset."
          loading={initialLoading}
          error={error}
          onRetry={reload}
          isEmpty={(data?.recent_failures?.length ?? 0) === 0}
          emptyMessage="No failures recorded."
        >
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Type</th>
                  <th className="num">Duration</th>
                  <th>Lead-up data</th>
                </tr>
              </thead>
              <tbody>
                {(data?.recent_failures ?? []).map((failure) => (
                  <tr key={failure.failure_event_id}>
                    <td className="tabular">{new Date(failure.start).toLocaleDateString()}</td>
                    <td>{failure.failure_type}</td>
                    <td className="num">{formatMinutes(failure.duration_minutes)}</td>
                    <td>
                      {failure.lead_up_usability === "USABLE" ? (
                        <span className="muted">Usable</span>
                      ) : (
                        <span className="warn-text">
                          {failure.lead_up_usability === "UNUSABLE" ? "Mostly repeated" : "None"}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  );
}

function QualityRow({
  label,
  value,
  share,
  tone,
  note,
}: {
  label: string;
  value: string;
  share: number;
  tone: "good" | "warning" | "critical" | "neutral";
  note: string;
}) {
  return (
    <div className="quality-row">
      <div className="quality-row-head">
        <span>{label}</span>
        <span className="tabular quality-row-value">{value}</span>
      </div>
      <ShareBar value={share} tone={tone} label={label} />
      <p className="quality-row-note">{note}</p>
    </div>
  );
}

function formatMinutes(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const hours = minutes / 60;
  return hours < 48 ? `${hours.toFixed(1)} h` : `${(hours / 24).toFixed(1)} d`;
}
