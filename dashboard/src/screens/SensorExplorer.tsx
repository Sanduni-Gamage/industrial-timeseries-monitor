/**
 * Sensor explorer - pick a sensor and a period, see what it did.
 *
 * The server chooses the resolution and the UI states it. Seven months at 10-second
 * resolution is 1.8 million points, so the API returns daily buckets and says so.
 *
 * The operating-state filter is prominent because this compressor is off 55% of the
 * time, and a chart mixing idle and running periods describes the duty cycle rather than
 * the sensor.
 */

import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, type OperatingState } from "../api/client";
import { Panel } from "../components/Panel";
import { TimeRange } from "../components/TimeRange";
import { rangeEndingAt, type Range } from "../components/timeRange.utils";
import { TrendChart, type ChartPoint } from "../components/TrendChart";
import { useApi, useDebounced } from "../hooks";

const STATES: Array<{ value: "" | OperatingState; label: string }> = [
  { value: "", label: "All (mixed)" },
  { value: "OFF", label: "Off" },
  { value: "OFFLOADED", label: "Running, unloaded" },
  { value: "LOADED", label: "Running, under load" },
];

export function SensorExplorer() {
  const [params, setParams] = useSearchParams();
  const sensors = useApi((signal) => api.sensors(undefined, signal), []);
  const summary = useApi((signal) => api.summary(signal), []);

  const selected = params.get("sensor") ?? "TP3";
  const state = (params.get("state") ?? "") as "" | OperatingState;

  const archiveEnd = summary.data?.archive_end ?? null;
  const archiveStart = summary.data?.archive_start ?? null;

  const [chosenRange, setChosenRange] = useState<Range | null>(null);
  const [preset, setPreset] = useState<number | null>(24 * 7);

  // Derived during render, and anchored to the archive's last reading rather than to
  // today, since a "last 7 days" default over 2020 data would be empty.
  //
  // useMemo is load-bearing, not an optimisation: without it the object has a new
  // identity every render, which resets useDebounced's timer, whose setState renders
  // again. See DEV_LOG DL-041.
  const range: Range | null = useMemo(
    () => chosenRange ?? (archiveEnd ? rangeEndingAt(archiveEnd, 24 * 7) : null),
    [chosenRange, archiveEnd],
  );

  const setRange = setChosenRange;
  const debounced = useDebounced(range, 400);

  const readings = useApi(
    (signal) =>
      debounced
        ? api.readings(selected, { start: debounced.start, end: debounced.end }, signal)
        : Promise.resolve(null as never),
    [selected, debounced?.start, debounced?.end],
  );

  const trend = useApi(
    (signal) =>
      debounced
        ? api.trend(
            selected,
            {
              start: debounced.start,
              end: debounced.end,
              operating_state: state || undefined,
              window_hours: 24,
            },
            signal,
          )
        : Promise.resolve(null as never),
    [selected, state, debounced?.start, debounced?.end],
  );

  const sensor = sensors.data?.find((s) => s.sensor_code === selected);

  const readingPoints: ChartPoint[] = useMemo(() => {
    const series = readings.data;
    if (!series) return [];
    if (series.points?.length) {
      return series.points.map((p) => ({ t: new Date(p.timestamp).getTime(), value: p.value }));
    }
    return (series.aggregates ?? []).map((p) => ({
      t: new Date(p.timestamp).getTime(),
      value: p.value,
      sampleCount: p.sample_count,
    }));
  }, [readings.data]);

  const trendPoints: ChartPoint[] = useMemo(
    () =>
      (trend.data?.points ?? []).map((p) => ({
        t: new Date(p.timestamp).getTime(),
        value: p.value,
        rolling: p.rolling_mean ?? null,
        sampleCount: p.sample_count,
      })),
    [trend.data],
  );

  const applyPreset = (hours: number) => {
    if (!archiveEnd) return;
    setPreset(hours);
    setRange(rangeEndingAt(archiveEnd, hours, archiveStart ?? undefined));
  };

  return (
    <div className="screen">
      <div className="screen-head">
        <h2>Sensor explorer</h2>
        <p>
          Look at how one sensor behaved over a chosen period. The chart resolution is
          chosen to suit the length of the period and is always stated below the chart.
        </p>
      </div>

      <Panel
        title="Selection"
        loading={sensors.initialLoading}
        error={sensors.error}
        onRetry={sensors.reload}
      >
        <div className="selector-row">
          <label className="field">
            <span>Sensor</span>
            <select
              value={selected}
              onChange={(event) => {
                params.set("sensor", event.target.value);
                setParams(params, { replace: true });
              }}
            >
              {(sensors.data ?? []).map((s) => (
                <option key={s.sensor_id} value={s.sensor_code}>
                  {s.sensor_code} - {s.sensor_name}
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span>Machine state</span>
            <select
              value={state}
              onChange={(event) => {
                if (event.target.value) params.set("state", event.target.value);
                else params.delete("state");
                setParams(params, { replace: true });
              }}
            >
              {STATES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          {range && (
            <TimeRange
              range={range}
              onChange={(next) => {
                setPreset(null);
                setRange(next);
              }}
              archiveStart={archiveStart ?? undefined}
              archiveEnd={archiveEnd ?? undefined}
              activePreset={preset}
              onPreset={applyPreset}
            />
          )}
        </div>

        {sensor && (
          <p className="sensor-blurb">
            <strong>{sensor.sensor_name}</strong>
            {sensor.unit ? ` · measured in ${sensor.unit}` : ""} ·{" "}
            <span className="muted">{sensor.description}</span>
          </p>
        )}
      </Panel>

      <Panel
        title="Readings"
        subtitle={
          readings.data
            ? `${readings.data.point_count.toLocaleString()} points at ${resolutionLabel(readings.data.resolution)} resolution.` +
              (readings.data.truncated ? " Result limit reached - narrow the period to see everything." : "")
            : "Values recorded over the selected period."
        }
        loading={readings.initialLoading || !range}
        error={readings.error}
        onRetry={readings.reload}
        isEmpty={readingPoints.length === 0}
        emptyMessage="No readings in this period."
        emptyHint="The archive has gaps totalling about 18% of the timeline. Try a wider period."
        minBodyHeight={360}
      >
        <TrendChart
          points={readingPoints}
          unit={sensor?.unit}
          seriesLabel={selected}
          referenceValue={sensor?.documented_setpoint ?? null}
          referenceLabel={
            sensor?.documented_setpoint != null
              ? `Manufacturer setpoint ${sensor.documented_setpoint} ${sensor.unit ?? ""}`.trim()
              : undefined
          }
        />
        {readings.data?.truncated && (
          <p className="callout" role="status">
            <strong>Not the whole period.</strong> The result limit was reached, so this chart
            shows only the beginning of the selected range.
          </p>
        )}
      </Panel>

      <Panel
        title="Trend"
        subtitle={
          state
            ? `Hourly averages while the machine was ${STATES.find((s) => s.value === state)?.label.toLowerCase()}, with a 24-hour rolling average.`
            : "Hourly averages with a 24-hour rolling average. Choose a machine state above to compare like with like."
        }
        loading={trend.initialLoading || !range}
        error={trend.error}
        onRetry={trend.reload}
        isEmpty={trendPoints.length === 0}
        emptyMessage="No hourly data in this period."
        emptyHint={
          state
            ? "The machine may not have been in this state during the selected period."
            : "Try a wider period."
        }
        minBodyHeight={360}
      >
        <TrendChart
          points={trendPoints}
          unit={sensor?.unit}
          seriesLabel={`${selected} hourly average`}
          rollingLabel="24-hour rolling average"
        />
        {!state && (
          <p className="callout">
            <strong>Why this line looks jagged.</strong> The compressor is switched off for
            about 55% of the archive, so a mixed view swings between idle and running values.
            Selecting a machine state above compares the sensor against itself doing the same
            job.
          </p>
        )}
      </Panel>
    </div>
  );
}

function resolutionLabel(resolution: string): string {
  if (resolution === "raw") return "full (every reading)";
  if (resolution === "hourly") return "hourly average";
  if (resolution === "daily") return "daily average";
  return resolution;
}
