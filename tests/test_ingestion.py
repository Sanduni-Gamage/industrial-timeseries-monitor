"""Loader and transformer tests - reading the file, and reshaping it for the database."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ingestion.loader import (
    INDEX_COLUMN,
    TIMESTAMP_COLUMN,
    iter_chunks,
    verify_header,
)
from ingestion.quality import QualityCode
from ingestion.transformer import to_reading_rows
from tests.conftest import make_row

# ---------------------------------------------------------------------------------
# Reading the source file
# ---------------------------------------------------------------------------------

def test_empty_first_header_is_named_not_inferred(csv_factory, sensors):
    """The source file's first column has an EMPTY header. Left to itself pandas invents
    'Unnamed: 0', which would change silently if the export ever gained a real header."""
    path = csv_factory([make_row(0, "2020-02-01 00:00:00")])
    chunk = next(iter_chunks(path, sensors, chunk_rows=100))

    assert INDEX_COLUMN in chunk.columns
    assert "Unnamed: 0" not in chunk.columns
    assert chunk[INDEX_COLUMN].iloc[0] == 0


def test_timestamps_parsed_and_typed(csv_factory, sensors):
    path = csv_factory([
        make_row(0, "2020-02-01 00:00:00"),
        make_row(10, "2020-02-01 00:00:10"),
    ])
    chunk = next(iter_chunks(path, sensors, chunk_rows=100))

    assert pd.api.types.is_datetime64_any_dtype(chunk[TIMESTAMP_COLUMN])
    assert chunk[TIMESTAMP_COLUMN].iloc[0] == pd.Timestamp("2020-02-01 00:00:00")


def test_unparseable_timestamp_becomes_nat_not_an_exception(csv_factory, sensors):
    """One malformed row must not abort a 1.5-million-row read. It becomes NaT so the
    validator can count and quarantine it."""
    path = csv_factory([
        make_row(0, "2020-02-01 00:00:00"),
        make_row(10, "not-a-timestamp"),
        make_row(20, "2020-02-01 00:00:20"),
    ])
    chunk = next(iter_chunks(path, sensors, chunk_rows=100))

    assert len(chunk) == 3
    assert chunk[TIMESTAMP_COLUMN].isna().sum() == 1


def test_ambiguous_date_is_not_silently_transposed(csv_factory, sensors):
    """An explicit format string, not inference. Inference can pick a different format
    per chunk, which would swap day and month for the first twelve days of any month."""
    path = csv_factory([make_row(0, "2020-03-04 07:08:09")])
    chunk = next(iter_chunks(path, sensors, chunk_rows=100))

    stamp = chunk[TIMESTAMP_COLUMN].iloc[0]
    assert (stamp.month, stamp.day) == (3, 4), "must read as 4 March, never 3 April"


def test_line_numbers_survive_chunking(csv_factory, sensors):
    """Quarantined rows must point back at the real line in the source file, which means
    the counter cannot restart at each chunk boundary."""
    rows = [make_row(i * 10, f"2020-02-01 00:00:{i:02d}") for i in range(10)]
    path = csv_factory(rows)

    chunks = list(iter_chunks(path, sensors, chunk_rows=4))
    assert [len(c) for c in chunks] == [4, 4, 2]
    assert chunks[0]["source_line_no"].tolist() == [2, 3, 4, 5]
    assert chunks[1]["source_line_no"].tolist() == [6, 7, 8, 9]
    assert chunks[2]["source_line_no"].tolist() == [10, 11]


def test_utf8_bom_header_does_not_break_the_read(csv_factory, sensors):
    """A file re-saved by Excel gains a BOM. Without utf-8-sig handling the first column
    name gains an invisible prefix and every lookup fails."""
    path = csv_factory([make_row(0, "2020-02-01 00:00:00")], encoding="utf-8-sig")
    chunk = next(iter_chunks(path, sensors, chunk_rows=100))
    assert len(chunk) == 1
    assert "TP2" in chunk.columns


def test_header_only_file_yields_an_empty_chunk(csv_factory, sensors):
    """Degrades gracefully rather than raising. The pipeline is responsible for deciding
    that zero rows is a failure - see test_pipeline.py."""
    path = csv_factory([])
    assert [len(c) for c in iter_chunks(path, sensors, chunk_rows=100)] == [0]


def test_completely_empty_file_yields_nothing(csv_factory, sensors, tmp_path):
    """A zero-byte file yields no chunks rather than raising.

    verify_header() already turns this into a clear, actionable failure and the pipeline
    refuses to report success on zero rows, so the reader's job is simply to report that
    there is nothing there."""
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    assert list(iter_chunks(path, sensors, chunk_rows=100)) == []


def test_chunk_boundary_does_not_lose_or_duplicate_rows(csv_factory, sensors):
    rows = [make_row(i * 10, f"2020-02-01 00:{i // 60:02d}:{i % 60:02d}")
            for i in range(97)]
    path = csv_factory(rows)

    chunks = list(iter_chunks(path, sensors, chunk_rows=10))
    total = sum(len(c) for c in chunks)
    indexes = pd.concat([c[INDEX_COLUMN] for c in chunks])

    assert total == 97
    assert indexes.is_unique
    assert indexes.is_monotonic_increasing


# ---------------------------------------------------------------------------------
# Header verification
# ---------------------------------------------------------------------------------

def test_missing_column_is_a_hard_error(csv_factory, sensors):
    """A signal the database expects but the file does not have is not something to
    proceed through quietly."""
    header = (",timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,Oil_temperature,"
              "Motor_current,COMP,DV_eletric,Towers,MPG,LPS,Pressure_switch,Oil_level")
    path = csv_factory([], header=header)  # Caudal_impulses missing

    with pytest.raises(ValueError, match="Caudal_impulses"):
        verify_header(path, sensors)


def test_extra_column_is_a_warning_not_an_error(csv_factory, sensors):
    """A source that gained a signal is a reason to look, not a reason to abort a load
    of the fifteen signals we do understand."""
    header = (",timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,Oil_temperature,"
              "Motor_current,COMP,DV_eletric,Towers,MPG,LPS,Pressure_switch,"
              "Oil_level,Caudal_impulses,Vibration")
    path = csv_factory([], header=header)

    assert verify_header(path, sensors) == ["Vibration"]


def test_misspelled_source_column_is_matched_exactly(csv_factory, sensors):
    """DV_eletric is misspelled in the source. The pipeline matches the file's spelling,
    because silently renaming a source column is a data-lineage bug."""
    assert verify_header(csv_factory([]), sensors) == []
    assert any(s.source_column == "DV_eletric" for s in sensors)
    assert any(s.sensor_code == "DV_ELECTRIC" for s in sensors)


# ---------------------------------------------------------------------------------
# Wide to narrow
# ---------------------------------------------------------------------------------

def test_one_source_row_becomes_one_reading_per_sensor(chunks, sensors):
    frame = chunks.steady(4)
    quality = np.full((4, 15), QualityCode.GOOD, dtype=np.uint8)

    rows = to_reading_rows(frame, quality, sensors)
    assert len(rows) == 4 * 15


def test_rows_are_emitted_tag_major(chunks, sensors):
    """Tag-major order matches the (SensorId, ReadingTs) clustered key, so rows arrive at
    the index in physical order instead of scattering inserts across it."""
    frame = chunks.steady(3)
    quality = np.full((3, 15), QualityCode.GOOD, dtype=np.uint8)

    rows = to_reading_rows(frame, quality, sensors)
    sensor_ids = [r[0] for r in rows]

    assert sensor_ids[:3] == [1, 1, 1], "all of sensor 1 before sensor 2"
    assert sensor_ids == sorted(sensor_ids)
    for start in range(0, len(rows), 3):
        block = rows[start:start + 3]
        assert [r[1] for r in block] == sorted(r[1] for r in block), (
            "within a tag, readings must be time-ascending"
        )


def test_missing_readings_are_omitted_not_substituted(chunks, sensors):
    """Value is NOT NULL by design. Inventing a zero to satisfy the constraint would be
    worse than an absent row: a fabricated value silently skews every average over it."""
    frame = chunks.steady(3)
    quality = np.full((3, 15), QualityCode.GOOD, dtype=np.uint8)
    quality[1, 0] = QualityCode.BAD_MISSING

    rows = to_reading_rows(frame, quality, sensors)

    assert len(rows) == 3 * 15 - 1
    assert not any(r[3] == QualityCode.BAD_MISSING for r in rows)


def test_quality_codes_travel_with_their_own_reading(chunks, sensors):
    """A transposition bug here would attach the wrong trust label to the wrong value -
    invisible in aggregate, wrong in every individual case."""
    frame = chunks.steady(2)
    quality = np.full((2, 15), QualityCode.GOOD, dtype=np.uint8)
    quality[0, 6] = QualityCode.BAD_RANGE          # first row, MOTOR_CURRENT
    quality[1, 7] = QualityCode.UNCERTAIN_STALE    # second row, COMP

    rows = to_reading_rows(frame, quality, sensors)
    by_key = {(r[0], r[1]): r[3] for r in rows}

    motor_id = next(s.sensor_id for s in sensors if s.sensor_code == "MOTOR_CURRENT")
    comp_id = next(s.sensor_id for s in sensors if s.sensor_code == "COMP")
    first_ts = frame[TIMESTAMP_COLUMN].iloc[0].to_pydatetime()
    second_ts = frame[TIMESTAMP_COLUMN].iloc[1].to_pydatetime()

    assert by_key[(motor_id, first_ts)] == QualityCode.BAD_RANGE
    assert by_key[(motor_id, second_ts)] == QualityCode.GOOD
    assert by_key[(comp_id, second_ts)] == QualityCode.UNCERTAIN_STALE
    assert by_key[(comp_id, first_ts)] == QualityCode.GOOD


def test_values_are_not_transposed_between_sensors(chunks, sensors):
    """The reshape flattens a transposed matrix. An axis mistake would give every
    sensor another sensor's readings - plausible-looking and completely wrong."""
    frame = chunks.build(
        ["2020-02-01 00:00:00", "2020-02-01 00:00:10"],
        overrides={"TP2": [1.5, 1.6], "Oil_temperature": [61.0, 62.0]},
    )
    quality = np.full((2, 15), QualityCode.GOOD, dtype=np.uint8)
    rows = to_reading_rows(frame, quality, sensors)

    tp2_id = next(s.sensor_id for s in sensors if s.sensor_code == "TP2")
    oil_id = next(s.sensor_id for s in sensors if s.sensor_code == "OIL_TEMPERATURE")

    assert sorted(r[2] for r in rows if r[0] == tp2_id) == [1.5, 1.6]
    assert sorted(r[2] for r in rows if r[0] == oil_id) == [61.0, 62.0]


def test_empty_frame_produces_no_rows(chunks, sensors):
    frame = chunks.build([])
    rows = to_reading_rows(frame, np.empty((0, 15), dtype=np.uint8), sensors)
    assert rows == []


def test_shape_mismatch_is_rejected_loudly(chunks, sensors):
    """A quality matrix out of step with the frame means the two came from different
    chunks. Failing fast beats writing 22 million mislabelled readings."""
    frame = chunks.steady(3)
    with pytest.raises(ValueError, match="out of step"):
        to_reading_rows(frame, np.empty((2, 15), dtype=np.uint8), sensors)


def test_timestamps_bind_as_python_datetimes(chunks, sensors):
    """pyodbc binds DATETIME2 from datetime.datetime; a numpy datetime64 would fail at
    the driver with a much less obvious error."""
    from datetime import datetime

    frame = chunks.steady(2)
    quality = np.full((2, 15), QualityCode.GOOD, dtype=np.uint8)
    rows = to_reading_rows(frame, quality, sensors)

    assert all(isinstance(r[1], datetime) for r in rows)
    assert all(isinstance(r[0], int) for r in rows)
    assert all(isinstance(r[2], float) for r in rows)


def test_non_ascii_in_the_file_does_not_break_the_read(csv_factory, sensors, tmp_path):
    """A file re-exported through a tool that adds an accented column comment, or a BOM
    plus non-ASCII, must not take the reader down. Only the configured sensor columns are
    read, so anything extra is ignored by name rather than by position."""
    header = (",timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,Oil_temperature,Motor_current,"
              "COMP,DV_eletric,Towers,MPG,LPS,Pressure_switch,Oil_level,Caudal_impulses,"
              "Notaç\u00e3o")
    row = make_row(0, "2020-02-01 00:00:00") + ",temperatura média"
    path = tmp_path / "unicode.csv"
    path.write_text("\n".join([header, row]) + "\n", encoding="utf-8")

    extra = verify_header(path, sensors)
    assert extra == ["Notaç\u00e3o"], "the unknown column is reported, not fatal"

    chunk = next(iter_chunks(path, sensors, chunk_rows=10))
    assert len(chunk) == 1
    assert chunk["TP2"].iloc[0] == 5.0


def test_single_row_file(csv_factory, sensors):
    """The smallest possible real file. Nothing downstream may assume a predecessor."""
    path = csv_factory([make_row(0, "2020-02-01 00:00:00")])
    chunks_read = list(iter_chunks(path, sensors, chunk_rows=100))
    assert len(chunks_read) == 1
    assert len(chunks_read[0]) == 1


def test_chunk_size_larger_than_the_file(csv_factory, sensors):
    path = csv_factory([make_row(i * 10, f"2020-02-01 00:00:{i:02d}") for i in range(3)])
    chunks_read = list(iter_chunks(path, sensors, chunk_rows=10_000))
    assert [len(c) for c in chunks_read] == [3]
