"""API tests.

Every test here runs without SQL Server. The routes depend on a `Repository`
provided through FastAPI's dependency system, so a fake is substituted with
`app.dependency_overrides`. That is the whole reason the repository exists as a seam:
the entire HTTP surface - status codes, error shapes, pagination, resolution selection -
is verifiable in CI with no database infrastructure.

The fake returns data shaped exactly like the real queries: tuples in column order. That
keeps it honest. A fake returning convenient dictionaries would pass while the real
mapping was wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_repository
from api.main import app
from api.models.schemas import Resolution
from api.services.repository import NotFoundError, Repository
from ingestion.db import DatabaseError

ARCHIVE_START = datetime(2020, 2, 1, 0, 0, 0)
ARCHIVE_END = datetime(2020, 9, 1, 3, 59, 50)


class FakeRepository:
    """Stand-in for `Repository`, returning tuples in real column order."""

    def __init__(self) -> None:
        self.sensor_missing = False
        self.equipment_missing = False
        self.raw_truncated = False
        self.anomaly_total = 15_196

    # -- assets -----------------------------------------------------------------
    def list_equipment(self):
        return [(1, "APU-01", "Metro Train Air Production Unit",
                 "Compressor / Air Production Unit", "Onboard metro train",
                 "Air production unit.", True, 15)]

    def get_equipment(self, equipment_id):
        if self.equipment_missing or equipment_id != 1:
            raise NotFoundError(f"Equipment {equipment_id} does not exist.")
        return self.list_equipment()[0]

    def list_sensors(self, equipment_id=None, sensor_class=None):
        rows = [
            (15, 1, "TP3", "Pneumatic panel pressure (TP3)", "Analogue", "Pressure",
             "bar", -1.0, 16.0, None, "the measure of the pressure generated at the "
             "pneumatic panel.", True),
            (2, 1, "COMP", "Air intake valve signal (COMP)", "Digital", "ValveState",
             None, 0.0, 1.0, None, "the electrical signal of the air intake valve.", True),
        ]
        if sensor_class:
            rows = [r for r in rows if r[4] == sensor_class]
        return rows

    def get_sensor(self, sensor_id):
        for row in self.list_sensors():
            if row[0] == sensor_id and not self.sensor_missing:
                return row
        raise NotFoundError(f"Sensor {sensor_id} does not exist.")

    def resolve_sensor(self, identifier):
        if self.sensor_missing:
            raise NotFoundError(f"No sensor with id or code {identifier!r}.")
        for row in self.list_sensors():
            if str(row[0]) == identifier or row[2] == identifier.upper():
                return row
        raise NotFoundError(f"No sensor with id or code {identifier!r}.")

    def archive_bounds(self):
        return ARCHIVE_START, ARCHIVE_END

    # -- readings ---------------------------------------------------------------
    choose_resolution = staticmethod(Repository.choose_resolution)

    def raw_readings(self, sensor_id, start, end, limit=50_000):
        rows = [(start + timedelta(seconds=10 * i), 8.9 + i * 0.001, "GOOD", True)
                for i in range(5)]
        return rows, self.raw_truncated

    def hourly_readings(self, sensor_id, start, end, limit=50_000):
        return [(start + timedelta(hours=i), 8.9, 8.5, 9.3, 360) for i in range(3)], False

    def daily_readings(self, sensor_id, start, end, limit=50_000):
        return [(start + timedelta(days=i), 8.9, 8.1, 9.9, 8640) for i in range(3)], False

    def trend(self, sensor_id, start, end, operating_state, window_hours):
        return [
            (start, 8.9, 360, None, None, None, None),
            (start + timedelta(hours=1), 9.1, 360, 9.0, 0.14, 0.2, 1),
            (start + timedelta(hours=2), 9.3, 300, 9.1, 0.2, 0.2, 1),
        ]

    # -- operations -------------------------------------------------------------
    def _anomaly_row(self, anomaly_id=1):
        return (anomaly_id, 15, "TP3", "Pneumatic panel pressure (TP3)", "APU-01",
                datetime(2020, 7, 15, 17, 0), 6.09, "bar", 7.15, 11.65,
                "ADAPTIVE_MAD", 12.4, "CRITICAL", "OFF", datetime(2026, 9, 8, 1, 0))

    def count_anomalies(self, *args):
        return self.anomaly_total

    def list_anomalies(self, start, end, sensor_id, severity, method, limit, offset):
        return [self._anomaly_row(i) for i in range(offset + 1, offset + 1 + min(limit, 3))]

    def active_anomalies(self, limit=50):
        return [self._anomaly_row()]

    def list_failures(self, equipment_id=None):
        return [
            (1, 1, "APU-01", datetime(2020, 4, 18), datetime(2020, 4, 18, 23, 59),
             1439, "Air leak", "High stress", "#1", None,
             "69.5% of the lead-up is held data.", 6301, 4378, 69.5, "UNUSABLE"),
            (4, 1, "APU-01", datetime(2020, 7, 15, 14, 30), datetime(2020, 7, 15, 19, 0),
             270, "Air Leak", "High stress", "#4", "Maintenance on 16Jul", None,
             6185, 0, 0.0, "USABLE"),
        ]

    def equipment_health(self, equipment_id=None):
        return [(1, "APU-01", "Metro Train Air Production Unit", 15, ARCHIVE_END,
                 22_754_220, 28, 48, 4, "CRITICAL")]

    def latest_readings(self):
        return [(15, "TP3", "Pneumatic panel pressure (TP3)", "Analogue", "bar",
                 ARCHIVE_END, 8.86, "GOOD", True)]

    def data_quality_summary(self, use_cache=True):
        return {
            "total_readings": 22_754_220, "good_readings": 21_991_395,
            "good_pct": 96.648, "held_readings": 762_825, "held_pct": 3.352,
            "quarantined_rows": 0, "gap_count": 331, "coverage_pct": 82.36,
            "last_successful_ingestion": datetime(2026, 9, 8, 0, 49, 42),
            "last_ingestion_status": "SUCCEEDED",
        }


@pytest.fixture
def fake() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def client(fake: FakeRepository):
    app.dependency_overrides[get_repository] = lambda: fake
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------------
# Contract basics
# ---------------------------------------------------------------------------------

def test_openapi_is_generated(client):
    """The interactive documentation is a deliverable, not a side effect."""
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "Industrial Time-Series Monitoring API"
    for path in ("/equipment", "/sensors", "/readings/{sensor_id}", "/trends/{sensor_id}",
                 "/anomalies", "/failures", "/summary", "/health"):
        assert path in spec["paths"], f"{path} is missing from the schema"


def test_root_redirects_to_docs(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/docs"


def test_every_response_carries_a_request_id_and_timing(client):
    response = client.get("/equipment")
    assert response.headers["X-Request-ID"]
    assert float(response.headers["X-Response-Time-ms"]) >= 0


def test_a_supplied_request_id_is_echoed(client):
    """A client-supplied id must survive, so a trace spans client and server logs."""
    response = client.get("/equipment", headers={"X-Request-ID": "trace-me-123"})
    assert response.headers["X-Request-ID"] == "trace-me-123"


# ---------------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------------

def test_list_equipment(client):
    body = client.get("/equipment").json()
    assert body[0]["equipment_code"] == "APU-01"
    assert body[0]["sensor_count"] == 15


def test_sensor_lookup_accepts_a_code_as_well_as_an_id(client):
    """`/sensors/TP3` is far easier to use by hand than `/sensors/15`, and the ids are
    assignment-order artefacts with no meaning to a user."""
    by_code = client.get("/sensors/TP3").json()
    by_id = client.get("/sensors/15").json()
    assert by_code == by_id
    assert by_code["sensor_code"] == "TP3"


def test_sensor_description_is_the_verbatim_source_text(client):
    """The documentation travels with the data, so a dashboard can explain a tag without
    a lookup elsewhere."""
    body = client.get("/sensors/TP3").json()
    assert "pneumatic panel" in body["description"]


def test_sensors_filter_by_class(client):
    body = client.get("/sensors", params={"sensor_class": "Digital"}).json()
    assert [s["sensor_code"] for s in body] == ["COMP"]


def test_equipment_health_explains_itself(client):
    """A status an operator cannot act on is not a status."""
    body = client.get("/equipment/1/health").json()
    assert body["status"] == "CRITICAL"
    assert "28 critical" in body["status_reason"]
    assert body["active_window_hours"] == 24


# ---------------------------------------------------------------------------------
# Resolution selection
# ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hours, expected",
    [
        (1, Resolution.RAW),
        (6, Resolution.RAW),
        (7, Resolution.HOURLY),
        (24 * 30, Resolution.HOURLY),
        (24 * 31, Resolution.DAILY),
        (24 * 213, Resolution.DAILY),
    ],
)
def test_auto_resolution_scales_with_the_window(hours, expected):
    """Without this, a six-month request asks for 1.8 million points per sensor."""
    start = ARCHIVE_START
    assert Repository.choose_resolution(Resolution.AUTO, start,
                                        start + timedelta(hours=hours)) is expected


def test_explicit_resolution_is_honoured(client):
    """A caller who genuinely wants raw data over a wide window gets it, and is told if
    a cap was hit."""
    body = client.get("/readings/TP3", params={
        "start": "2020-02-01T00:00:00", "end": "2020-09-01T00:00:00",
        "resolution": "raw"}).json()
    assert body["resolution"] == "raw"
    assert body["points"], "raw resolution must return points, not aggregates"


def test_wide_window_defaults_to_daily(client):
    body = client.get("/readings/TP3", params={
        "start": "2020-02-01T00:00:00", "end": "2020-09-01T00:00:00"}).json()
    assert body["resolution"] == "daily"
    assert body["aggregates"] and not body["points"]


def test_aggregates_report_coverage(client):
    """An hourly mean from 12 samples is not the same number as one from 360."""
    body = client.get("/readings/TP3", params={
        "start": "2020-06-01T00:00:00", "end": "2020-06-08T00:00:00"}).json()
    assert body["resolution"] == "hourly"
    assert body["aggregates"][0]["coverage_pct"] == 100.0


def test_truncation_is_declared_never_silent(client, fake):
    fake.raw_truncated = True
    body = client.get("/readings/TP3", params={
        "start": "2020-06-05T00:00:00", "end": "2020-06-05T02:00:00"}).json()
    assert body["truncated"] is True


def test_readings_carry_quality(client):
    """A reading that failed validation is returned labelled, not filtered out silently."""
    body = client.get("/readings/TP3", params={
        "start": "2020-06-05T00:00:00", "end": "2020-06-05T01:00:00"}).json()
    assert body["points"][0]["quality"] == "GOOD"
    assert body["points"][0]["quality_usable"] is True


def test_multi_sensor_request_is_capped(client):
    """The point cap is per series, so an uncapped sensor list would multiply it."""
    body = client.get("/readings", params={
        "sensors": "TP3,TP3,TP3,TP3,TP3,TP3,TP3,TP3,TP3,TP3",
        "start": "2020-06-05T00:00:00", "end": "2020-06-05T02:00:00"}).json()
    assert len(body) == 8


def test_limits_are_published(client):
    """Clients should read the caps, not discover them by being truncated."""
    body = client.get("/limits").json()
    assert body["max_points_per_series"] == 50_000
    assert body["max_sensors_per_multi_request"] == 8


# ---------------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------------

def test_trend_returns_rolling_statistics(client):
    body = client.get("/trends/TP3", params={
        "start": "2020-06-01T00:00:00", "end": "2020-06-08T00:00:00"}).json()
    assert body["point_count"] == 3
    assert body["points"][1]["rolling_mean"] == pytest.approx(9.0)
    assert body["summary"]["latest"] == pytest.approx(9.3)


def test_trend_can_be_restricted_to_one_operating_state(client):
    body = client.get("/trends/TP3", params={
        "start": "2020-06-01T00:00:00", "end": "2020-06-08T00:00:00",
        "operating_state": "LOADED"}).json()
    assert body["operating_state"] == "LOADED"


def test_first_trend_point_has_no_rate_of_change(client):
    """Nothing precedes it, so a rate would be invented."""
    body = client.get("/trends/TP3", params={
        "start": "2020-06-01T00:00:00", "end": "2020-06-08T00:00:00"}).json()
    assert body["points"][0]["rate_of_change_per_hour"] is None


# ---------------------------------------------------------------------------------
# Anomalies and failures
# ---------------------------------------------------------------------------------

def test_anomaly_page_reports_the_unpaginated_total(client):
    """So a client can show '3 of 15,196' rather than walking off the end of the list."""
    body = client.get("/anomalies", params={"limit": 3}).json()
    assert body["total"] == 15_196
    assert len(body["items"]) == 3
    assert body["limit"] == 3 and body["offset"] == 0


def test_anomaly_carries_the_rule_that_fired(client):
    """The same instant can be flagged by more than one rule; collapsing them would hide
    which one is actually firing."""
    item = client.get("/anomalies", params={"limit": 1}).json()["items"][0]
    assert item["method"] == "ADAPTIVE_MAD"
    assert item["expected_low"] is not None and item["expected_high"] is not None


def test_failures_expose_lead_up_usability(client):
    """Event #1's run-up is 69.5% held data. A consumer must not have to discover that."""
    body = client.get("/failures").json()
    unusable = [f for f in body if f["lead_up_usability"] == "UNUSABLE"]
    assert unusable and unusable[0]["lead_up_24h_stale_pct"] == pytest.approx(69.5)


def test_failure_source_reference_is_verbatim(client):
    """The published table numbers two different events '#1'. Preserved, not corrected."""
    assert [f["source_reference"] for f in client.get("/failures").json()] == ["#1", "#4"]


# ---------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------

def test_summary_answers_the_overview_screen_in_one_call(client):
    body = client.get("/summary").json()
    for key in ("equipment", "latest_readings", "active_anomalies", "recent_failures",
                "data_quality", "archive_start", "archive_end"):
        assert key in body, f"{key} missing from the summary"
    assert body["data_quality"]["held_readings"] == 762_825


def test_data_quality_surfaces_held_readings(client):
    """The number no standard check would find: non-null, in range, not a measurement."""
    body = client.get("/data-quality").json()
    assert body["held_readings"] == 762_825
    assert body["held_pct"] == pytest.approx(3.352)
    assert body["gap_count"] == 331


# ---------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------

def test_unknown_sensor_is_404_as_a_problem_document(client, fake):
    fake.sensor_missing = True
    response = client.get("/sensors/NOPE")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "/problems/not-found"
    assert body["status"] == 404
    assert body["instance"] == "/sensors/NOPE"


def test_unknown_equipment_is_404(client, fake):
    fake.equipment_missing = True
    assert client.get("/equipment/999").status_code == 404


def test_health_endpoint_404s_before_reporting_on_nothing(client, fake):
    fake.equipment_missing = True
    assert client.get("/equipment/999/health").status_code == 404


def test_reversed_time_range_is_422(client):
    response = client.get("/readings/TP3", params={
        "start": "2020-06-01T00:00:00", "end": "2020-05-01T00:00:00"})
    assert response.status_code == 422
    assert response.json()["type"] == "/problems/invalid-time-range"


def test_absurdly_wide_window_is_refused(client):
    """A ten-year request would scan every index for data that does not exist."""
    response = client.get("/readings/TP3", params={
        "start": "2015-01-01T00:00:00", "end": "2025-01-01T00:00:00"})
    assert response.status_code == 422
    assert "exceeds" in response.json()["detail"]


@pytest.mark.parametrize(
    "path, params",
    [
        ("/anomalies", {"limit": 99_999}),
        ("/anomalies", {"limit": 0}),
        ("/anomalies", {"offset": -1}),
        ("/anomalies", {"severity": "BANANA"}),
        ("/equipment/abc", None),
        ("/trends/TP3", {"window_hours": 1}),
        ("/trends/TP3", {"window_hours": 500}),
        ("/sensors", {"sensor_class": "Imaginary"}),
    ],
)
def test_invalid_parameters_are_422_problem_documents(client, path, params):
    response = client.get(path, params=params)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] in ("/problems/validation-error",
                                       "/problems/invalid-time-range")


def test_database_failure_is_503_and_leaks_nothing(client, fake, monkeypatch):
    """A database message can carry a server name, schema detail or a credential. The
    client gets a reference to quote; the detail goes to the log."""
    def explode():
        raise DatabaseError(
            "Could not connect to secret-server\\PROD/AppDb "
            "(SQL auth as admin, ODBC Driver 18): login failed for user 'admin'."
        )

    app.dependency_overrides[get_repository] = explode
    response = client.get("/equipment")
    app.dependency_overrides[get_repository] = lambda: fake

    assert response.status_code == 503
    body = response.json()
    assert body["type"] == "/problems/database-unavailable"
    for leak in ("secret-server", "PROD", "admin", "ODBC", "login failed"):
        assert leak not in response.text, f"{leak!r} leaked into the error response"


def test_unhandled_error_is_500_with_a_reference(fake, monkeypatch):
    """TestClient re-raises server exceptions by default, which is convenient when
    debugging and wrong when the thing under test IS the error handler. This one uses a
    client configured to behave like a real server."""
    monkeypatch.setattr(fake, "list_equipment",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    app.dependency_overrides[get_repository] = lambda: fake
    try:
        with TestClient(app, raise_server_exceptions=False) as strict_client:
            response = strict_client.get("/equipment")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    body = response.json()
    assert body["type"] == "/problems/internal-error"
    assert "boom" not in response.text, "internal detail must not reach the client"
    assert "reference" in body["detail"]
