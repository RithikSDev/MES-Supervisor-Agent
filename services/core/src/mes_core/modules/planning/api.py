from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, select

from mes_core.modules.masterdata.public import get_materials, search_material_ids
from mes_core.modules.org.public import work_centers
from mes_core.modules.planning import schedule_service as sched
from mes_core.modules.planning import service
from mes_core.modules.planning.models import WorkOrder, WorkOrderComponent, WorkOrderStatusHistory
from mes_core.modules.planning.schemas import (
    AddEntryCommand,
    AutoSequenceCommand,
    AutoSequenceResult,
    CreateScheduleCommand,
    EntryOut,
    LockCommand,
    MoveEntryCommand,
    PriorityCommand,
    PublishCommand,
    ReasonCommand,
    ScheduleOut,
    ScheduleView,
    VersionedCommand,
    WorkOrderDetail,
    WorkOrderPage,
    WorkOrderSummary,
)
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, request_id, require

router = APIRouter(tags=["planning"])


def _summary(wo: WorkOrder, materials: dict, counts: dict) -> dict[str, Any]:
    mat = materials.get(wo.material_id)
    total, confirmed = counts.get(wo.id, (0, 0))
    return {
        **{c: getattr(wo, c) for c in WorkOrderSummary.model_fields if hasattr(wo, c)},
        "material_number": mat.material_number if mat else None,
        "material_description": mat.description if mat else None,
        "op_count": total,
        "ops_confirmed": confirmed,
    }


# ------------------------------------------------------------------------------------------------ work orders


@router.get("/work-orders", response_model=WorkOrderPage)
async def list_work_orders(
    db: DbDep,
    user=require(Perm.WORK_ORDER_READ),  # type: ignore[no-untyped-def]
    plant_id: UUID | None = None,
    status: Annotated[list[str] | None, Query()] = None,
    search: str | None = None,
    due_before: date | None = None,
    sort: str = Query("due", pattern="^(due|order|priority|updated)$"),
    offset: int = 0,
    limit: int = Query(50, le=500),
) -> dict:
    f = service.WorkOrderFilter(plant_id=plant_id, status=status, search=search, due_before=due_before)
    async with db.read() as s:
        q = service.scoped_work_orders(user, f)
        if search:
            q = q.where(service.search_clause(search, await search_material_ids(s, search)))
        total = (await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
        order = {
            "due": (WorkOrder.erp_basic_end, WorkOrder.erp_order_number),
            "order": (WorkOrder.erp_order_number.desc(),),
            "priority": (WorkOrder.priority, WorkOrder.erp_basic_end),
            "updated": (WorkOrder.updated_at.desc(),),
        }[sort]
        rows = list((await s.scalars(q.order_by(*order).offset(offset).limit(limit))).all())
        materials = await get_materials(s, {w.material_id for w in rows})
        counts = await service.count_operations(s, [w.id for w in rows])
    return {"items": [_summary(w, materials, counts) for w in rows], "total": total, "offset": offset, "limit": limit}


async def _detail(s, user, wo_id: UUID) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    wo = await service.get_work_order(s, wo_id, user)
    ops = await service.operations_of(s, wo.id)
    comps = list((await s.scalars(select(WorkOrderComponent).where(WorkOrderComponent.work_order_id == wo.id))).all())
    history = list(
        (
            await s.scalars(
                select(WorkOrderStatusHistory)
                .where(WorkOrderStatusHistory.work_order_id == wo.id)
                .order_by(WorkOrderStatusHistory.changed_at)
            )
        ).all()
    )
    materials = await get_materials(s, {wo.material_id} | {c.material_id for c in comps})
    wcs = await work_centers(s, wo.plant_id)
    op_no = {o.id: o.op_no for o in ops}
    counts = {wo.id: (len(ops), sum(1 for o in ops if o.status == "CONFIRMED"))}
    return {
        **_summary(wo, materials, counts),
        "hold_prev_status": wo.hold_prev_status,
        "source": wo.source,
        "erp_last_changed_at": wo.erp_last_changed_at,
        "operations": [
            {
                **{k: getattr(o, k) for k in (
                    "id", "op_no", "sequence", "description", "work_center_id", "control_key", "planned_qty", "uom",
                    "setup_min", "run_min_per_unit", "labor_min_per_unit", "status", "yield_qty", "scrap_qty",
                    "row_version",
                )},
                "work_center_code": wcs[o.work_center_id].code if o.work_center_id in wcs else None,
                "duration_min": sched.operation_duration_min(
                    float(o.setup_min), float(o.run_min_per_unit), float(o.planned_qty), float(o.yield_qty)
                ),
            }
            for o in ops
        ],
        "components": [
            {
                "id": c.id,
                "material_id": c.material_id,
                "material_number": materials[c.material_id].material_number if c.material_id in materials else None,
                "material_description": materials[c.material_id].description if c.material_id in materials else None,
                "operation_id": c.operation_id,
                "op_no": op_no.get(c.operation_id) if c.operation_id else None,
                "required_qty": c.required_qty,
                "uom": c.uom,
                "storage_location": c.storage_location,
                "backflush": c.backflush,
            }
            for c in comps
        ],
        "history": history,
    }


@router.get("/work-orders/{wo_id}", response_model=WorkOrderDetail)
async def get_work_order(wo_id: UUID, db: DbDep, user=require(Perm.WORK_ORDER_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        return await _detail(s, user, wo_id)


@router.post("/work-orders/{wo_id}/dispatch", response_model=WorkOrderDetail)
async def dispatch(wo_id: UUID, cmd: VersionedCommand, request: Request, db: DbDep, user=require(Perm.WORK_ORDER_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await service.dispatch(s, user, wo_id, cmd.row_version)
    async with db.read() as s:
        return await _detail(s, user, wo_id)


@router.post("/work-orders/{wo_id}/hold", response_model=WorkOrderDetail)
async def hold(wo_id: UUID, cmd: ReasonCommand, request: Request, db: DbDep, user=require(Perm.WORK_ORDER_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await service.hold(s, user, wo_id, cmd.row_version, cmd.reason)
    async with db.read() as s:
        return await _detail(s, user, wo_id)


@router.post("/work-orders/{wo_id}/resume", response_model=WorkOrderDetail)
async def resume(wo_id: UUID, cmd: ReasonCommand, request: Request, db: DbDep, user=require(Perm.WORK_ORDER_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await service.resume(s, user, wo_id, cmd.row_version, cmd.reason or None)
    async with db.read() as s:
        return await _detail(s, user, wo_id)


@router.put("/work-orders/{wo_id}/priority", response_model=WorkOrderDetail)
async def priority(wo_id: UUID, cmd: PriorityCommand, request: Request, db: DbDep, user=require(Perm.WORK_ORDER_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await service.set_priority(s, user, wo_id, cmd.row_version, cmd.priority)
    async with db.read() as s:
        return await _detail(s, user, wo_id)


# ------------------------------------------------------------------------------------------------ schedules


@router.get("/schedules", response_model=list[ScheduleOut])
async def list_schedules(
    plant_id: UUID,
    db: DbDep,
    user=require(Perm.SCHEDULE_READ),  # type: ignore[no-untyped-def]
    status: Annotated[list[str] | None, Query()] = None,
) -> list:
    async with db.read() as s:
        return await sched.list_schedules(s, user, plant_id, status)


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
async def create_schedule(cmd: CreateScheduleCommand, request: Request, db: DbDep, user=require(Perm.SCHEDULE_EDIT)):  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        return await sched.create_draft(s, user, cmd.plant_id, cmd.horizon_days)


@router.get("/schedules/{schedule_id}", response_model=ScheduleView)
async def get_schedule(schedule_id: UUID, db: DbDep, user=require(Perm.SCHEDULE_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        return await sched.schedule_view(s, user, schedule_id)


@router.post("/schedules/{schedule_id}/entries", response_model=EntryOut, status_code=201)
async def add_entry(schedule_id: UUID, cmd: AddEntryCommand, request: Request, db: DbDep, user=require(Perm.SCHEDULE_EDIT)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        entry = await sched.add_entry(s, user, schedule_id, cmd.operation_id, cmd.equipment_id, cmd.planned_start)
    return await _entry_out(db, user, schedule_id, entry.id)


@router.put("/schedules/{schedule_id}/entries/{entry_id}", response_model=EntryOut)
async def move_entry(schedule_id: UUID, entry_id: UUID, cmd: MoveEntryCommand, request: Request, db: DbDep, user=require(Perm.SCHEDULE_EDIT)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await sched.move_entry(
            s, user, schedule_id, entry_id,
            equipment_id=cmd.equipment_id, start=cmd.planned_start, locked=cmd.locked, row_version=cmd.row_version,
        )
    return await _entry_out(db, user, schedule_id, entry_id)


@router.put("/schedules/{schedule_id}/entries/{entry_id}/lock", response_model=EntryOut)
async def lock_entry(schedule_id: UUID, entry_id: UUID, cmd: LockCommand, request: Request, db: DbDep, user=require(Perm.SCHEDULE_EDIT)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await sched.set_entry_lock(s, user, schedule_id, entry_id, cmd.locked)
    return await _entry_out(db, user, schedule_id, entry_id)


@router.delete("/schedules/{schedule_id}/entries/{entry_id}", status_code=204)
async def remove_entry(schedule_id: UUID, entry_id: UUID, request: Request, db: DbDep, user=require(Perm.SCHEDULE_EDIT)) -> Response:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await sched.remove_entry(s, user, schedule_id, entry_id)
    return Response(status_code=204)


@router.post("/schedules/{schedule_id}/auto-sequence", response_model=AutoSequenceResult)
async def auto_sequence(schedule_id: UUID, cmd: AutoSequenceCommand, request: Request, db: DbDep, user=require(Perm.SCHEDULE_EDIT)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        return await sched.run_auto_sequence(s, user, schedule_id, reset_unlocked=cmd.reset_unlocked)


@router.post("/schedules/{schedule_id}/publish", response_model=ScheduleOut)
async def publish(schedule_id: UUID, cmd: PublishCommand, request: Request, db: DbDep, user=require(Perm.SCHEDULE_PUBLISH)):  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=cmd.reason) as s:
        return await sched.publish(s, user, schedule_id, reason=cmd.reason, row_version=cmd.row_version)


@router.post("/schedules/{schedule_id}/discard", response_model=ScheduleOut)
async def discard(schedule_id: UUID, request: Request, db: DbDep, user=require(Perm.SCHEDULE_EDIT)):  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        return await sched.discard(s, user, schedule_id)


async def _entry_out(db, user, schedule_id: UUID, entry_id: UUID) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        view = await sched.schedule_view(s, user, schedule_id)
    for e in view["entries"]:
        if e["id"] == entry_id:
            return e
    raise RuntimeError("entry vanished after write")
