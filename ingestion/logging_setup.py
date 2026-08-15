"""Structured logging shared by every entry point.

Console output uses the ``[LEVEL] message`` form the brief asks for - readable by an
operator watching a PowerShell window. The rotating file handler keeps the same content
plus timestamps and module names, so a failed overnight run can still be diagnosed.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import sys
from pathlib import Path

from ingestion.config import Settings

CONSOLE_FORMAT = "[%(levelname)s] %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

_configured: set[str] = set()


def configure_logging(
    settings: Settings,
    *,
    logger_name: str = "app",
    log_file: str | None = None,
) -> logging.Logger:
    """Configure root logging once per process and return the named logger.

    Repeated calls are safe: handlers are attached only on the first call, so importing
    two modules that both configure logging does not produce duplicated output.
    """
    root = logging.getLogger()
    if "root" not in _configured:
        root.setLevel(settings.log_level)

        # A Windows console defaults to a legacy code page (cp1252 here), which turns
        # any non-ASCII character in a log line into a replacement glyph. Sensor
        # descriptions carry degree signs and the dataset authors' names carry accents,
        # so this is not hypothetical. errors="replace" keeps a console that genuinely
        # cannot render a character from taking the process down mid-load.
        # Not a real stream (captured output under pytest, a redirected handle):
        # nothing to reconfigure, and nothing worth failing over.
        with contextlib.suppress(AttributeError, OSError):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        console.setLevel(settings.log_level)
        root.addHandler(console)

        log_dir: Path = settings.log_dir
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_dir / (log_file or f"{logger_name}.log"),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(FILE_FORMAT))
            handler.setLevel(logging.DEBUG)
            root.addHandler(handler)
        except OSError as exc:
            # A missing or read-only log directory must not stop a pipeline run; say so
            # on the console and carry on with console logging only.
            root.warning("File logging disabled (%s): %s", log_dir, exc)

        # pyodbc and urllib chatter at DEBUG drowns out anything useful.
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        _configured.add("root")

    return logging.getLogger(logger_name)
