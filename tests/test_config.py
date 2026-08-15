"""Configuration tests.

Two things matter here and neither is about convenience. First, a credential must never
reach a log file or an exception message. Second, a misconfiguration must fail loudly at
startup rather than quietly at row 800,000.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ingestion.config import PROJECT_ROOT, Settings


def make(**overrides) -> Settings:
    """Build settings isolated from the developer's own .env."""
    base = {"_env_file": None, "db_server": "s", "db_database": "d",
            "db_trusted_connection": True}
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------------

def test_password_is_not_exposed_by_repr():
    """Settings objects end up in tracebacks and debug logs. A plain string password
    would be printed by every one of them."""
    settings = make(db_trusted_connection=False, db_username="svc",
                    db_password="hunter2")

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)
    assert "hunter2" not in repr(settings.db_password)
    assert settings.db_password.get_secret_value() == "hunter2"


def test_connection_summary_never_contains_the_password():
    """This string is written to log files and health reports by design."""
    settings = make(db_trusted_connection=False, db_username="svc",
                    db_password="hunter2")
    summary = settings.safe_connection_summary()

    assert "hunter2" not in summary
    assert "svc" in summary, "the account is useful for diagnosis; the secret is not"


def test_blank_env_values_are_treated_as_unset():
    """.env.example ships `DB_PASSWORD=` so a reader can see the key exists. Without
    coercion that becomes an empty string, and an empty SecretStr is a TRUTHY object -
    which would let the credential check below pass on a config that cannot connect."""
    settings = make(db_username="", db_password="")
    assert settings.db_username is None
    assert settings.db_password is None


def test_sql_auth_without_a_password_is_rejected():
    with pytest.raises(ValidationError, match="DB_USERNAME and DB_PASSWORD"):
        make(db_trusted_connection=False, db_username="svc", db_password="")


def test_sql_auth_without_a_username_is_rejected():
    with pytest.raises(ValidationError, match="DB_USERNAME and DB_PASSWORD"):
        make(db_trusted_connection=False, db_username=None, db_password="hunter2")


def test_windows_auth_needs_no_credentials():
    settings = make(db_trusted_connection=True)
    assert "Trusted_Connection=yes" in settings.odbc_connection_string()
    assert "PWD=" not in settings.odbc_connection_string()


# ---------------------------------------------------------------------------------
# Connection strings
# ---------------------------------------------------------------------------------

def test_driver_18_gets_trust_server_certificate():
    """ODBC Driver 18 defaults to Encrypt=yes and rejects the self-signed certificate a
    local SQL Server generates. Without this the connection fails with an error that
    reads like a network problem."""
    assert "TrustServerCertificate=yes" in make().odbc_connection_string()


def test_database_can_be_overridden_for_bootstrap():
    """CREATE DATABASE has to run through master, before the target exists."""
    assert "DATABASE=master" in make().odbc_connection_string(database="master")


def test_sqlalchemy_url_is_percent_encoded():
    """A named instance contains a backslash and the driver name contains spaces. Both
    would corrupt the URL if passed through raw."""
    url = make(db_server=r"localhost\SQLEXPRESS01").sqlalchemy_url()
    assert url.startswith("mssql+pyodbc:///?odbc_connect=")
    assert " " not in url
    assert "\\" not in url


# ---------------------------------------------------------------------------------
# Paths and thresholds
# ---------------------------------------------------------------------------------

def test_relative_paths_resolve_against_the_repo_not_the_cwd():
    """The pipeline must behave identically whether it is run from the repository root,
    from scripts/, or from a scheduled task with an arbitrary working directory."""
    settings = make(metropt_raw_csv=Path("data/raw/x.csv"))
    assert settings.metropt_raw_csv.is_absolute()
    assert settings.metropt_raw_csv == (PROJECT_ROOT / "data/raw/x.csv").resolve()


def test_absolute_paths_are_left_alone():
    absolute = Path(r"C:\elsewhere\x.csv") if Path("C:/").exists() else Path("/tmp/x.csv")
    assert make(metropt_raw_csv=absolute).metropt_raw_csv == absolute


def test_log_level_is_validated_and_normalised():
    assert make(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        make(log_level="CHATTY")


@pytest.mark.parametrize(
    "field, value",
    [
        ("gap_threshold_seconds", 0),      # a zero-second gap threshold is meaningless
        ("flatline_min_samples", 1),       # one sample cannot be a run
        ("xsensor_divergence_bar", 0.0),   # zero tolerance would flag every reading
        ("ingest_chunk_rows", 10),         # below the sane floor
        ("api_port", 70000),
    ],
)
def test_nonsensical_thresholds_are_rejected(field, value):
    """These bounds exist because a typo in .env should fail at startup, not produce a
    load that silently flags everything or nothing."""
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_documented_defaults_match_the_profile():
    """The defaults are derived from measurements in docs/DATA_PROFILE.md. If someone
    changes them, this test should make them justify it."""
    settings = make()
    assert settings.gap_threshold_seconds == 30    # 3x the measured 10 s modal interval
    assert settings.flatline_min_samples == 6      # 60 s at that interval
    assert settings.xsensor_divergence_bar == 0.5  # ~3x the largest observed divergence
