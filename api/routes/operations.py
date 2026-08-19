"""Anomalies, failures, health and the overview summary."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status

from api.dependencies import LimitQuery, OffsetQuery, get_repository
from api.models.schemas import (
    Anomaly,
    AnomalyPage,
    ComponentHealth,
    DataQualitySummary,
    FailureEvent,
    HealthResponse,
    LatestReading,
    ProblemDetail,
    Severity,
    SystemSummary,
)
from api.services.repository import Repository
from ingestion import __version__
from ingestion.db import DatabaseError, connect

router = APIRouter()

#: Tables the API cannot serve anything useful without.
REQUIRED_TABLES = ("asset.Equipment", "asset.Sensor", "ts.SensorReading",
                   "ref.QualityCode", "ops.IngestionRun")


def _anomaly(row: tuple) -> Anomaly:
    return Anomaly(
        anomaly_id=row[0], sensor_id=row[1], sensor_code=row[2], sensor_name=row[3],
        equipment_code=row[4], timestamp=row[5], value=float(row[6]), unit=row[7],
        expected_low=float(row[8]) if row[8] is not None else None,
        expected_high=float(row[9]) if row[9] is not None else None,
        method=row[10], score=float(row[11]), severity=row[12],
        operating_state=row[13], detected_at=row[14],
    )


def _failure(row: tuple) -> FailureEvent:
    return FailureEvent(
        failure_event_id=row[0], equipment_id=row[1], equipment_code=row[2],
        start=row[3], end=row[4], duration_minutes=int(row[5]), failure_type=row[6],
        severity=row[7], source_reference=row[8], report_note=row[9],
        data_quality_note=row[10],
        lead_up_24h_scans=int(row[11]) if row[11] is not None else None,
        lead_up_24h_stale_scans=int(row[12]) if row[12] is not None else None,
        lead_up_24h_stale_pct=float(row[13]) if row[13] is not None else None,
        lead_up_usability=row[14],
    )


# --------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse, tags=["meta"],
            summary="Liveness and dependency status",
            responses={503: {"model": HealthResponse,
                             "description": "A required dependency is unavailable"}})
def health(response: Response) -> HealthResponse:
    """Report whether the API can actually serve requests.

    Owns its own connection rather than using the repository dependency, which would fail
    before the handler ran and lose the detail of which component is down. Returns 503 on
    an unreachable database, because a monitoring system needs the status code.
    """
    components: list[ComponentHealth] = []
    overall: Literal["healthy", "degraded", "unhealthy"] = "healthy"

    started = time.perf_counter()
    try:
        with connect() as conn:
            repo = Repository(conn)
            version = repo.ping()
            latency = (time.perf_counter() - started) * 1000
            components.append(ComponentHealth(
                name="sql_server", status="up",
                detail=f"SQL Server {version}", latency_ms=round(latency, 1),
            ))

            counts = repo.table_counts()
            missing = [t for t in REQUIRED_TABLES if t not in counts]
            if missing:
                overall = "unhealthy"
                components.append(ComponentHealth(
                    name="schema", status="down",
                    detail=f"Missing table(s): {', '.join(missing)}. Run "
                           f"scripts/init_database.py.",
                ))
            else:
                readings = counts.get("ts.SensorReading", 0)
                components.append(ComponentHealth(
                    name="schema", status="up",
                    detail=f"{len(counts)} tables present, ~{readings:,} readings",
                ))

            run = repo.last_ingestion()
            if run is None:
                overall = "degraded" if overall == "healthy" else overall
                components.append(ComponentHealth(
                    name="ingestion", status="degraded",
                    detail="No successful ingestion recorded. Run `python -m ingestion`.",
                ))
            else:
                components.append(ComponentHealth(
                    name="ingestion", status="up",
                    detail=f"Run #{run[0]} completed {run[3]}, archive covers "
                           f"{run[5]} to {run[6]}",
                ))
    except DatabaseError as exc:
        overall = "unhealthy"
        components.append(ComponentHealth(
            name="sql_server", status="down", detail=str(exc)[:400],
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        ))

    if overall == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(status=overall, version=__version__,
                          checked_at=datetime.now(), components=components)


# --------------------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------------------

@router.get("/anomalies", response_model=AnomalyPage, tags=["operations"],
            summary="Detected anomalies",
            responses={422: {"model": ProblemDetail}})
def list_anomalies(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    sensor_id: int | None = Query(default=None, ge=1),
    severity: Severity | None = Query(default=None),
    method: str | None = Query(
        default=None,
        description="ADAPTIVE_MAD, BASELINE_IQR or SETPOINT. The same instant can be "
                    "flagged by more than one rule, so filtering by method answers "
                    "'which rule fired' rather than collapsing them.",
    ),
    limit: int = LimitQuery,
    offset: int = OffsetQuery,
    repo: Repository = Depends(get_repository),
) -> AnomalyPage:
    """Paginated anomalies, worst severity first, then most recent.

    ``total`` is the unpaginated count, so a client can show "23 of 1,204" rather than
    discovering the end of the list by walking off it.
    """
    severity_value = severity.value if severity else None
    total = repo.count_anomalies(start, end, sensor_id, severity_value, method)
    rows = repo.list_anomalies(start, end, sensor_id, severity_value, method,
                               limit, offset)
    return AnomalyPage(total=total, limit=limit, offset=offset,
                       items=[_anomaly(r) for r in rows])


# --------------------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------------------

@router.get("/failures", response_model=list[FailureEvent], tags=["operations"],
            summary="Documented failure events")
def list_failures(
    equipment_id: int | None = Query(default=None, ge=1),
    repo: Repository = Depends(get_repository),
) -> list[FailureEvent]:
    """The four maintenance reports published with the dataset, verbatim.

    Each carries its lead-up data usability. Event #1's 24-hour run-up is 69.5% held
    data, so any statistic computed over it would describe a frozen logger rather than a
    compressor - the API says so rather than leaving a consumer to find out.
    """
    return [_failure(row) for row in repo.list_failures(equipment_id)]


# --------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------

@router.get("/summary", response_model=SystemSummary, tags=["operations"],
            summary="Everything the overview screen needs")
def get_summary(repo: Repository = Depends(get_repository)) -> SystemSummary:
    """One round trip for the whole Overview.

    A dashboard that fires eight requests to draw one screen is slower and harder to
    reason about than one endpoint answering the question the screen actually asks.
    """
    from api.routes.assets import _health  # local import avoids a circular import

    equipment_rows = repo.equipment_health()
    archive_start, archive_end = repo.archive_bounds()
    quality = repo.data_quality_summary()

    return SystemSummary(
        generated_at=datetime.now(),
        archive_start=archive_start,
        archive_end=archive_end,
        equipment_count=len(equipment_rows),
        sensor_count=len(repo.list_sensors()),
        equipment=[_health(row) for row in equipment_rows],
        latest_readings=[
            LatestReading(
                sensor_id=r[0], sensor_code=r[1], sensor_name=r[2], sensor_class=r[3],
                unit=r[4], timestamp=r[5], value=float(r[6]), quality=r[7],
                quality_usable=bool(r[8]),
            )
            for r in repo.latest_readings()
        ],
        active_anomalies=[_anomaly(r) for r in repo.active_anomalies(limit=50)],
        recent_failures=[_failure(r) for r in repo.list_failures()],
        data_quality=DataQualitySummary(**quality),
    )


@router.get("/data-quality", response_model=DataQualitySummary, tags=["operations"],
            summary="Archive quality at a glance")
def get_data_quality(repo: Repository = Depends(get_repository)) -> DataQualitySummary:
    """Coverage, trust and ingestion recency.

    ``held_readings`` is the number that matters and the one no standard check would
    surface: values from a data-acquisition freeze are non-null and inside range, so both
    a null check and a range check pass them.
    """
    return DataQualitySummary(**repo.data_quality_summary())
