"""Generate the HTML data-quality report from the live database.

Everything in the report is queried, never hard-coded, so a stale figure is impossible -
if the archive changes, the report changes with it.

Usage
-----
    python scripts/generate_quality_report.py [--out reports/data_quality_report.html]

Exit codes: 0 written · 1 failure · 2 nothing to report (no ingestion runs)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

from ingestion.config import PROJECT_ROOT, get_settings  # noqa: E402
from ingestion.db import DatabaseError, connect, fetch_all, scalar  # noqa: E402
from ingestion.logging_setup import configure_logging  # noqa: E402

logger = logging.getLogger("quality_report")

TEMPLATE_DIR = PROJECT_ROOT / "reports" / "templates"
DEFAULT_OUT = PROJECT_ROOT / "reports" / "data_quality_report.html"

EXIT_OK, EXIT_FAILURE, EXIT_NOTHING = 0, 1, 2

#: How each issue type is handled. Stated in the report so a reader never has to wonder
#: whether something was quietly dropped.
DISPOSITION = {
    "GAP": "Recorded. No data existed to store; the interval is reported, not filled.",
    "FLATLINE": "Stored and marked UNCERTAIN_STALE. Excluded from baselines and trends.",
    "OUT_OF_RANGE": "Stored and marked BAD_RANGE. Excluded from statistics, retained for audit.",
    "NON_BINARY_DIGITAL": "Stored and marked BAD_DIGITAL.",
    "MISSING_VALUE": "Not stored (Value is NOT NULL); counted here instead of substituted.",
    "UNPARSEABLE_TIMESTAMP": "Quarantined verbatim in ops.RejectedRow.",
    "DUPLICATE_TIMESTAMP": "Quarantined verbatim; would collide on the primary key.",
    "NON_MONOTONIC_TS": "Quarantined verbatim; would collide on the primary key.",
    "XSENSOR_DIVERGENCE": "Recorded only. A relationship between two tags, so neither is blamed.",
    "LOAD_RECONCILIATION": "Self-audit of the load's arithmetic.",
}


def collect(conn) -> dict:
    """Query everything the template needs."""
    settings = get_settings()

    equipment_row = fetch_all(
        conn, "SELECT TOP (1) EquipmentCode, EquipmentName FROM asset.Equipment ORDER BY EquipmentId"
    )
    if not equipment_row:
        raise DatabaseError("No equipment configured. Run database/seed.sql.")
    equipment = {"code": equipment_row[0][0], "name": equipment_row[0][1]}

    readings = int(scalar(conn, "SELECT COUNT_BIG(*) FROM ts.SensorReading") or 0)
    sensors_n = int(scalar(conn, "SELECT COUNT(*) FROM asset.Sensor WHERE IsActive = 1") or 0)
    scans = int(scalar(conn, "SELECT COUNT_BIG(*) FROM analytics.ScanState") or 0)
    min_ts, max_ts = fetch_all(
        conn, "SELECT MIN(ReadingTs), MAX(ReadingTs) FROM ts.SensorReading")[0]

    # Coverage against a continuous archive at the measured 10 s interval.
    coverage_pct = 0.0
    if min_ts and max_ts and scans:
        expected = (max_ts - min_ts).total_seconds() / 10.0 + 1
        coverage_pct = 100.0 * scans / expected

    quality_rows = fetch_all(conn, """
        SELECT q.Code, q.Family, q.IsUsable, q.Description, COUNT_BIG(*) AS N
        FROM ts.SensorReading AS r
        INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
        GROUP BY q.Code, q.Family, q.IsUsable, q.Description
        ORDER BY N DESC""")
    by_code = [
        {"code": r[0], "family": r[1], "usable": bool(r[2]), "description": r[3],
         "n": int(r[4]), "pct": 100.0 * int(r[4]) / readings if readings else 0.0}
        for r in quality_rows
    ]
    good = next((c["n"] for c in by_code if c["code"] == "GOOD"), 0)
    stale = next((c["n"] for c in by_code if c["code"] == "UNCERTAIN_STALE"), 0)

    issue_rows = fetch_all(conn, """
        SELECT IssueType,
               MAX(Severity),
               SUM(CASE WHEN IsSummary = 1 THEN CAST(AffectedRows AS BIGINT) ELSE 0 END),
               SUM(CASE WHEN IsSummary = 0 THEN 1 ELSE 0 END)
        FROM ops.DataQualityIssue
        WHERE IngestionRunId = (SELECT MAX(IngestionRunId) FROM ops.IngestionRun
                                WHERE Status = 'SUCCEEDED')
        GROUP BY IssueType
        HAVING SUM(CASE WHEN IsSummary = 1 THEN CAST(AffectedRows AS BIGINT) ELSE 0 END) > 0
        ORDER BY 3 DESC""")
    issues = [
        {"issue_type": r[0], "severity": r[1], "affected": int(r[2]), "details": int(r[3]),
         "disposition": DISPOSITION.get(r[0], "Recorded.")}
        for r in issue_rows
    ]

    gap_count = int(scalar(conn, "SELECT COUNT(*) FROM ops.vw_TimestampGap") or 0)
    longest_gaps = [
        {"start": r[0], "end": r[1], "hours": float(r[2]), "missing": int(r[3])}
        for r in fetch_all(conn, """
            SELECT TOP (10) GapStart, GapEnd, GapHours, EstimatedMissingScans
            FROM ops.vw_TimestampGap ORDER BY GapSeconds DESC""")
    ]

    sensor_rows = fetch_all(conn, """
        SELECT s.SensorCode, s.SensorClass, s.Unit, s.PhysicalMin, s.PhysicalMax,
               COUNT_BIG(*) AS N,
               SUM(CASE WHEN r.QualityCodeId = 192 THEN 1 ELSE 0 END) AS Good,
               SUM(CASE WHEN r.QualityCodeId = 65 THEN 1 ELSE 0 END) AS Stale,
               MIN(r.Value), MAX(r.Value)
        FROM ts.SensorReading AS r
        INNER JOIN asset.Sensor AS s ON s.SensorId = r.SensorId
        GROUP BY s.SensorCode, s.SensorClass, s.Unit, s.PhysicalMin, s.PhysicalMax,
                 s.DisplayOrder
        ORDER BY s.DisplayOrder""")
    sensors = [
        {"code": r[0], "sensor_class": r[1], "unit": r[2],
         "physical_min": float(r[3]) if r[3] is not None else 0.0,
         "physical_max": float(r[4]) if r[4] is not None else 0.0,
         "readings": int(r[5]),
         "good_pct": 100.0 * int(r[6]) / int(r[5]) if r[5] else 0.0,
         "stale_pct": 100.0 * int(r[7]) / int(r[5]) if r[5] else 0.0,
         "min_value": float(r[8]), "max_value": float(r[9])}
        for r in sensor_rows
    ]

    frozen = [
        {"start": r[0], "end": r[1], "scans": int(r[2]),
         "hours": (int(r[2]) + 1) * 10 / 3600.0}
        for r in fetch_all(conn, """
            SELECT TOP (10) WindowStartTs, WindowEndTs, AffectedRows
            FROM ops.DataQualityIssue
            WHERE IssueType = 'FLATLINE' AND IsSummary = 0
              -- Same per-run duplication as gaps: scope to the latest completed run.
              AND IngestionRunId = (SELECT MAX(IngestionRunId) FROM ops.IngestionRun
                                    WHERE Status = 'SUCCEEDED')
            ORDER BY AffectedRows DESC""")
    ]

    run_rows = fetch_all(conn, """
        SELECT IngestionRunId, Status, StartedUtc,
               DATEDIFF(second, StartedUtc, CompletedUtc),
               RowsRead, RowsInserted, RowsSkippedDup, RowsRejected,
               SourceFile, SourceSha256, SourceBytes, CompletedUtc
        FROM ops.IngestionRun ORDER BY IngestionRunId""")
    if not run_rows:
        raise DatabaseError("No ingestion runs recorded. Run `python -m ingestion` first.")

    runs = [
        {"id": int(r[0]), "status": r[1],
         "started": r[2].strftime("%Y-%m-%d %H:%M:%S") if r[2] else None,
         "seconds": r[3],
         "rows_read": int(r[4]), "inserted": int(r[5]), "skipped": int(r[6]),
         "rejected": int(r[7])}
        for r in run_rows
    ]
    # The latest FULL run. A PARTIAL run is an explicit --limit-days dev subset, so
    # reporting it as "last successful ingestion" would tell an operator the archive is
    # current when only one day of it was loaded.
    last_ok = next((r for r in reversed(run_rows) if r[1] == "SUCCEEDED"), run_rows[-1])

    return {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "equipment": equipment,
        "archive": {"readings": readings, "sensors": sensors_n, "scans": scans,
                    "min_ts": min_ts, "max_ts": max_ts, "coverage_pct": coverage_pct},
        "quality": {"good": good, "good_pct": 100.0 * good / readings if readings else 0.0,
                    "stale": stale,
                    "stale_pct": 100.0 * stale / readings if readings else 0.0,
                    "rejected": int(scalar(conn, "SELECT COUNT_BIG(*) FROM ops.RejectedRow") or 0),
                    "by_code": by_code, "issues": issues},
        "gaps": {"count": gap_count, "longest": longest_gaps},
        "gap_threshold": settings.gap_threshold_seconds,
        "sensors": sensors,
        "frozen": frozen,
        "runs": runs,
        "last_run": {"id": int(last_ok[0]), "status": last_ok[1],
                     # Second resolution: microseconds on an ingestion timestamp are noise.
                     "completed": (last_ok[11].strftime("%Y-%m-%d %H:%M:%S")
                                   if last_ok[11] else None)},
        "source": {"file": Path(str(last_ok[8])).name, "sha256": last_ok[9],
                   "bytes": int(last_ok[10] or 0)},
    }


def render(context: dict, out_path: Path) -> Path:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template("data_quality_report.html.jinja").render(**context)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings, logger_name="quality_report")

    try:
        with connect(settings=settings) as conn:
            context = collect(conn)
    except DatabaseError as exc:
        logger.error("%s", exc)
        return EXIT_NOTHING if "No ingestion runs" in str(exc) else EXIT_FAILURE
    except Exception:
        logger.exception("Failed to build the data-quality report.")
        return EXIT_FAILURE

    written = render(context, args.out)
    size_kb = written.stat().st_size / 1024
    logger.info("Wrote %s (%.0f KB)", written, size_kb)
    logger.info("Readings %s | good %.2f%% | held %.2f%% | gaps %s | quarantined %s",
                f"{context['archive']['readings']:,}",
                context["quality"]["good_pct"], context["quality"]["stale_pct"],
                f"{context['gaps']['count']:,}", f"{context['quality']['rejected']:,}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
