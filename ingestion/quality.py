"""Quality codes and data-quality issue types.

The numeric codes mirror the OPC DA convention (Good 192+, Uncertain 64+, Bad 0+) and
must stay in step with the ``ref.QualityCode`` rows seeded by ``database/seed.sql``.
They are defined here as well so that the pipeline can assign them without a database
round trip per cell, and so unit tests can assert on them without a database at all.
"""

from __future__ import annotations

from enum import IntEnum


class QualityCode(IntEnum):
    """Per-reading trust marker, stored alongside every value."""

    GOOD = 192
    GOOD_SUBSTITUTED = 200
    UNCERTAIN_RANGE = 64
    UNCERTAIN_STALE = 65
    BAD_MISSING = 0
    BAD_RANGE = 8
    BAD_DIGITAL = 16

    @property
    def is_usable(self) -> bool:
        """Whether analytics may include readings carrying this code."""
        return self >= QualityCode.UNCERTAIN_RANGE


class IssueType:
    """Vocabulary for ``ops.DataQualityIssue.IssueType``.

    Plain string constants rather than an enum: these values are written to the database
    and read back by the API and dashboard, so a stable string is the contract.
    """

    MISSING_VALUE = "MISSING_VALUE"
    UNPARSEABLE_TIMESTAMP = "UNPARSEABLE_TIMESTAMP"
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
    NON_MONOTONIC_TS = "NON_MONOTONIC_TS"
    GAP = "GAP"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    NON_BINARY_DIGITAL = "NON_BINARY_DIGITAL"
    FLATLINE = "FLATLINE"
    XSENSOR_DIVERGENCE = "XSENSOR_DIVERGENCE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"


class Severity:
    """Vocabulary for ``ops.DataQualityIssue.Severity``."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class RunStatus:
    """Vocabulary for ``ops.IngestionRun.Status``."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"


class RejectReason:
    """Vocabulary for ``ops.RejectedRow.ReasonCode``."""

    UNPARSEABLE_TIMESTAMP = "UNPARSEABLE_TIMESTAMP"
    NON_MONOTONIC_TIMESTAMP = "NON_MONOTONIC_TIMESTAMP"
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
    MALFORMED_ROW = "MALFORMED_ROW"
