"""Compress the archive with swinging-door trending and measure what it costs.

The error bound is verified on every run by reconstructing the full series from the
retained points. A violation is a bug here, not a property of the data.

Usage
-----
    python scripts/run_compression.py --sweep              # trade-off curve, writes nothing
    python scripts/run_compression.py --deviation-pct 0.1  # compress at one deviation
    python scripts/run_compression.py --sensor TP3 --sweep

Exit codes: 0 success · 1 failure · 2 nothing to compress · 3 an error bound was violated
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from analytics.compression import (  # noqa: E402
    DEFAULT_DEVIATION_PCT,
    SWEEP_DEVIATION_PCT,
    deviation_for_span,
    reconstruction_error,
    swinging_door,
)
from ingestion.config import get_settings  # noqa: E402
from ingestion.db import DatabaseError, connect, fetch_all, scalar  # noqa: E402
from ingestion.logging_setup import configure_logging  # noqa: E402

logger = logging.getLogger("compression")

EXIT_OK, EXIT_FAILURE, EXIT_NOTHING, EXIT_BOUND = 0, 1, 2, 3


def load_series(conn, sensor_id: int):
    """Read one sensor's whole series as (seconds, values, quality).

    Seconds are relative to the first reading. Absolute epoch seconds would put ~1.6e9
    into every slope calculation, where the differences that matter are ~10 - a needless
    loss of float precision.
    """
    rows = fetch_all(
        conn,
        """
        SELECT ReadingTs, Value, QualityCodeId
        FROM ts.SensorReading WHERE SensorId = ? ORDER BY ReadingTs
        """,
        (sensor_id,),
    )
    if not rows:
        return np.array([]), np.array([]), np.array([]), None

    origin = rows[0][0]
    seconds = np.fromiter(((r[0] - origin).total_seconds() for r in rows),
                          dtype=np.float64, count=len(rows))
    values = np.fromiter((float(r[1]) for r in rows), dtype=np.float64, count=len(rows))
    quality = np.fromiter((int(r[2]) for r in rows), dtype=np.int16, count=len(rows))
    return seconds, values, quality, origin


def _store(conn, sensor_id, pct, deviation, result, error, seconds, values, quality,
           origin) -> None:
    """Write the retained points and the measured cost of retaining only those."""
    from datetime import timedelta

    kept = result.kept_indices
    rows = [
        (sensor_id,
         origin + timedelta(seconds=float(seconds[i])),
         float(values[i]),
         int(quality[i]))
        for i in kept
    ]
    cursor = conn.cursor()
    cursor.fast_executemany = True
    cursor.executemany(
        "INSERT INTO ts.SensorReadingCompressed (SensorId, ReadingTs, Value, QualityCodeId) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    cursor.execute(
        """
        INSERT INTO analytics.CompressionStat
            (SensorId, DeviationPct, Deviation, OriginalCount, KeptCount,
             MaxError, MeanError, WithinBound)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        sensor_id, pct, deviation, result.original_count, result.kept_count,
        error["max_error"], error["mean_error"], 1 if error["within_bound"] else 0,
    )
    conn.commit()
    cursor.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deviation-pct", type=float, default=DEFAULT_DEVIATION_PCT,
                        help="compression deviation as a percentage of each sensor's span")
    parser.add_argument("--sweep", action="store_true",
                        help="report the trade-off across several deviations; write nothing")
    parser.add_argument("--sensor", default=None, help="limit to one sensor code")
    parser.add_argument("--store", action="store_true",
                        help="write the compressed archive and its statistics to SQL Server")
    parser.add_argument("--analogue-only", action="store_true", default=True,
                        help="skip digital tags (a 0/1 signal compresses by state change, "
                             "not by straight-line fit)")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings, logger_name="compression", log_file="compression.log")

    try:
        with connect(settings=settings) as conn:
            total = int(scalar(conn, "SELECT COUNT_BIG(*) FROM ts.SensorReading") or 0)
            if total == 0:
                logger.error("No readings stored. Run scripts\\ingest.ps1 first.")
                return EXIT_NOTHING

            sensors = fetch_all(
                conn,
                """
                SELECT s.SensorId, s.SensorCode, s.Unit, s.SensorClass,
                       mm.MinValue, mm.MaxValue
                FROM asset.Sensor AS s
                CROSS APPLY (
                    SELECT MIN(a.MinValue) AS MinValue, MAX(a.MaxValue) AS MaxValue
                    FROM analytics.SensorDailyAgg AS a WHERE a.SensorId = s.SensorId
                ) AS mm
                WHERE s.IsActive = 1
                  AND (? IS NULL OR s.SensorCode = ?)
                  AND (? = 0 OR s.SensorClass = 'Analogue')
                ORDER BY s.DisplayOrder
                """,
                (args.sensor, args.sensor, 1 if args.analogue_only else 0),
            )
            if not sensors:
                logger.error("No matching sensors.")
                return EXIT_NOTHING

            deviations = SWEEP_DEVIATION_PCT if args.sweep else (args.deviation_pct,)

            origin_lookup: dict[int, object] = {}
            if args.store and not args.sweep:
                # A fresh archive each run: compression is deterministic, so an
                # incremental write would only add a way for the two to diverge.
                cursor = conn.cursor()
                cursor.execute("TRUNCATE TABLE ts.SensorReadingCompressed")
                cursor.execute("DELETE FROM analytics.CompressionStat")
                conn.commit()
            violations = 0
            started = time.perf_counter()

            if args.sweep:
                logger.info("Compression trade-off - retained %% of raw samples")
                header = f"{'sensor':<17} {'unit':<6} " + "".join(
                    f"{p:>8}%" for p in SWEEP_DEVIATION_PCT)
                logger.info(header)
                logger.info("-" * len(header))

            totals: dict[float, list[int]] = {p: [0, 0] for p in deviations}

            for sensor_id, code, unit, _cls, low, high in sensors:
                seconds, values, quality, origin = load_series(conn, int(sensor_id))
                if seconds.size == 0:
                    continue
                origin_lookup[int(sensor_id)] = origin

                cells = []
                for pct in deviations:
                    deviation = deviation_for_span(float(low), float(high), pct)
                    result = swinging_door(seconds, values, deviation, quality=quality)
                    error = reconstruction_error(seconds, values, result)

                    if not error["within_bound"]:
                        violations += 1
                        logger.error("%s at %.2f%%: max error %.6g exceeds deviation %.6g",
                                     code, pct, error["max_error"], deviation)

                    totals[pct][0] += result.original_count
                    totals[pct][1] += result.kept_count
                    cells.append(f"{result.retained_pct:>8.2f}%")

                    if not args.sweep and args.store:
                        _store(conn, int(sensor_id), pct, deviation, result, error,
                               seconds, values, quality, origin_lookup[int(sensor_id)])

                    if not args.sweep:
                        logger.info(
                            "%-17s %-6s dev=%.5g  %s -> %s  (%.1fx, %.2f%% kept)  "
                            "max error %.5g",
                            code, unit or "-", deviation,
                            f"{result.original_count:,}", f"{result.kept_count:,}",
                            result.ratio, result.retained_pct, error["max_error"],
                        )

                if args.sweep:
                    logger.info("%-17s %-6s %s", code, unit or "-", "".join(cells))

            logger.info("-" * 60)
            for pct in deviations:
                original, kept = totals[pct]
                if original:
                    logger.info("%.2f%% of span: %s -> %s readings  (%.1fx, %.2f%% kept)",
                                pct, f"{original:,}", f"{kept:,}",
                                original / kept if kept else 0, 100.0 * kept / original)
            logger.info("Elapsed %.1f s", time.perf_counter() - started)

            if violations:
                logger.error("%d error-bound violation(s). This is a bug in the "
                             "compression implementation, not a property of the data.",
                             violations)
                return EXIT_BOUND

            logger.info("Error bound held for every sensor at every deviation.")
            return EXIT_OK

    except DatabaseError as exc:
        logger.error("%s", exc)
        return EXIT_FAILURE
    except Exception:
        logger.exception("Compression failed.")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
