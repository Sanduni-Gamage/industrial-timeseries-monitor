"""FastAPI application for the industrial time-series monitoring platform.

Run it with:

    python -m api                       # honours API_HOST / API_PORT from .env
    uvicorn api.main:app --reload       # development

Interactive documentation is generated from the route signatures and response models:
``/docs`` (Swagger UI), ``/redoc``, and the raw schema at ``/openapi.json``.

Design notes worth stating once:

**Endpoints are synchronous ``def``, not ``async def``.** ``pyodbc`` is a blocking driver.
FastAPI runs a sync endpoint in a worker thread, so a slow query delays one request
instead of stalling the event loop for everyone. Declaring these ``async`` would be
strictly worse while looking more modern.

**No credentials reach the client.** Connection details live in configuration, database
errors are logged with a reference and replaced with a stable message, and no endpoint
echoes a connection string.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from api.errors import register_error_handlers
from api.routes import assets, operations, readings
from ingestion import __version__
from ingestion.config import get_settings
from ingestion.db import DatabaseError, connect
from ingestion.logging_setup import configure_logging

logger = logging.getLogger("api")

DESCRIPTION = """
REST API over a historical archive of condition-monitoring data from a metro train's
compressor Air Production Unit (UCI MetroPT-3, DOI 10.24432/C5VW3R).

**What the data is.** 22,754,220 readings - 1,516,948 scans of 15 sensors, February to
September 2020. Coverage is 82.4%: the remaining 17.6% of the timeline has no data, in
331 recorded gaps.

**Three things this API will not do to you.**

* *Silently truncate.* Any series that hits a result cap is returned with
  `truncated: true`.
* *Hide data quality.* 3.35% of the archive is held data from a logger freeze - non-null,
  in range, and not a measurement. It is returned with its quality code, never filtered
  out behind your back.
* *Return an unusable number of points.* `resolution=auto` picks raw, hourly or daily
  from the width of the window.

**Thresholds are data, not code.** Every severity boundary traces to a row in
`analytics.SensorBaseline` or to a setpoint published by the equipment owner.
"""

TAGS = [
    {"name": "meta", "description": "Health, limits and service metadata."},
    {"name": "assets", "description": "Equipment and sensor definitions."},
    {"name": "readings", "description": "Historical values and trends."},
    {"name": "operations", "description": "Anomalies, failures and overview."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Report configuration and connectivity at startup, without refusing to start.

    A database that is down at boot should produce a service whose `/health` says so,
    not a process that exits. The operator then has something to query.
    """
    settings = get_settings()
    configure_logging(settings, logger_name="api", log_file="api.log")
    logger.info("Starting API v%s", __version__)
    logger.info("Database: %s", settings.safe_connection_summary())
    try:
        with connect(settings=settings) as conn:
            cursor = conn.cursor()
            count = cursor.execute("SELECT COUNT_BIG(*) FROM ts.SensorReading").fetchone()[0]
            cursor.close()
        logger.info("Archive holds %s readings", f"{int(count):,}")
    except DatabaseError as exc:
        logger.error("Database unreachable at startup: %s", exc)
        logger.error("The API will start; /health will report the failure.")
    yield
    logger.info("API shutting down")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Industrial Time-Series Monitoring API",
        description=DESCRIPTION,
        version=__version__,
        openapi_tags=TAGS,
        lifespan=lifespan,
        contact={"name": "Project documentation", "url": "https://archive.ics.uci.edu/dataset/791/metropt+3+dataset"},
        license_info={"name": "Dataset: UCI MetroPT-3, DOI 10.24432/C5VW3R"},
    )

    # Explicit origins, never "*": the dashboard is the only intended browser client.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.api_cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-ms"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Tag every request with an id and a duration.

        The id is what makes a client-reported problem findable in the log; the duration
        is what makes a slow endpoint visible before a user has to report it.
        """
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"

        log = logger.warning if elapsed_ms > 1000 else logger.info
        log("%s %s -> %d in %.1f ms [%s]", request.method, request.url.path,
            response.status_code, elapsed_ms, request_id)
        return response

    register_error_handlers(app)

    app.include_router(operations.router)
    app.include_router(assets.router)
    app.include_router(readings.router)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    return app


app = create_app()
