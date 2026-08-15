"""Equipment, sensors and per-equipment health."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from api.dependencies import get_repository
from api.models.schemas import Equipment, EquipmentHealth, ProblemDetail, Sensor
from api.services.repository import ACTIVE_WINDOW_HOURS, Repository

router = APIRouter(tags=["assets"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ProblemDetail, "description": "No such resource"}}


def _equipment(row: tuple) -> Equipment:
    return Equipment(
        equipment_id=row[0], equipment_code=row[1], equipment_name=row[2],
        equipment_type=row[3], location=row[4], description=row[5],
        is_active=bool(row[6]), sensor_count=int(row[7]),
    )


def _sensor(row: tuple) -> Sensor:
    return Sensor(
        sensor_id=row[0], equipment_id=row[1], sensor_code=row[2], sensor_name=row[3],
        sensor_class=row[4], measurement_type=row[5], unit=row[6],
        physical_min=row[7], physical_max=row[8], documented_setpoint=row[9],
        description=row[10], is_active=bool(row[11]),
    )


def _health(row: tuple) -> EquipmentHealth:
    status = row[9]
    critical, warning = int(row[6] or 0), int(row[7] or 0)

    # The status is only useful if the operator can see what caused it.
    if status == "NO DATA":
        reason = "No readings have been ingested for this equipment."
    elif critical:
        reason = (f"{critical} critical anomal{'y' if critical == 1 else 'ies'} in the "
                  f"last {ACTIVE_WINDOW_HOURS} hours of the archive.")
    elif warning:
        reason = (f"{warning} warning-level anomal{'y' if warning == 1 else 'ies'} in "
                  f"the last {ACTIVE_WINDOW_HOURS} hours of the archive.")
    else:
        reason = (f"No anomalies in the last {ACTIVE_WINDOW_HOURS} hours of the archive.")

    return EquipmentHealth(
        equipment_id=row[0], equipment_code=row[1], equipment_name=row[2],
        status=status, status_reason=reason, sensor_count=int(row[3] or 0),
        last_reading_at=row[4], total_readings=int(row[5] or 0),
        active_critical_anomalies=critical, active_warning_anomalies=warning,
        recorded_failures=int(row[8] or 0), active_window_hours=ACTIVE_WINDOW_HOURS,
    )


@router.get("/equipment", response_model=list[Equipment], summary="List equipment")
def list_equipment(repo: Repository = Depends(get_repository)) -> list[Equipment]:
    """Every monitored asset, with its active sensor count."""
    return [_equipment(row) for row in repo.list_equipment()]


@router.get("/equipment/{equipment_id}", response_model=Equipment, responses=NOT_FOUND,
            summary="Get one piece of equipment")
def get_equipment(
    equipment_id: int = Path(ge=1),
    repo: Repository = Depends(get_repository),
) -> Equipment:
    return _equipment(repo.get_equipment(equipment_id))


@router.get("/equipment/{equipment_id}/health", response_model=EquipmentHealth,
            responses=NOT_FOUND, summary="Current health of one piece of equipment")
def get_equipment_health(
    equipment_id: int = Path(ge=1),
    repo: Repository = Depends(get_repository),
) -> EquipmentHealth:
    """Health over a trailing window, not over all history.

    Anomalies are counted across the last 24 hours of the archive. A status that only
    accumulates can never return to NORMAL, which makes it a history rather than a
    status.
    """
    repo.get_equipment(equipment_id)          # 404 before reporting on nothing
    rows = repo.equipment_health(equipment_id)
    return _health(rows[0])


@router.get("/sensors", response_model=list[Sensor], summary="List sensors")
def list_sensors(
    equipment_id: int | None = Query(default=None, ge=1),
    sensor_class: str | None = Query(default=None, pattern="^(Analogue|Digital)$"),
    repo: Repository = Depends(get_repository),
) -> list[Sensor]:
    """Sensor definitions, including the verbatim description from the dataset docs."""
    return [_sensor(row) for row in repo.list_sensors(equipment_id, sensor_class)]


@router.get("/sensors/{sensor_id}", response_model=Sensor, responses=NOT_FOUND,
            summary="Get one sensor")
def get_sensor(
    sensor_id: str = Path(description="Numeric id or sensor code, e.g. 15 or TP3."),
    repo: Repository = Depends(get_repository),
) -> Sensor:
    return _sensor(repo.resolve_sensor(sensor_id))
