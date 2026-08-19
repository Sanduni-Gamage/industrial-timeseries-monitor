"""Shared FastAPI dependencies.

The repository is provided through a dependency so tests can swap in a fake with
``app.dependency_overrides``. That single indirection is what lets the entire HTTP
surface be tested with no SQL Server running, which in turn is what keeps CI free of
database infrastructure.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

from fastapi import Query

from api.errors import InvalidTimeRangeError
from api.services.repository import Repository
from ingestion.config import Settings, get_settings
from ingestion.db import connect

#: Widest window a single request may ask for. The archive is ~213 days, so this permits
#: the whole thing while refusing an accidental decade that would scan every index.
MAX_WINDOW_DAYS = 400

#: Applied when a caller supplies neither bound. Seven days is a useful default view and
#: cheap to serve from the aggregate tables.
DEFAULT_WINDOW_DAYS = 7


def get_app_settings() -> Settings:
    return get_settings()


def get_repository() -> Iterator[Repository]:
    """Yield a repository bound to a live connection, closed after the request.

    Connection creation is cheap because the ODBC driver manager pools underneath. An
    application-level pool would add a second layer of lifecycle bugs for no measured
    benefit at this scale.
    """
    with connect() as conn:
        yield Repository(conn)


def resolve_window(
    start: datetime | None,
    end: datetime | None,
    *,
    fallback_end: datetime | None = None,
    default_days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[datetime, datetime]:
    """Turn optional bounds into a validated half-open [start, end) window.

    Half-open so adjacent windows tile without double-counting the boundary sample.
    Defaults anchor to the end of the archive rather than to now: against the wall clock a
    "last 7 days" default over 2020 data returns nothing and looks like a broken API.
    """
    anchor = fallback_end or datetime.now()

    # Resolved into new locals that are unconditionally datetimes, rather than assigning
    # back into the optional parameters. The earlier version was correct at runtime but
    # unprovable - a type checker cannot follow narrowing through reassignment, and a
    # reader skimming it has to hold the whole branch table in their head to be sure
    # neither value can still be None by the end.
    if end is not None:
        resolved_end: datetime = end
    elif start is not None:
        candidate = start + timedelta(days=default_days)
        resolved_end = min(candidate, anchor) if anchor > start else candidate
    else:
        resolved_end = anchor

    if start is not None:
        resolved_start: datetime = start
    else:
        resolved_start = resolved_end - timedelta(days=default_days)

    if resolved_start >= resolved_end:
        raise InvalidTimeRangeError(
            f"start ({resolved_start.isoformat()}) must be earlier than "
            f"end ({resolved_end.isoformat()})."
        )
    if (resolved_end - resolved_start) > timedelta(days=MAX_WINDOW_DAYS):
        raise InvalidTimeRangeError(
            f"Requested window of {(resolved_end - resolved_start).days} days exceeds the "
            f"{MAX_WINDOW_DAYS}-day maximum. Narrow the range, or request a coarser "
            f"resolution."
        )
    return resolved_start, resolved_end


# Reusable query parameters, declared once so every endpoint documents them identically.
StartQuery = Query(default=None, description="Inclusive start of the window (ISO 8601). "
                                             "Defaults to the end of the archive minus "
                                             "the default window.")
EndQuery = Query(default=None, description="Exclusive end of the window (ISO 8601).")
LimitQuery = Query(default=100, ge=1, le=1000, description="Maximum items to return.")
OffsetQuery = Query(default=0, ge=0, description="Items to skip, for pagination.")
