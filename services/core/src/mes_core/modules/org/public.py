"""Public interface of the org module. Other modules import only this file."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.org.models import Equipment, Line, Plant, ShiftPattern, Terminal, WorkCenter
from mes_core.shared.calendar import ShiftDef, WorkingCalendar, expand_shifts
from mes_core.shared.errors import NotFound


@dataclass(frozen=True)
class PlantRef:
    id: UUID
    code: str
    name: str
    timezone: str
    erp_plant_code: str | None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class WorkCenterRef:
    id: UUID
    code: str
    name: str
    line_id: UUID
    erp_work_center: str | None


@dataclass(frozen=True)
class EquipmentRef:
    id: UUID
    code: str
    name: str
    equipment_type: str
    status: str
    work_center_id: UUID
    work_center_code: str
    line_id: UUID
    line_code: str
    plant_id: UUID | None = None


def _plant(p: Plant) -> PlantRef:
    return PlantRef(p.id, p.code, p.name, p.timezone, p.erp_plant_code)


async def get_plant(s: AsyncSession, plant_id: UUID) -> PlantRef:
    p = await s.get(Plant, plant_id)
    if p is None:
        raise NotFound(f"plant {plant_id} not found")
    return _plant(p)


async def list_plants(s: AsyncSession, only: frozenset[UUID] | None = None) -> list[PlantRef]:
    q = select(Plant).order_by(Plant.code)
    if only is not None:
        q = q.where(Plant.id.in_(only))
    return [_plant(p) for p in (await s.scalars(q)).all()]


async def find_plant_by_erp_code(s: AsyncSession, erp_plant_code: str) -> PlantRef | None:
    p = (await s.scalars(select(Plant).where(Plant.erp_plant_code == erp_plant_code))).first()
    return _plant(p) if p else None


async def work_centers(s: AsyncSession, plant_id: UUID) -> dict[UUID, WorkCenterRef]:
    rows = (await s.scalars(select(WorkCenter).where(WorkCenter.plant_id == plant_id))).all()
    return {w.id: WorkCenterRef(w.id, w.code, w.name, w.line_id, w.erp_work_center) for w in rows}


async def find_work_center_by_erp(s: AsyncSession, plant_id: UUID, erp_work_center: str) -> WorkCenterRef | None:
    w = (
        await s.scalars(
            select(WorkCenter).where(WorkCenter.plant_id == plant_id, WorkCenter.erp_work_center == erp_work_center)
        )
    ).first()
    return WorkCenterRef(w.id, w.code, w.name, w.line_id, w.erp_work_center) if w else None


EQUIPMENT_STATUSES = ("AVAILABLE", "DOWN", "MAINTENANCE", "DECOMMISSIONED")


async def get_equipment(s: AsyncSession, equipment_id: UUID) -> EquipmentRef:
    row = (
        await s.execute(
            select(Equipment, WorkCenter.code, Line.code)
            .join(WorkCenter, WorkCenter.id == Equipment.work_center_id)
            .join(Line, Line.id == Equipment.line_id)
            .where(Equipment.id == equipment_id)
        )
    ).first()
    if row is None:
        raise NotFound("machine not found")
    e, wc_code, line_code = row
    return EquipmentRef(e.id, e.code, e.name, e.equipment_type, e.status, e.work_center_id, wc_code, e.line_id, line_code, e.plant_id)


async def set_equipment_status(s: AsyncSession, equipment_id: UUID, status: str) -> None:
    """Availability of a machine (maintenance owns the transitions; execution refuses runs unless AVAILABLE)."""
    if status not in EQUIPMENT_STATUSES:
        raise ValueError(f"unknown equipment status {status}")
    eq = (await s.scalars(select(Equipment).where(Equipment.id == equipment_id).with_for_update())).first()
    if eq is None:
        raise NotFound("machine not found")
    if eq.status != status:
        eq.status = status
        await s.flush()


async def list_equipment(s: AsyncSession, plant_id: UUID) -> list[EquipmentRef]:
    rows = (
        await s.execute(
            select(Equipment, WorkCenter.code, Line.code)
            .join(WorkCenter, WorkCenter.id == Equipment.work_center_id)
            .join(Line, Line.id == Equipment.line_id)
            .where(Equipment.plant_id == plant_id, Equipment.status != "DECOMMISSIONED")
            .order_by(Line.code, WorkCenter.code, Equipment.code)
        )
    ).all()
    return [
        EquipmentRef(e.id, e.code, e.name, e.equipment_type, e.status, e.work_center_id, wc_code, e.line_id, line_code, e.plant_id)
        for e, wc_code, line_code in rows
    ]


async def line_calendars(
    s: AsyncSession, plant: PlantRef, start: datetime, end: datetime
) -> dict[UUID, WorkingCalendar]:
    """Working calendar per line: plant-wide shift patterns plus the line's own."""
    lines = (await s.scalars(select(Line.id).where(Line.plant_id == plant.id))).all()
    patterns = (await s.scalars(select(ShiftPattern).where(ShiftPattern.plant_id == plant.id))).all()
    out = {}
    for line_id in lines:
        defs = [
            ShiftDef(p.start_time, p.end_time, tuple(p.days_of_week))
            for p in patterns
            if p.line_id is None or p.line_id == line_id
        ]
        out[line_id] = WorkingCalendar(expand_shifts(defs, plant.tz, start, end))
    return out


# ------------------------------------------------------------------------------------------------ lines, terminals


@dataclass(frozen=True)
class LineRef:
    id: UUID
    plant_id: UUID
    code: str
    name: str


async def get_line(s: AsyncSession, line_id: UUID) -> LineRef:
    ln = await s.get(Line, line_id)
    if ln is None:
        raise NotFound(f"line {line_id} not found")
    return LineRef(ln.id, ln.plant_id, ln.code, ln.name)


async def list_lines(s: AsyncSession, plant_id: UUID) -> list[LineRef]:
    rows = (await s.scalars(select(Line).where(Line.plant_id == plant_id).order_by(Line.code))).all()
    return [LineRef(x.id, x.plant_id, x.code, x.name) for x in rows]


@dataclass(frozen=True)
class TerminalRef:
    id: UUID
    plant_id: UUID
    line_id: UUID
    line_code: str
    code: str
    name: str
    active: bool
    pin_locked_until: datetime | None


def _terminal(t: Terminal, line_code: str) -> TerminalRef:
    return TerminalRef(t.id, t.plant_id, t.line_id, line_code, t.code, t.name, t.active, t.pin_locked_until)


async def get_terminal(s: AsyncSession, terminal_id: UUID) -> TerminalRef:
    row = (
        await s.execute(select(Terminal, Line.code).join(Line, Line.id == Terminal.line_id).where(Terminal.id == terminal_id))
    ).first()
    if row is None:
        raise NotFound("terminal not found")
    return _terminal(row[0], row[1])


async def list_terminals(s: AsyncSession, plant_id: UUID) -> list[TerminalRef]:
    rows = (
        await s.execute(
            select(Terminal, Line.code)
            .join(Line, Line.id == Terminal.line_id)
            .where(Terminal.plant_id == plant_id)
            .order_by(Terminal.code)
        )
    ).all()
    return [_terminal(t, lc) for t, lc in rows]


async def register_pin_failure(s: AsyncSession, terminal_id: UUID, *, max_failures: int, lockout_s: int) -> datetime | None:
    """Count a wrong PIN on this terminal; after `max_failures` the terminal refuses PINs for `lockout_s`."""
    t = (await s.scalars(select(Terminal).where(Terminal.id == terminal_id).with_for_update())).one()
    t.pin_failures += 1
    if t.pin_failures >= max_failures:
        t.pin_locked_until = datetime.now(UTC) + timedelta(seconds=lockout_s)
        t.pin_failures = 0
    await s.flush()
    return t.pin_locked_until


async def clear_pin_failures(s: AsyncSession, terminal_id: UUID) -> None:
    t = (await s.scalars(select(Terminal).where(Terminal.id == terminal_id).with_for_update())).one()
    if t.pin_failures or t.pin_locked_until:
        t.pin_failures = 0
        t.pin_locked_until = None
        await s.flush()


async def create_terminal(s: AsyncSession, *, plant_id: UUID, line_id: UUID, code: str, name: str) -> TerminalRef:
    line = await get_line(s, line_id)
    if line.plant_id != plant_id:
        raise NotFound("line not found in this plant")
    t = Terminal(plant_id=plant_id, line_id=line_id, code=code, name=name)
    s.add(t)
    await s.flush()
    return _terminal(t, line.code)
