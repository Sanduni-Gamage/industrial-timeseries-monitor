/**
 * Time-series chart.
 *
 * Chart decisions, and why:
 *
 * - **One y-axis, always.** Never a second scale for a second measure. Two scales make
 *   their alignment arbitrary, which invents a correlation the data does not contain.
 *   Reading and rolling average share a scale because they share a unit; anything else
 *   gets its own chart.
 * - **Two series maximum, from fixed palette slots.** Colour follows the entity, not its
 *   position in a list, so toggling a series never repaints the other one.
 * - **A legend whenever more than one series is drawn**, so identity is never carried by
 *   colour alone.
 * - **Solid hairline gridlines.** Dashed gridlines read as "threshold" or "projection"
 *   when they are just a grid.
 * - **Crosshair and tooltip by default.** An HTML chart is interactive; the axis cannot
 *   carry per-point values, and labelling every point is unreadable.
 * - **Reference lines are labelled.** A documented setpoint drawn without a label is
 *   just a mysterious line.
 */

import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import "./chart.css";

export interface ChartPoint {
  t: number;
  value: number | null;
  rolling?: number | null;
  /** Present only on points the detectors flagged; drives the anomaly overlay. */
  anomaly?: number | null;
  anomalySeverity?: string;
  sampleCount?: number;
}

interface TrendChartProps {
  points: ChartPoint[];
  unit?: string | null;
  seriesLabel: string;
  rollingLabel?: string;
  /** A setpoint published by the equipment owner, e.g. the 7 bar low-pressure switch. */
  referenceValue?: number | null;
  referenceLabel?: string;
  /** Expected band from the stored baseline, drawn behind the data. */
  bandLow?: number | null;
  bandHigh?: number | null;
  height?: number;
}

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const fullFormatter = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

/** Recharts calls these with the data extent; the closure keeps the pair symmetric. */
let lastExtent: { min: number; max: number } = { min: 0, max: 1 };

function paddedBound(value: number, which: "min" | "max"): number {
  if (which === "min") lastExtent.min = value;
  else lastExtent.max = value;

  const span = lastExtent.max - lastExtent.min;
  // A flat series has no range to pad; give it a small absolute margin so the line
  // does not sit exactly on the frame.
  const pad = span > 0 ? span * 0.08 : Math.max(Math.abs(value) * 0.05, 0.5);
  const bound = which === "min" ? value - pad : value + pad;

  // Round to something a tick label can show cleanly.
  const magnitude = Math.max(Math.abs(bound), 1e-6);
  const decimals = magnitude < 1 ? 3 : magnitude < 10 ? 2 : 1;
  return Number(bound.toFixed(decimals));
}

function readVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

export function TrendChart({
  points,
  unit,
  seriesLabel,
  rollingLabel,
  referenceValue,
  referenceLabel,
  bandLow,
  bandHigh,
  height = 320,
}: TrendChartProps) {
  const ink = readVar("--ink-muted", "#898781");
  const grid = readVar("--grid", "#e1e0d9");
  const axis = readVar("--axis", "#c3c2b7");
  const series1 = readVar("--series-1", "#2a78d6");
  const series2 = readVar("--series-2", "#eb6834");
  const critical = readVar("--status-critical", "#d03b3b");
  const surface = readVar("--surface", "#fcfcfb");

  const hasRolling = points.some((p) => p.rolling !== null && p.rolling !== undefined);
  const hasAnomalies = points.some((p) => p.anomaly !== null && p.anomaly !== undefined);
  const showLegend = hasRolling || hasAnomalies;

  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={points} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
          {/* The expected band sits behind everything, as context rather than a mark. */}
          {bandLow != null && bandHigh != null && (
            <ReferenceArea
              y1={bandLow}
              y2={bandHigh}
              fill={series1}
              fillOpacity={0.07}
              stroke="none"
            />
          )}

          <CartesianGrid stroke={grid} strokeDasharray="0" vertical={false} />

          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            tickFormatter={(value: number) => dateFormatter.format(new Date(value))}
            stroke={axis}
            tick={{ fill: ink, fontSize: 11 }}
            tickLine={false}
            minTickGap={48}
          />
          {/* The y-axis fits the data rather than starting at zero.
           *
           * On a BAR chart a truncated axis is a lie: bar length encodes magnitude, so
           * cutting the baseline exaggerates differences. On a LINE chart of a continuous
           * physical quantity it is the correct default - pressure sitting between 8.5
           * and 9.7 bar plotted from zero wastes three quarters of the plot and flattens
           * exactly the variation the operator is looking for. Temperature and pressure
           * charts have worked this way in process monitoring forever.
           *
           * Padded by 8% of the observed range so the line never touches the frame. */}
          <YAxis
            stroke={axis}
            tick={{ fill: ink, fontSize: 11 }}
            tickLine={false}
            width={56}
            domain={[
              (min: number) => paddedBound(min, "min"),
              (max: number) => paddedBound(max, "max"),
            ]}
            allowDecimals
            label={
              unit
                ? { value: unit, angle: -90, position: "insideLeft", fill: ink, fontSize: 11 }
                : undefined
            }
          />

          <Tooltip
            content={<ChartTooltip unit={unit} seriesLabel={seriesLabel} rollingLabel={rollingLabel} />}
            cursor={{ stroke: axis, strokeWidth: 1 }}
          />

          {referenceValue != null && (
            <ReferenceLine
              y={referenceValue}
              stroke={ink}
              strokeWidth={1}
              label={{
                value: referenceLabel ?? `${referenceValue}`,
                position: "insideTopRight",
                fill: ink,
                fontSize: 11,
              }}
            />
          )}

          <Line
            type="monotone"
            dataKey="value"
            name={seriesLabel}
            stroke={series1}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, strokeWidth: 2, stroke: surface }}
            isAnimationActive={false}
            connectNulls={false}
          />

          {hasRolling && (
            <Line
              type="monotone"
              dataKey="rolling"
              name={rollingLabel ?? "Rolling average"}
              stroke={series2}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          )}

          {hasAnomalies && (
            /* A status colour, because these points mean "flagged", not "series 3".
               The 2px surface ring keeps overlapping markers separable. */
            <Scatter
              dataKey="anomaly"
              name="Flagged"
              fill={critical}
              stroke={surface}
              strokeWidth={2}
              shape="circle"
              isAnimationActive={false}
            />
          )}

          {showLegend && (
            <Legend
              verticalAlign="top"
              align="left"
              height={28}
              iconType="plainline"
              wrapperStyle={{ fontSize: 12, color: ink, paddingBottom: 4 }}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

interface TooltipProps {
  active?: boolean;
  payload?: Array<{ dataKey?: string | number; value?: number; payload?: ChartPoint }>;
  unit?: string | null;
  seriesLabel: string;
  rollingLabel?: string;
}

function ChartTooltip({ active, payload, unit, seriesLabel, rollingLabel }: TooltipProps) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload;
  if (!point) return null;

  const format = (value: number | null | undefined) =>
    value === null || value === undefined
      ? "-"
      : `${value.toLocaleString(undefined, { maximumFractionDigits: 3 })}${unit ? ` ${unit}` : ""}`;

  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-time">{fullFormatter.format(new Date(point.t))}</div>
      <dl>
        <div>
          <dt>
            <span className="swatch" style={{ background: "var(--series-1)" }} aria-hidden="true" />
            {seriesLabel}
          </dt>
          <dd className="tabular">{format(point.value)}</dd>
        </div>
        {point.rolling != null && (
          <div>
            <dt>
              <span className="swatch" style={{ background: "var(--series-2)" }} aria-hidden="true" />
              {rollingLabel ?? "Rolling average"}
            </dt>
            <dd className="tabular">{format(point.rolling)}</dd>
          </div>
        )}
        {point.sampleCount != null && (
          <div>
            <dt>Samples in bucket</dt>
            <dd className="tabular">{point.sampleCount.toLocaleString()}</dd>
          </div>
        )}
        {point.anomaly != null && (
          <div>
            <dt>
              <span
                className="swatch"
                style={{ background: "var(--status-critical)" }}
                aria-hidden="true"
              />
              Flagged
            </dt>
            <dd>{point.anomalySeverity ?? "yes"}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}
