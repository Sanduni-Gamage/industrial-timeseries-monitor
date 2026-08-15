"""RFC 7807 problem responses.

Errors are part of the API contract, not an afterthought. A client should be able to
branch on a stable ``type`` rather than pattern-matching prose that will be reworded, and
an operator reading a failed request should learn what to do differently.

Every handler here also guarantees one thing that matters more than the format: a
database error never reaches the client. Connection strings and SQL text can carry
server names, schema details and occasionally credentials, so the caller gets a stable
message and the detail goes to the log.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.services.repository import NotFoundError
from ingestion.db import DatabaseError

logger = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"

#: Starlette renamed this constant; the old spelling is deprecated but still present.
#: Resolved once here rather than being deprecation-warned at every raise site.
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT",
                   status.HTTP_422_UNPROCESSABLE_ENTITY)

# Stable problem type URIs. These are identifiers, not URLs to fetch.
TYPE_NOT_FOUND = "/problems/not-found"
TYPE_VALIDATION = "/problems/validation-error"
TYPE_INVALID_RANGE = "/problems/invalid-time-range"
TYPE_UPSTREAM = "/problems/database-unavailable"
TYPE_INTERNAL = "/problems/internal-error"


class InvalidTimeRangeError(ValueError):
    """A time window that cannot be satisfied - start after end, or absurdly wide."""


def problem(status_code: int, type_uri: str, title: str, detail: str,
            instance: str | None = None) -> JSONResponse:
    body = {"type": type_uri, "title": title, "status": status_code, "detail": detail}
    if instance:
        body["instance"] = instance
    return JSONResponse(status_code=status_code, content=body,
                        media_type=PROBLEM_MEDIA_TYPE)


def register_error_handlers(app: FastAPI) -> None:
    """Attach handlers so every failure leaves as a problem document."""

    @app.exception_handler(NotFoundError)
    def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return problem(status.HTTP_404_NOT_FOUND, TYPE_NOT_FOUND, "Resource not found",
                       str(exc), str(request.url.path))

    @app.exception_handler(InvalidTimeRangeError)
    def _invalid_range(request: Request, exc: InvalidTimeRangeError) -> JSONResponse:
        # 422 rather than 400: the request is syntactically valid and semantically wrong,
        # which is exactly the distinction 422 exists to make.
        return problem(HTTP_422, TYPE_INVALID_RANGE,
                       "Invalid time range", str(exc), str(request.url.path))

    @app.exception_handler(RequestValidationError)
    def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default body is a list of dicts under "detail", which does not match
        # the problem shape every other error uses. Flattened here so clients have one
        # error format to handle.
        parts = []
        for error in exc.errors():
            location = " -> ".join(str(p) for p in error.get("loc", ()) if p != "body")
            parts.append(f"{location}: {error.get('msg', 'invalid')}")
        return problem(HTTP_422, TYPE_VALIDATION,
                       "Request validation failed", "; ".join(parts) or "Invalid request.",
                       str(request.url.path))

    @app.exception_handler(DatabaseError)
    def _database(request: Request, exc: DatabaseError) -> JSONResponse:
        reference = uuid.uuid4().hex[:12]
        # The real error goes to the log; the client gets a reference to quote. Database
        # messages can contain server names and SQL text.
        logger.error("Database error [%s] on %s: %s", reference, request.url.path, exc)
        return problem(
            status.HTTP_503_SERVICE_UNAVAILABLE, TYPE_UPSTREAM,
            "Database unavailable",
            f"The database could not be reached. Quote reference {reference} when "
            f"reporting this.",
            str(request.url.path),
        )

    @app.exception_handler(StarletteHTTPException)
    def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, f"/problems/http-{exc.status_code}",
                       str(exc.detail), str(exc.detail), str(request.url.path))

    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        reference = uuid.uuid4().hex[:12]
        logger.exception("Unhandled error [%s] on %s", reference, request.url.path)
        return problem(
            status.HTTP_500_INTERNAL_SERVER_ERROR, TYPE_INTERNAL, "Internal server error",
            f"An unexpected error occurred. Quote reference {reference} when reporting "
            f"this.",
            str(request.url.path),
        )
