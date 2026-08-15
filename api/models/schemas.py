"""Response contracts.

These models are the API's public surface, so they are written for the consumer rather
than mirroring the database. Two conventions run through all of them:

**Every aggregated number is accompanied by what produced it.** An hourly average built
from twelve samples is not the same number as one built from 360, and a client that
cannot tell them apart will draw both as an equally confident line. Sample counts and
coverage travel with the values.

**Quality is never hidden.** A reading that failed validation is returned with its
quality code rather than filtered out silently, so the caller decides what to trust.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Resolution(StrEnum):
    """Time resolution of a series.

    ``AUTO`` lets the server choose based on the requested window, which is what stops a
    six-month request from trying to return 1.8 million points per sensor.
    """

    AUTO = "auto"
    RAW = "raw"
    HOURLY = "hourly"
    DAILY = "daily"


class Severity(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class HealthStatus(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    NO_DATA = "NO DATA"


class OperatingState(StrEnum):
    OFF = "OFF"
    OFFLOADED = "OFFLOADED"
    LOADED = "LOADED"
    STARTING = "STARTING"


# --------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------

class ComponentHealth(BaseModel):
    name: str
    status: Literal["up", "down", "degraded"]
    detail: str | None = None
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    """Liveness plus dependency status.

    Returns 200 when the API itself is serving and 503 when a dependency it cannot work
    without is unavailable. A monitoring system needs that distinction; a body that says
    "unhealthy" behind a 200 is not actionable.
    """

    status: Literal["healthy", "degraded", "unhealthy"]
    version: str
    checked_at: datetime
    components: list[ComponentHealth]


# --------------------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------------------

class Equipment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    equipment_id: int
    equipment_code: str
    equipment_name: str
    equipment_type: str
    location: str | None = None
    description: str | None = None
    sensor_count: int
    is_active: bool


class Sensor(BaseModel):
    sensor_id: int
    equipment_id: int
    sensor_code: str
    sensor_name: str
    sensor_class: Literal["Analogue", "Digital"]
    measurement_type: str
    unit: str | None = None
    physical_min: float | None = None
    physical_max: float | None = None
    documented_setpoint: float | None = Field(
        default=None,
        description="A setpoint published by the equipment owner (e.g. the 7 bar "
                    "low-pressure switch), not a threshold chosen by this project.",
    )
    description: str = Field(
        description="Verbatim description from the dataset documentation."
    )
    is_active: bool


# --------------------------------------------------------------------------------------
# Readings and trends
# --------------------------------------------------------------------------------------

class ReadingPoint(BaseModel):
    """One point of a raw series."""

    timestamp: datetime
    value: float
    quality: str
    quality_usable: bool


class AggregatePoint(BaseModel):
    """One bucket of an aggregated series."""

    timestamp: datetime
    value: float = Field(description="Mean over the bucket.")
    min_value: float
    max_value: float
    sample_count: int
    coverage_pct: float | None = Field(
        default=None,
        description="Samples present as a percentage of a fully-covered bucket at the "
                    "measured 10 s interval. The archive is 82.4% covered overall, so "
                    "many buckets hold fewer samples than a naive reader would assume.",
    )


class ReadingSeries(BaseModel):
    sensor_id: int
    sensor_code: str
    unit: str | None
    resolution: Resolution = Field(
        description="The resolution actually served, which may differ from the one "
                    "requested when 'auto' was used or when a cap was applied."
    )
    start: datetime
    end: datetime
    point_count: int
    truncated: bool = Field(
        default=False,
        description="True when a result cap was reached and the series is incomplete. "
                    "Never silently trimmed.",
    )
    points: list[ReadingPoint] = Field(default_factory=list)
    aggregates: list[AggregatePoint] = Field(default_factory=list)


class TrendPoint(BaseModel):
    timestamp: datetime
    value: float
    rolling_mean: float | None = None
    rolling_std: float | None = None
    rate_of_change_per_hour: float | None = None
    sample_count: int


class TrendSeries(BaseModel):
    sensor_id: int
    sensor_code: str
    unit: str | None
    operating_state: OperatingState | None = Field(
        default=None,
        description="When set, the trend covers only periods in this operating state. "
                    "The compressor is off 54.65% of the time, so a state-blind trend "
                    "blends idle and running behaviour into a number describing neither.",
    )
    window_hours: int
    start: datetime
    end: datetime
    point_count: int
    points: list[TrendPoint]
    summary: dict[str, float | None] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Anomalies and failures
# --------------------------------------------------------------------------------------

class Anomaly(BaseModel):
    anomaly_id: int
    sensor_id: int
    sensor_code: str
    sensor_name: str
    equipment_code: str
    timestamp: datetime
    value: float
    unit: str | None = None
    expected_low: float | None = None
    expected_high: float | None = None
    method: str = Field(description="Which rule fired. The same instant may be flagged "
                                    "by more than one.")
    score: float
    severity: Severity
    operating_state: OperatingState | None = None
    detected_at: datetime


class AnomalyPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[Anomaly]


class FailureEvent(BaseModel):
    failure_event_id: int
    equipment_id: int
    equipment_code: str
    start: datetime
    end: datetime
    duration_minutes: int
    failure_type: str
    severity: str
    source_reference: str | None = Field(
        default=None,
        description="The identifier used in the published failure table, verbatim - "
                    "including its known defects.",
    )
    report_note: str | None = None
    data_quality_note: str | None = None
    lead_up_24h_scans: int | None = None
    lead_up_24h_stale_scans: int | None = None
    lead_up_24h_stale_pct: float | None = None
    lead_up_usability: str | None = Field(
        default=None,
        description="USABLE, UNUSABLE or NO DATA. Event #1's run-up is 69.5% held data, "
                    "so any statistic computed over it would describe a frozen logger.",
    )


# --------------------------------------------------------------------------------------
# Equipment health and summary
# --------------------------------------------------------------------------------------

class EquipmentHealth(BaseModel):
    equipment_id: int
    equipment_code: str
    equipment_name: str
    status: HealthStatus
    status_reason: str = Field(
        description="Plain-language explanation. A status an operator cannot act on is "
                    "not a status."
    )
    sensor_count: int
    last_reading_at: datetime | None
    total_readings: int
    active_critical_anomalies: int
    active_warning_anomalies: int
    recorded_failures: int
    active_window_hours: int = Field(
        default=24,
        description="Anomalies are counted over this trailing window of the archive, "
                    "not over all history - a status that only accumulates can never "
                    "return to NORMAL.",
    )


class LatestReading(BaseModel):
    sensor_id: int
    sensor_code: str
    sensor_name: str
    sensor_class: str
    unit: str | None
    timestamp: datetime
    value: float
    quality: str
    quality_usable: bool


class DataQualitySummary(BaseModel):
    total_readings: int
    good_readings: int
    good_pct: float
    held_readings: int = Field(
        description="Readings from a data-acquisition freeze: every analogue signal "
                    "repeating the previous scan. Plausible, in range, not measurements."
    )
    held_pct: float
    quarantined_rows: int
    gap_count: int
    coverage_pct: float
    last_successful_ingestion: datetime | None
    last_ingestion_status: str | None


class SystemSummary(BaseModel):
    """Everything the Overview screen needs, in one call.

    Deliberately one round trip: a dashboard that fires eight requests to render one
    screen is slower and harder to reason about than one endpoint that answers the
    question the screen actually asks.
    """

    generated_at: datetime
    archive_start: datetime | None
    archive_end: datetime | None
    equipment_count: int
    sensor_count: int
    equipment: list[EquipmentHealth]
    latest_readings: list[LatestReading]
    active_anomalies: list[Anomaly]
    recent_failures: list[FailureEvent]
    data_quality: DataQualitySummary


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------

class ProblemDetail(BaseModel):
    """RFC 7807 problem details.

    A machine-readable error shape, so a client can branch on ``type`` rather than
    pattern-matching prose that will be reworded eventually.
    """

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "type": "/problems/invalid-time-range",
            "title": "Invalid time range",
            "status": 422,
            "detail": "start (2020-06-01T00:00:00) must be earlier than end "
                      "(2020-05-01T00:00:00).",
            "instance": "/readings/14",
        }
    })

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
