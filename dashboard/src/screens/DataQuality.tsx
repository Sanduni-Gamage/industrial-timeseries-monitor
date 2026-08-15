/**
 * Data quality - how much of the archive can be trusted, and why.
 *
 * The screen leads with the thing a completeness check would miss. The source metadata
 * says the data has no missing values, and cell-wise that is true. But 3.35% of it is a
 * logger repeating its last reading: non-null, inside range, and not a measurement. That
 * distinction is the whole reason this screen exists, so it is stated in words at the top
 * rather than left implicit in a percentage.
 */

import { api } from "../api/client";
import { Panel } from "../components/Panel";
import { ShareBar, StatTile, StatusBadge } from "../components/Status";
import { useApi } from "../hooks";

export function DataQuality() {
  const quality = useApi((signal) => api.dataQuality(signal), []);
  const health = useApi((signal) => api.health(signal), []);
  const summary = useApi((signal) => api.summary(signal), []);

  const q = quality.data;

  return (
    <div className="screen">
      <div className="screen-head">
        <h2>Data quality</h2>
        <p>
          How complete, how fresh and how trustworthy the stored archive is - and what was
          done with anything that failed a check.
        </p>
      </div>

      <Panel
        title="At a glance"
        loading={quality.initialLoading}
        error={quality.error}
        onRetry={quality.reload}
        isEmpty={!q}
        emptyMessage="No readings stored."
        emptyHint="Run the ingestion pipeline to load the archive."
      >
        {q && (
          <div className="tile-grid">
            <StatTile
              label="Readings stored"
              value={q.total_readings.toLocaleString()}
              note="1,516,948 scans × 15 sensors"
            />
            <StatTile
              label="Trusted"
              value={`${q.good_pct.toFixed(2)}%`}
              tone="good"
              note={`${q.good_readings.toLocaleString()} readings`}
            />
            <StatTile
              label="Repeated (held)"
              value={`${q.held_pct.toFixed(2)}%`}
              tone="warning"
              note={`${q.held_readings.toLocaleString()} readings`}
            />
            <StatTile
              label="Timeline coverage"
              value={`${q.coverage_pct.toFixed(1)}%`}
              tone={q.coverage_pct > 90 ? "good" : "warning"}
              note={`${q.gap_count.toLocaleString()} periods with no data`}
            />
            <StatTile
              label="Rows set aside"
              value={q.quarantined_rows.toLocaleString()}
              note="kept in full, never deleted"
            />
            <StatTile
              label="Last load"
              value={
                q.last_successful_ingestion
                  ? new Date(q.last_successful_ingestion).toLocaleDateString()
                  : "never"
              }
              note={q.last_ingestion_status ?? undefined}
            />
          </div>
        )}
      </Panel>

      <Panel title="Why “complete” is not the same as “correct”">
        <div className="prose">
          <p>
            The published dataset states it has no missing values, and measured cell by
            cell that is true - there is not one empty value in{" "}
            <span className="tabular">{q?.total_readings.toLocaleString() ?? "22,754,220"}</span>{" "}
            readings.
          </p>
          <p>
            But <strong>{q?.held_pct.toFixed(2) ?? "3.35"}% of them</strong> were recorded
            while the logging device had frozen and was repeating its last reading. Every
            analogue sensor reported an identical value at the same moment - oil
            temperature steady to four decimal places for two days, while the motor current
            sat at a load value with the intake valve shut. That cannot happen on a real
            machine.
          </p>
          <p>
            Those readings pass both checks people reach for first: nothing is empty, and
            every value is inside its plausible range. Only checking whether the value ever{" "}
            <em>changes</em> finds them. They are kept and labelled rather than deleted, and
            excluded from every average and expected range on this dashboard.
          </p>
        </div>
      </Panel>

      <div className="grid-2">
        <Panel
          title="Coverage"
          subtitle="Periods where nothing was recorded at all."
          loading={quality.initialLoading}
          error={quality.error}
          isEmpty={!q}
        >
          {q && (
            <>
              <div className="quality-row-head">
                <span>Timeline with data</span>
                <span className="tabular quality-row-value">{q.coverage_pct.toFixed(1)}%</span>
              </div>
              <ShareBar
                value={q.coverage_pct}
                tone={q.coverage_pct > 90 ? "good" : "warning"}
                label="Timeline coverage"
              />
              <p className="quality-row-note">
                {q.gap_count.toLocaleString()} separate periods have no readings, the longest
                running 48 hours. Gaps are recorded and reported, never filled in with
                invented values - a straight line across a gap would look like a steady
                machine.
              </p>
            </>
          )}
        </Panel>

        <Panel
          title="Service status"
          subtitle="Whether the monitoring system itself is working."
          loading={health.initialLoading}
          error={health.error}
          onRetry={health.reload}
          isEmpty={!health.data}
          actions={
            /* While the check is still in flight the badge must not assert a verdict.
               Showing "Needs attention" before the answer arrives is worse than showing
               nothing: it reports a problem that has not been found. */
            health.initialLoading ? (
              <StatusBadge status="UNKNOWN" label="Checking…" size="sm" />
            ) : (
              <StatusBadge
                status={health.data?.status === "healthy" ? "NORMAL" : "WARNING"}
                size="sm"
              />
            )
          }
        >
          <ul className="note-list">
            {(health.data?.components ?? []).map((component) => (
              <li key={component.name}>
                <strong>{friendlyComponent(component.name)}</strong>{" "}
                <span className={component.status === "up" ? "muted" : "warn-text"}>
                  {component.status === "up" ? "working" : component.status}
                </span>
                {component.detail && <div className="muted">{component.detail}</div>}
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      <Panel
        title="What happens to a bad reading"
        subtitle="Nothing that fails a check is discarded. Each category has a defined disposition."
      >
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Problem</th>
                <th>What is done with it</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Repeated (held) value</td>
                <td className="wrap">
                  Stored and labelled. Excluded from averages and expected ranges, shown
                  separately here.
                </td>
              </tr>
              <tr>
                <td>Physically impossible value</td>
                <td className="wrap">
                  Stored and labelled as untrustworthy. Excluded from statistics, kept so
                  the record is auditable.
                </td>
              </tr>
              <tr>
                <td>No data for a period</td>
                <td className="wrap">
                  Recorded as a gap. Nothing is invented to fill it.
                </td>
              </tr>
              <tr>
                <td>Row that cannot be stored at all</td>
                <td className="wrap">
                  Set aside in full, with its original content and the reason it was
                  refused. {q?.quarantined_rows === 0 ? "None so far." : ""}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Panel>

      {summary.data?.archive_start && summary.data?.archive_end && (
        <p className="callout">
          <strong>Archive period.</strong>{" "}
          {new Date(summary.data.archive_start).toLocaleString()} to{" "}
          {new Date(summary.data.archive_end).toLocaleString()}. Readings were captured every
          second by the onboard device; the published dataset keeps every tenth one.
        </p>
      )}
    </div>
  );
}

function friendlyComponent(name: string): string {
  if (name === "sql_server") return "Database";
  if (name === "schema") return "Tables";
  if (name === "ingestion") return "Data loading";
  return name;
}
