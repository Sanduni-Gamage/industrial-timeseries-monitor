"""Reading the source CSV and the sensor metadata that drives the load.

The loader knows nothing about validation or the database schema. Its two jobs are to
hand out the source file in bounded chunks, and to fetch the sensor definitions that
tell the rest of the pipeline which CSV column maps to which ``SensorId``.

The file is streamed rather than read whole. At 208 MB the whole-file read would fit in
memory here, but the endpoint publishes no ``Content-Length`` (DEV_LOG DL-004), so an
unexpectedly large file must degrade throughput instead of exhausting memory.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyodbc

from ingestion.db import fetch_all

logger = logging.getLogger(__name__)

#: The source file's first column has an EMPTY header. Naming it explicitly stops pandas
#: inventing ``Unnamed: 0``, which would silently change if the export ever gained a
#: header for that column.
INDEX_COLUMN = "source_index"
TIMESTAMP_COLUMN = "timestamp"

#: Timestamp layout in the source file. Given explicitly rather than left to inference:
#: pandas can infer a different format per chunk, which would silently transpose day and
#: month for the first twelve days of any month.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class SensorDefinition:
    """One row of ``asset.Sensor``, as the pipeline needs it."""

    sensor_id: int
    sensor_code: str
    source_column: str
    sensor_class: str
    unit: str | None
    physical_min: float | None
    physical_max: float | None

    @property
    def is_digital(self) -> bool:
        return self.sensor_class == "Digital"


@dataclass(frozen=True)
class SourceFile:
    """Identity of the file being ingested, for the run ledger."""

    path: Path
    size_bytes: int
    sha256: str


def describe_source(path: Path, *, chunk_bytes: int = 8 * 1024 * 1024) -> SourceFile:
    """Hash and measure the source file.

    The hash is what makes re-ingestion detectable rather than merely harmless: a run
    whose file hash and row count match an earlier successful run can be skipped
    outright instead of re-verified row by row.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(block)
    stat = path.stat()
    return SourceFile(path=path, size_bytes=stat.st_size, sha256=digest.hexdigest())


def load_sensor_definitions(conn: pyodbc.Connection) -> list[SensorDefinition]:
    """Read active sensors from the database.

    This is what makes ingestion data-driven: adding a sixteenth signal is a row in
    ``asset.Sensor``, not a code change here.
    """
    rows = fetch_all(
        conn,
        """
        SELECT SensorId, SensorCode, SourceColumn, SensorClass, Unit, PhysicalMin, PhysicalMax
        FROM asset.Sensor
        WHERE IsActive = 1
        ORDER BY DisplayOrder, SensorId
        """,
    )
    definitions = [
        SensorDefinition(
            sensor_id=row[0],
            sensor_code=row[1],
            source_column=row[2],
            sensor_class=row[3],
            unit=row[4],
            physical_min=row[5],
            physical_max=row[6],
        )
        for row in rows
    ]
    if not definitions:
        raise RuntimeError(
            "No active sensors found in asset.Sensor. Run database/seed.sql "
            "(scripts/init_database.py) before ingesting."
        )
    logger.debug("Loaded %d sensor definitions", len(definitions))
    return definitions


def verify_header(path: Path, sensors: list[SensorDefinition]) -> list[str]:
    """Check the CSV header against the sensors the database expects.

    Raises on a missing column. Returns any *extra* columns as a warning list rather
    than an error: a source that gained a signal is a reason to look, not a reason to
    abort a load of the signals we do understand.
    """
    try:
        header = pd.read_csv(path, nrows=0)
    except pd.errors.EmptyDataError as exc:
        # A zero-byte file is the signature of a truncated or failed download
        # (DEV_LOG DL-006: the UCI endpoint sends no Content-Length and drops
        # connections mid-stream). Raise the same ValueError as a structurally wrong
        # header so the caller has one failure mode to handle, not two.
        raise ValueError(
            f"Source file is empty: {path}. This usually means a truncated download - "
            f"verify it against the hashes in data/README.md."
        ) from exc

    actual = list(header.columns)

    expected = {sensor.source_column for sensor in sensors}
    missing = sorted(expected - set(actual))
    if missing:
        raise ValueError(
            f"Source file is missing {len(missing)} expected column(s): {missing}. "
            f"Header was: {actual}"
        )

    known = expected | {TIMESTAMP_COLUMN, "", "Unnamed: 0", INDEX_COLUMN}
    return sorted(set(actual) - known)


def iter_chunks(
    path: Path,
    sensors: list[SensorDefinition],
    *,
    chunk_rows: int,
) -> Iterator[pd.DataFrame]:
    """Yield the source file in chunks with timestamps parsed and a line number attached.

    Each yielded frame carries:
      - ``timestamp``      parsed to datetime64, ``NaT`` where unparseable
      - ``source_index``   the original 1 Hz scan ordinal from the file
      - ``source_line_no`` 1-based line number in the file, for quarantine traceability
      - one float column per sensor, named by its ``SourceColumn``

    Unparseable timestamps become ``NaT`` rather than raising, so they can be counted
    and quarantined instead of killing the whole read.
    """
    signal_columns = [sensor.source_column for sensor in sensors]

    # Columns are selected BY NAME, not by position.
    #
    # An earlier version supplied a positional `names` list covering exactly 17 columns.
    # That reads the real file correctly and breaks on any file with an extra column -
    # pandas treats the surplus leading columns as an index and every subsequent field
    # shifts, so the timestamp lands in the integer index column and the read dies with
    # "invalid literal for int()".
    #
    # That directly contradicted verify_header(), which deliberately treats an extra
    # column as a warning rather than an error. Selecting by name honours that contract:
    # a source that gains a signal is still readable for the signals we do understand.
    try:
        header = pd.read_csv(path, nrows=0)
    except pd.errors.EmptyDataError:
        # A zero-byte file has no header to read. Yield nothing rather than raising:
        # verify_header() already turns this into a clear, actionable failure, and the
        # pipeline's own "zero rows read" guard catches it either way. The reader's job
        # is to report what is there, not to decide that nothing is a crisis.
        return
    actual = list(header.columns)
    if not actual:
        return

    # The source file's first column has an empty header, which pandas renders as
    # "Unnamed: 0". Whatever it is called, position 0 is the scan ordinal.
    index_source_name = actual[0]

    wanted = [index_source_name, TIMESTAMP_COLUMN, *signal_columns]
    dtypes: dict[str, str] = dict.fromkeys(signal_columns, "float64")
    dtypes[index_source_name] = "int64"

    reader = pd.read_csv(
        path,
        header=0,
        usecols=[c for c in wanted if c in actual],
        dtype=dtypes,
        chunksize=chunk_rows,
        low_memory=False,
    )

    line_offset = 2  # line 1 is the header; data starts at line 2
    for chunk in reader:
        chunk = chunk.rename(columns={index_source_name: INDEX_COLUMN})
        chunk[TIMESTAMP_COLUMN] = pd.to_datetime(
            chunk[TIMESTAMP_COLUMN], format=TIMESTAMP_FORMAT, errors="coerce"
        )
        chunk["source_line_no"] = range(line_offset, line_offset + len(chunk))
        line_offset += len(chunk)
        yield chunk.reset_index(drop=True)
