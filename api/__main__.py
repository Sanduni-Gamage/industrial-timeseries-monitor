"""Run the API server.

    python -m api

Reads API_HOST and API_PORT from configuration so the port is never hard-coded in a
script or a README that will drift.
"""

from __future__ import annotations

import uvicorn

from ingestion.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
