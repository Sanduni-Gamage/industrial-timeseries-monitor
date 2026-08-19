"""Shared fixtures.

Every test in this suite runs without a database. That is deliberate: the validation
and transformation logic is where the bugs live, it is pure, and keeping it testable
without SQL Server means CI needs no paid infrastructure and the suite runs in seconds.

The database-facing code (`ingestion/db.py`, `ingestion/writer.py`) is exercised by the
integration checks in `scripts/init_database.py` and `scripts/run_queries.py`, which need
a real server.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ingestion.config import Settings
from ingestion.loader import INDEX_COLUMN, TIMESTAMP_COLUMN, SensorDefinition

# The seven analogue and eight digital signals, with the plausibility limits actually
# seeded by database/seed.sql. Kept in step with the seed on purpose: if the limits
# change there and not here, the tests that assert on range behaviour should fail.
ANALOGUE_SPECS = [
    ("TP2", "TP2", -1.0, 16.0),
    ("TP3", "TP3", -1.0, 16.0),
    ("H1", "H1", -1.0, 16.0),
    ("DV_PRESSURE", "DV_pressure", -1.0, 16.0),
    ("RESERVOIRS", "Reservoirs", -1.0, 16.0),
    ("OIL_TEMPERATURE", "Oil_temperature", -20.0, 150.0),
    ("MOTOR_CURRENT", "Motor_current", -1.0, 30.0),
]
DIGITAL_SPECS = [
    ("COMP", "COMP"),
    ("DV_ELECTRIC", "DV_eletric"),
    ("TOWERS", "Towers"),
    ("MPG", "MPG"),
    ("LPS", "LPS"),
    ("PRESSURE_SWITCH", "Pressure_switch"),
    ("OIL_LEVEL", "Oil_level"),
    ("CAUDAL_IMPULSES", "Caudal_impulses"),
]


@pytest.fixture
def sensors() -> list[SensorDefinition]:
    """The 15 sensor definitions the pipeline would read from `asset.Sensor`."""
    definitions: list[SensorDefinition] = []
    sensor_id = 1
    for code, column, low, high in ANALOGUE_SPECS:
        definitions.append(
            SensorDefinition(
                sensor_id=sensor_id, sensor_code=code, source_column=column,
                sensor_class="Analogue", unit="bar", physical_min=low, physical_max=high,
            )
        )
        sensor_id += 1
    for code, column in DIGITAL_SPECS:
        definitions.append(
            SensorDefinition(
                sensor_id=sensor_id, sensor_code=code, source_column=column,
                sensor_class="Digital", unit=None, physical_min=0.0, physical_max=1.0,
            )
        )
        sensor_id += 1
    return definitions


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolated from the developer's own .env.

    ``_env_file=None`` matters: without it a test would silently inherit whatever
    thresholds happen to be in the local .env, and would pass or fail depending on the
    machine it ran on.
    """
    return Settings(
        _env_file=None,
        db_server="test-server",
        db_database="TestDb",
        db_trusted_connection=True,
        metropt_raw_csv=tmp_path / "source.csv",
        log_dir=tmp_path / "logs",
        gap_threshold_seconds=30,
        flatline_min_samples=6,
        xsensor_divergence_bar=0.5,
        ingest_chunk_rows=1_000,
        ingest_batch_rows=1_000,
    )


class ChunkBuilder:
    """Builds source-shaped frames, including deliberately malformed ones.

    Mirrors exactly what `ingestion.loader.iter_chunks` yields: parsed timestamps, the
    original scan ordinal, a source line number, and one float column per sensor.
    """

    def __init__(self, sensors: list[SensorDefinition]) -> None:
        self.sensors = sensors
        self.columns = [s.source_column for s in sensors]

    def build(
        self,
        timestamps: list[str | None],
        *,
        overrides: dict[str, list[float]] | None = None,
        default_analogue: float = 5.0,
        default_digital: float = 1.0,
        start_line: int = 2,
    ) -> pd.DataFrame:
        """Create a chunk.

        ``timestamps`` accepts ``None`` to represent a value the source could not parse.
        ``overrides`` sets specific columns; anything unset takes the default for its
        sensor class, which keeps each test focused on the one thing it is checking.
        """
        row_count = len(timestamps)
        data: dict[str, object] = {
            INDEX_COLUMN: np.arange(row_count, dtype=np.int64) * 10,
            TIMESTAMP_COLUMN: pd.to_datetime(
                pd.Series(timestamps, dtype="object"),
                format="%Y-%m-%d %H:%M:%S",
                errors="coerce",
            ),
        }
        for sensor in self.sensors:
            default = default_digital if sensor.is_digital else default_analogue
            data[sensor.source_column] = np.full(row_count, default, dtype=np.float64)

        frame = pd.DataFrame(data)
        for column, values in (overrides or {}).items():
            if column not in frame.columns:
                raise KeyError(f"{column!r} is not a sensor column")
            frame[column] = np.asarray(values, dtype=np.float64)

        frame["source_line_no"] = range(start_line, start_line + row_count)
        return frame

    def steady(self, count: int, *, start: str = "2020-02-01 00:00:00",
               step_seconds: int = 10, **kwargs) -> pd.DataFrame:
        """A chunk on a regular cadence, with values that change every scan.

        Values drift slightly per row so the frame is *not* accidentally a flatline -
        several tests would otherwise trip the freeze detector without meaning to.
        """
        stamps = [
            (pd.Timestamp(start) + pd.Timedelta(seconds=i * step_seconds)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            for i in range(count)
        ]
        overrides = kwargs.pop("overrides", {}) or {}
        for sensor in self.sensors:
            if not sensor.is_digital and sensor.source_column not in overrides:
                overrides[sensor.source_column] = [
                    5.0 + i * 0.001 for i in range(count)
                ]
        return self.build(stamps, overrides=overrides, **kwargs)


@pytest.fixture
def chunks(sensors: list[SensorDefinition]) -> ChunkBuilder:
    return ChunkBuilder(sensors)


@pytest.fixture
def csv_factory(tmp_path: Path):
    """Write a CSV in the exact shape of the source file, including its quirks.

    The real file's first column has an EMPTY header, which is the detail most likely to
    break a reader. Reproduced here so the loader is tested against the awkward thing it
    actually has to parse.
    """

    def _write(rows: list[str], *, header: str | None = None,
               name: str = "source.csv", encoding: str = "utf-8") -> Path:
        default_header = (
            ",timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,Oil_temperature,Motor_current,"
            "COMP,DV_eletric,Towers,MPG,LPS,Pressure_switch,Oil_level,Caudal_impulses"
        )
        path = tmp_path / name
        path.write_text(
            "\n".join([header if header is not None else default_header, *rows]) + "\n",
            encoding=encoding,
        )
        return path

    return _write


def make_row(index: int, timestamp: str, *, analogue: float = 5.0,
             digital: float = 1.0, **overrides: float) -> str:
    """Render one CSV line in source column order."""
    values = {column: analogue for _, column, _, _ in ANALOGUE_SPECS}
    values.update({column: digital for _, column in DIGITAL_SPECS})
    values.update(overrides)
    ordered = [values[column] for _, column, _, _ in ANALOGUE_SPECS]
    ordered += [values[column] for _, column in DIGITAL_SPECS]
    return f"{index},{timestamp}," + ",".join(str(v) for v in ordered)
