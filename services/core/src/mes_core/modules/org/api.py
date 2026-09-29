from datetime import time
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from mes_core.modules.org.models import Area, Line, ShiftPattern, WorkCenter
from mes_core.modules.org.public import get_plant, list_equipment, list_plants
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, require

router = APIRouter(prefix="/plants", tags=["org"])


class PlantOut(BaseModel):
    id: UUID
    code: str
    name: str
    timezone: str
    erp_plant_code: str | None


class NamedOut(BaseModel):
    id: UUID
    code: str
    name: str


class LineOut(NamedOut):
    area_id: UUID


class WorkCenterOut(NamedOut):
    line_id: UUID
    erp_work_center: str | None


class EquipmentOut(NamedOut):
    equipment_type: str
    status: str
    work_center_id: UUID
    work_center_code: str
    line_id: UUID
    line_code: str


class ShiftPatternOut(BaseModel):
    id: UUID
    line_id: UUID | None
    code: str
    name: str
    start_time: time
    end_time: time
    days_of_week: list[int]


class PlantStructureOut(BaseModel):
    plant: PlantOut
    areas: list[NamedOut]
    lines: list[LineOut]
    work_centers: list[WorkCenterOut]
    equipment: list[EquipmentOut]
    shift_patterns: list[ShiftPatternOut]


@router.get("", response_model=list[PlantOut])
async def plants(db: DbDep, user=require(Perm.ORG_READ)) -> list:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        return [p.__dict__ for p in await list_plants(s, user.plant_ids)]


@router.get("/{plant_id}/structure", response_model=PlantStructureOut)
async def structure(plant_id: UUID, db: DbDep, user=require(Perm.ORG_READ)) -> dict:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        plant = await get_plant(s, plant_id)
        areas = (await s.scalars(select(Area).where(Area.plant_id == plant_id).order_by(Area.code))).all()
        lines = (await s.scalars(select(Line).where(Line.plant_id == plant_id).order_by(Line.code))).all()
        wcs = (await s.scalars(select(WorkCenter).where(WorkCenter.plant_id == plant_id).order_by(WorkCenter.code))).all()
        shifts = (
            await s.scalars(select(ShiftPattern).where(ShiftPattern.plant_id == plant_id).order_by(ShiftPattern.start_time))
        ).all()
        equipment = await list_equipment(s, plant_id)
    return {
        "plant": plant.__dict__,
        "areas": [{"id": a.id, "code": a.code, "name": a.name} for a in areas],
        "lines": [{"id": x.id, "code": x.code, "name": x.name, "area_id": x.area_id} for x in lines],
        "work_centers": [
            {"id": w.id, "code": w.code, "name": w.name, "line_id": w.line_id, "erp_work_center": w.erp_work_center}
            for w in wcs
        ],
        "equipment": [e.__dict__ for e in equipment],
        "shift_patterns": [
            {
                "id": p.id,
                "line_id": p.line_id,
                "code": p.code,
                "name": p.name,
                "start_time": p.start_time,
                "end_time": p.end_time,
                "days_of_week": p.days_of_week,
            }
            for p in shifts
        ],
    }
