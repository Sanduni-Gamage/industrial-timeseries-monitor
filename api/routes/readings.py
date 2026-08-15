"""Historical readings and trends."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from api.dependencies import EndQuery, StartQuery, get_repository, resolve_window
from api.models.schemas import (
    AggregatePoint,
    OperatingState,
    ProblemDetail,
    ReadingPoint,
    ReadingSeries,
    Resolution,
    TrendPoint,
    TrendSeries,
)
from api.services.repository import (
    MAX_POINTS,
    SAMPLES_PER_DAY,
    SAMPLES_PER_HOUR,
    Repository,
)

router = APIRouter(tags=["readings"])

RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetail, "description": "No such sensor"},
    422: {"model": ProblemDetail, "description": "Invalid time range"},
}


@router.get("/readings/{sensor_id}", response_model=ReadingSeries, responses=RESPONSES,
            summary="Historical readings for one sensor")
def get_readings(
    sensor_id: str = Path(description="Numeric id or sensor code, e.g. 15 or TP3."),
    start: datetime | None = StartQuery,
    end: datetime | None = EndQuery,
    resolution: Resolution = Query(
        default=Resolution.AUTO,
        description="'auto' picks raw / hourly / daily from the width of the window. "
                    "Ask for a specific resolution only if you need it - six months of "
                    "raw data is 1.8 million points.",
    ),
    repo: Repository = Depends(get_repository),
) -> ReadingSeries:
    """Return a series, choosing a resolution that a client can actually draw.

    The response always states the resolution it served and whether a cap was hit, so a
    truncated series can never be mistaken for a complete one.
    """
    sensor = repo.resolve_sensor(sensor_id)
    sensor_pk, sensor_code, unit = sensor[0], sensor[2], sensor[6]

    _, archive_end = repo.archive_bounds()
    window_start, window_end = resolve_window(start, end, fallback_end=archive_end)
    chosen = repo.choose_resolution(resolution, window_start, window_end)

    if chosen is Resolution.RAW:
        rows, truncated = repo.raw_readings(sensor_pk, window_start, window_end)
        points = [
            ReadingPoint(timestamp=r[0], value=float(r[1]), quality=r[2],
                         quality_usable=bool(r[3]))
            for r in rows
        ]
        return ReadingSeries(
            sensor_id=sensor_pk, sensor_code=sensor_code, unit=unit, resolution=chosen,
            start=window_start, end=window_end, point_count=len(points),
            truncated=truncated, points=points,
        )

    if chosen is Resolution.HOURLY:
        rows, truncated = repo.hourly_readings(sensor_pk, window_start, window_end)
        per_bucket = SAMPLES_PER_HOUR
    else:
        rows, truncated = repo.daily_readings(sensor_pk, window_start, window_end)
        per_bucket = SAMPLES_PER_DAY

    aggregates = [
        AggregatePoint(
            timestamp=r[0], value=float(r[1]), min_value=float(r[2]),
            max_value=float(r[3]), sample_count=int(r[4]),
            coverage_pct=round(100.0 * int(r[4]) / per_bucket, 1),
        )
        for r in rows
    ]
    return ReadingSeries(
        sensor_id=sensor_pk, sensor_code=sensor_code, unit=unit, resolution=chosen,
        start=window_start, end=window_end, point_count=len(aggregates),
        truncated=truncated, aggregates=aggregates,
    )


@router.get("/trends/{sensor_id}", response_model=TrendSeries, responses=RESPONSES,
            summary="Trend with rolling statistics")
def get_trend(
    sensor_id: str = Path(description="Numeric id or sensor code."),
    start: datetime | None = StartQuery,
    end: datetime | None = EndQuery,
    operating_state: OperatingState | None = Query(
        default=None,
        description="Restrict the trend to one operating state. Strongly recommended for "
                    "anything statistical: the compressor is off 54.65% of the time, so a "
                    "state-blind mean blends idle and running behaviour.",
    ),
    window_hours: int = Query(default=24, ge=2, le=168,
                              description="Rolling window, in hourly buckets."),
    repo: Repository = Depends(get_repository),
) -> TrendSeries:
    """Hourly series with a rolling mean, rolling spread and rate of change.

    Rate of change is per hour of **elapsed** time rather than per bucket, because 17.6%
    of the archive is missing: treating a 48-hour gap as one step would report a violent
    swing where there was simply no data.
    """
    sensor = repo.resolve_sensor(sensor_id)
    sensor_pk, sensor_code, unit = sensor[0], sensor[2], sensor[6]

    _, archive_end = repo.archive_bounds()
    window_start, window_end = resolve_window(start, end, fallback_end=archive_end,
                                              default_days=30)

    rows = repo.trend(sensor_pk, window_start, window_end,
                      operating_state.value if operating_state else None, window_hours)

    points: list[TrendPoint] = []
    for bucket, value, samples, rolling_mean, rolling_std, delta, hours_apart in rows:
        elapsed = float(hours_apart) if hours_apart else None
        points.append(TrendPoint(
            timestamp=bucket,
            value=float(value),
            rolling_mean=float(rolling_mean) if rolling_mean is not None else None,
            rolling_std=float(rolling_std) if rolling_std is not None else None,
            rate_of_change_per_hour=(float(delta) / elapsed
                                     if delta is not None and elapsed else None),
            sample_count=int(samples),
        ))

    values = [p.value for p in points]
    summary: dict[str, float | None] = {}
    if values:
        summary = {
            "min": min(values),
            "max": max(values),
            "mean": sum(values) / len(values),
            "latest": values[-1],
            "latest_rolling_mean": points[-1].rolling_mean,
        }

    return TrendSeries(
        sensor_id=sensor_pk, sensor_code=sensor_code, unit=unit,
        operating_state=operating_state, window_hours=window_hours,
        start=window_start, end=window_end, point_count=len(points),
        points=points, summary=summary,
    )


@router.get("/readings", response_model=list[ReadingSeries], responses=RESPONSES,
            summary="Readings for several sensors at once")
def get_multi_readings(
    sensors: str = Query(
        description="Comma-separated sensor ids or codes, e.g. 'TP2,TP3,OIL_TEMPERATURE'.",
        examples=["TP2,TP3"],
    ),
    start: datetime | None = StartQuery,
    end: datetime | None = EndQuery,
    resolution: Resolution = Query(default=Resolution.AUTO),
    repo: Repository = Depends(get_repository),
) -> list[ReadingSeries]:
    """Fetch several series in one round trip, for a multi-tag chart.

    Capped at eight sensors. The point cap is per series, so an uncapped list would let a
    single request ask for 15x the ceiling.
    """
    identifiers = [s.strip() for s in sensors.split(",") if s.strip()][:8]
    return [
        get_readings(sensor_id=identifier, start=start, end=end,
                     resolution=resolution, repo=repo)
        for identifier in identifiers
    ]


@router.get("/limits", tags=["meta"], summary="Result caps applied by this API")
def get_limits() -> dict[str, int | str]:
    """Publish the caps rather than letting clients discover them by being truncated."""
    return {
        "max_points_per_series": MAX_POINTS,
        "max_sensors_per_multi_request": 8,
        "auto_raw_max_hours": 6,
        "auto_hourly_max_days": 30,
        "note": "Series that hit a cap are returned with truncated=true; they are never "
                "silently trimmed.",
    }
