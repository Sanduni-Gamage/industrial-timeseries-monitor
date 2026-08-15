/**
 * Time-range control.
 *
 * Presets are relative to the **end of the archive**, not to the wall clock. This is a
 * historical dataset ending in September 2020, so a "last 7 days" measured from today
 * would return nothing and read as a broken dashboard. The control says which anchor it
 * is using, rather than leaving the operator to work it out.
 */

import { PRESETS, type Range, toLocalInput } from "./timeRange.utils";
import "./panel.css";

interface TimeRangeProps {
  range: Range;
  onChange: (range: Range) => void;
  archiveStart?: string | null;
  archiveEnd?: string | null;
  activePreset: number | null;
  onPreset: (hours: number) => void;
}

export function TimeRange({
  range,
  onChange,
  archiveStart,
  archiveEnd,
  activePreset,
  onPreset,
}: TimeRangeProps) {
  return (
    <div className="timerange">
      <div className="btn-group" role="group" aria-label="Quick time ranges">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            className="btn"
            aria-pressed={activePreset === preset.hours}
            onClick={() => onPreset(preset.hours)}
            disabled={!archiveEnd}
          >
            {preset.label}
          </button>
        ))}
      </div>

      {/* Bounded to the archive so the picker cannot offer a period that has no data.
          Stopping an empty result before it happens beats explaining one afterwards. */}
      <label className="field">
        <span>From</span>
        <input
          type="datetime-local"
          step="1"
          value={range.start}
          min={archiveStart ? toLocalInput(new Date(archiveStart)) : undefined}
          max={archiveEnd ? toLocalInput(new Date(archiveEnd)) : undefined}
          onChange={(event) => onChange({ ...range, start: event.target.value })}
        />
      </label>
      <label className="field">
        <span>To</span>
        <input
          type="datetime-local"
          step="1"
          value={range.end}
          min={archiveStart ? toLocalInput(new Date(archiveStart)) : undefined}
          max={archiveEnd ? toLocalInput(new Date(archiveEnd)) : undefined}
          onChange={(event) => onChange({ ...range, end: event.target.value })}
        />
      </label>

      {archiveEnd && (
        <p className="timerange-anchor">
          Presets end at the last reading in the archive
          <br />
          <span className="tabular">{new Date(archiveEnd).toLocaleString()}</span>
        </p>
      )}
    </div>
  );
}
