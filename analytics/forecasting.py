"""Short-horizon forecasting baselines.

The brief calls this optional and says the purpose is to demonstrate time-series
understanding, not to build something complex. The honest demonstration here is mostly a
negative one, and that is the useful part.

Three methods, evaluated on a held-out tail:

1. **Naive (persistence)** - tomorrow equals today. The benchmark any forecast must beat
   to have earned its complexity.
2. **Moving average** - the mean of the last *k* days.
3. **Holt-Winters exponential smoothing** - level plus trend, optionally seasonal.

The target is the *daily mean of a sensor within one operating state*. Forecasting the
state-blind daily mean would mostly be forecasting the duty cycle, which is driven by
train demand this dataset does not contain - an unforecastable quantity dressed up as a
sensor prediction.

Whether any of this beats persistence is reported as measured, including when it does
not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyodbc

from ingestion.db import fetch_all

logger = logging.getLogger(__name__)

#: Days held out for evaluation. Kept small: the archive is 213 days, so a large holdout
#: would leave too little history for the smoothing methods to fit.
DEFAULT_HOLDOUT_DAYS = 21

#: Window for the moving-average baseline.
DEFAULT_MA_WINDOW = 7


@dataclass
class ForecastResult:
    """Accuracy of one method on the holdout."""

    method: str
    mae: float
    rmse: float
    mape: float | None
    n: int

    def beats(self, other: ForecastResult) -> bool:
        return self.mae < other.mae

    def improvement_over(self, other: ForecastResult) -> float:
        """Percentage reduction in MAE against *other*. Negative means worse."""
        return 100.0 * (other.mae - self.mae) / other.mae if other.mae else 0.0


def _score(method: str, actual: np.ndarray, predicted: np.ndarray) -> ForecastResult:
    errors = actual - predicted
    mae = float(np.mean(np.abs(errors)))
    rmse = float(np.sqrt(np.mean(errors ** 2)))
    # MAPE is undefined and misleading near zero, which is exactly where several of these
    # signals sit. Reported only when every actual is comfortably away from zero.
    mape = (float(np.mean(np.abs(errors / actual)) * 100.0)
            if np.all(np.abs(actual) > 1e-6) else None)
    return ForecastResult(method=method, mae=mae, rmse=rmse, mape=mape, n=int(actual.size))


def load_daily_series(
    conn: pyodbc.Connection, sensor_code: str, operating_state: str = "LOADED"
) -> pd.Series:
    """Daily mean of one sensor within one operating state, held data excluded."""
    rows = fetch_all(
        conn,
        """
        SELECT CAST(a.BucketStart AS DATE) AS D,
               SUM(CAST(a.AvgValue AS FLOAT) * a.SampleCount) / SUM(a.SampleCount)
        FROM analytics.SensorHourlyStateAgg AS a
        INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId
        WHERE s.SensorCode = ? AND a.OperatingState = ?
        GROUP BY CAST(a.BucketStart AS DATE)
        HAVING SUM(a.SampleCount) >= 100      -- a day with a handful of scans is noise
        ORDER BY D
        """,
        (sensor_code, operating_state),
    )
    if not rows:
        return pd.Series(dtype=float)
    frame = pd.DataFrame.from_records(rows, columns=["date", "value"])
    return pd.Series(
        frame["value"].astype(float).to_numpy(),
        index=pd.to_datetime(frame["date"]),
        name=f"{sensor_code}/{operating_state}",
    )


def evaluate(
    series: pd.Series,
    *,
    holdout_days: int = DEFAULT_HOLDOUT_DAYS,
    ma_window: int = DEFAULT_MA_WINDOW,
) -> list[ForecastResult]:
    """Compare the three methods on a held-out tail, one day ahead each time.

    Evaluation is **walk-forward**: at each holdout day the model may use everything
    before it and nothing after. A single fit on the whole series, scored on part of it,
    would leak the answer into the forecast and report an accuracy nobody could achieve
    in operation.
    """
    if len(series) < holdout_days + ma_window + 10:
        logger.warning("Series %s too short (%d points) to evaluate %d holdout days.",
                       series.name, len(series), holdout_days)
        return []

    values = series.to_numpy(dtype=float)
    split = len(values) - holdout_days
    actual = values[split:]

    naive_pred = values[split - 1: -1]

    ma_pred = np.array([values[i - ma_window:i].mean() for i in range(split, len(values))])

    results = [
        _score("naive_persistence", actual, naive_pred),
        _score(f"moving_average_{ma_window}d", actual, ma_pred),
    ]

    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        hw_pred = []
        for i in range(split, len(values)):
            history = values[:i]
            model = ExponentialSmoothing(history, trend="add", seasonal=None,
                                         initialization_method="estimated")
            hw_pred.append(float(model.fit(optimized=True).forecast(1)[0]))
        results.append(_score("holt_winters_additive", actual, np.array(hw_pred)))
    except Exception as exc:
        logger.warning("Holt-Winters unavailable or failed to converge: %s", exc)

    return results


def report(series: pd.Series, results: list[ForecastResult]) -> str:
    """Render the comparison, judged against the persistence benchmark."""
    if not results:
        return f"{series.name}: insufficient history to evaluate."

    naive = next((r for r in results if r.method == "naive_persistence"), None)
    lines = [
        f"{series.name}  -  {len(series)} daily points, {results[0].n}-day walk-forward holdout",
        f"{'method':<26} {'MAE':>9} {'RMSE':>9} {'MAPE':>8}   vs persistence",
        "-" * 74,
    ]
    for r in sorted(results, key=lambda x: x.mae):
        mape = f"{r.mape:.1f}%" if r.mape is not None else "n/a"
        if naive and r.method != "naive_persistence":
            delta = r.improvement_over(naive)
            verdict = f"{delta:+.1f}% MAE" + ("  (better)" if delta > 0 else "  (WORSE)")
        else:
            verdict = "benchmark"
        lines.append(f"{r.method:<26} {r.mae:>9.4f} {r.rmse:>9.4f} {mape:>8}   {verdict}")

    if naive and all(r.mae >= naive.mae for r in results if r.method != "naive_persistence"):
        lines.append("")
        lines.append("Nothing beats persistence on this series. That is the result, and it")
        lines.append("is worth stating: added model complexity here buys nothing, because")
        lines.append("the day-to-day variation is driven by demand this dataset does not")
        lines.append("contain. A more complex model would fit that noise, not explain it.")
    return "\n".join(lines)
