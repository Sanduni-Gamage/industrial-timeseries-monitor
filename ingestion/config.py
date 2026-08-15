"""Typed configuration for the ingestion pipeline.

Everything the pipeline needs to know about its environment lives here, loaded from
environment variables (or a ``.env`` file) and validated by Pydantic. No module in this
project reads ``os.environ`` directly and no module hard-codes a path or a credential.

The data-quality thresholds are deliberately configuration rather than constants: each
one was derived from a measurement in ``docs/DATA_PROFILE.md``, and a different machine
or a different export would need different values. The docstrings record where each
number came from so nobody has to guess later.
"""

from __future__ import annotations

import urllib.parse
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root - two levels up from this file (``<root>/ingestion/config.py``).
#: Used to resolve relative paths so the pipeline behaves the same regardless of the
#: working directory it is invoked from.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Runtime configuration, validated at import time."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---------------------------------------------------------------
    db_server: str = Field(default=r"localhost\SQLEXPRESS01")
    db_database: str = Field(default="IndustrialMonitor")
    db_driver: str = Field(default="ODBC Driver 18 for SQL Server")
    db_trusted_connection: bool = Field(default=True)
    db_username: str | None = Field(default=None)
    db_password: SecretStr | None = Field(default=None)
    db_trust_server_certificate: bool = Field(default=True)
    db_connect_timeout_seconds: int = Field(default=15, ge=1, le=300)
    db_command_timeout_seconds: int = Field(default=600, ge=1)

    # --- Data source ------------------------------------------------------------
    metropt_raw_csv: Path = Field(default=Path("data/raw/MetroPT3(AirCompressor).csv"))

    # --- Ingestion tuning -------------------------------------------------------
    ingest_chunk_rows: int = Field(default=50_000, ge=1_000, le=500_000)
    ingest_batch_rows: int = Field(default=50_000, ge=1_000, le=200_000)

    # --- Data quality thresholds ------------------------------------------------
    gap_threshold_seconds: int = Field(default=30, ge=1)
    """Three times the measured 10 s modal sampling interval (DATA_PROFILE.md §3).

    The interval distribution is noisy - 1,337,521 steps of 10 s but also 128,277 of
    9 s and 38,321 of 12 s - so an exact-interval test would report a million false
    gaps. 30 s absorbs the jitter and still catches every real dropout (331 found).
    """

    flatline_min_samples: int = Field(default=6, ge=2)
    """Consecutive identical scans that constitute a data-acquisition freeze.

    Six samples at 10 s is 60 s. Longer than any genuine plateau on a duty-cycling
    compressor, short enough to catch the 24 frozen blocks found in DATA_PROFILE.md §10
    (the longest runs 51.4 hours).
    """

    xsensor_divergence_bar: float = Field(default=0.5, gt=0)
    """Limit on |Reservoirs - TP3|, which the UCI docs say should stay close.

    Measured mean divergence is 0.0019 bar and the maximum ever observed is 0.182 bar,
    so 0.5 bar flags instrument failure rather than noise.
    """

    # --- Logging ----------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_dir: Path = Field(default=Path("logs"))

    # --- API (used from Phase 4) ------------------------------------------------
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_cors_origins: str = Field(default="http://localhost:5173")

    # --- Validation -------------------------------------------------------------

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return upper

    @field_validator("metropt_raw_csv", "log_dir")
    @classmethod
    def _resolve_against_root(cls, value: Path) -> Path:
        """Make relative paths absolute against the repository root, not the CWD."""
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("db_username", "db_password", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat a blank env var as unset.

        ``.env.example`` ships ``DB_USERNAME=`` and ``DB_PASSWORD=`` so a reader can see
        the keys exist. Without this, those become empty strings - and an empty
        ``SecretStr`` is a truthy object, which would let the credential check below
        pass on a configuration that cannot actually authenticate.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_credentials(self) -> Settings:
        """SQL authentication needs both parts; Windows authentication needs neither."""
        if self.db_trusted_connection:
            return self
        password = self.db_password.get_secret_value() if self.db_password else ""
        if not self.db_username or not password:
            raise ValueError(
                "DB_TRUSTED_CONNECTION is false, so DB_USERNAME and DB_PASSWORD are both "
                "required. Set DB_TRUSTED_CONNECTION=true to use Windows authentication."
            )
        return self

    # --- Derived values ---------------------------------------------------------

    def odbc_connection_string(self, *, database: str | None = None) -> str:
        """Build an ODBC connection string.

        Pass ``database='master'`` to connect before the application database exists.
        The password is only materialised here, at the point of use, and is never
        logged: ``__repr__`` on ``SecretStr`` prints ``**********``.
        """
        parts = [
            f"DRIVER={{{self.db_driver}}}",
            f"SERVER={self.db_server}",
            f"DATABASE={database or self.db_database}",
            f"Connect Timeout={self.db_connect_timeout_seconds}",
        ]
        if self.db_trusted_connection:
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={self.db_username}")
            parts.append(f"PWD={self.db_password.get_secret_value() if self.db_password else ''}")
        if self.db_trust_server_certificate:
            # ODBC Driver 18 defaults to Encrypt=yes and rejects the self-signed
            # certificate a local SQL Server generates. Required for local dev.
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts) + ";"

    def sqlalchemy_url(self, *, database: str | None = None) -> str:
        """SQLAlchemy URL wrapping the ODBC string, correctly percent-encoded."""
        quoted = urllib.parse.quote_plus(self.odbc_connection_string(database=database))
        return f"mssql+pyodbc:///?odbc_connect={quoted}"

    def safe_connection_summary(self) -> str:
        """A one-line description safe to write to a log file or a health report."""
        auth = "Windows auth" if self.db_trusted_connection else f"SQL auth as {self.db_username}"
        return f"{self.db_server}/{self.db_database} ({auth}, {self.db_driver})"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that a misconfigured environment fails once, loudly, at first use rather
    than intermittently deep inside a loop.
    """
    return Settings()
