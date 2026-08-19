"""Reshape validated wide rows into the narrow tag/value form the database stores.

One source row carrying 15 signals becomes 15 reading rows. The output is emitted in
tag-major order - every reading for sensor 1, then every reading for sensor 2 - which
matches the ``(SensorId, ReadingTs)`` clustered key exactly. Rows therefore arrive at the
index in physical order and append to the end of each key range instead of scattering
inserts across it.
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import pandas as pd

from ingestion.loader import TIMESTAMP_COLUMN, SensorDefinition
from ingestion.quality import QualityCode

logger = logging.getLogger(__name__)

#: One row bound for ``ts.SensorReading``: (SensorId, ReadingTs, Value, QualityCodeId).
ReadingRow = tuple[int, datetime, float, int]


def to_reading_rows(
    frame: pd.DataFrame,
    quality: np.ndarray,
    sensors: list[SensorDefinition],
) -> list[ReadingRow]:
    """Convert a validated chunk into database-ready reading tuples.

    Cells marked ``BAD_MISSING`` are omitted. ``ts.SensorReading.Value`` is ``NOT NULL``
    by design, and inventing a substitute value to satisfy the constraint would be worse
    than an absent row: a fabricated zero silently skews every average computed over it.
    The omission is still counted and reported as a ``MISSING_VALUE`` issue.
    """
    row_count = len(frame)
    if row_count == 0:
        return []

    if quality.shape != (row_count, len(sensors)):
        raise ValueError(
            f"Quality matrix shape {quality.shape} does not match "
            f"({row_count}, {len(sensors)}) - frame and sensor list are out of step."
        )

    columns = [sensor.source_column for sensor in sensors]
    sensor_ids = np.array([sensor.sensor_id for sensor in sensors], dtype=np.int32)

    # DATETIME2 binds from datetime.datetime. Microsecond resolution is kept here and
    # truncated to milliseconds by the column type, which is finer than the 10 s
    # sampling interval by five orders of magnitude.
    timestamps = frame[TIMESTAMP_COLUMN].to_numpy(dtype="datetime64[us]").astype(object)

    values = frame[columns].to_numpy(dtype=np.float64)

    # Transpose to tag-major, then flatten. `.T.ravel()` walks sensor by sensor.
    flat_values = values.T.ravel()
    flat_quality = quality.T.ravel()
    flat_sensors = np.repeat(sensor_ids, row_count)
    flat_timestamps = np.tile(timestamps, len(sensors))

    keep = flat_quality != QualityCode.BAD_MISSING
    dropped = int((~keep).sum())
    if dropped:
        logger.debug("Skipped %s reading(s) with no value", f"{dropped:,}")

    return list(
        zip(
            flat_sensors[keep].tolist(),
            flat_timestamps[keep].tolist(),
            flat_values[keep].tolist(),
            flat_quality[keep].tolist(),
            strict=True,
        )
    )
