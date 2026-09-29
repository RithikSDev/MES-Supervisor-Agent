"""Shop-floor execution API (used by the operator terminal) and live machine state (supervisor dashboards)."""

from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from mes_core.modules.execution import run_service as rs
from mes_core.modules.execution.domain.runs import PAUSE_REASONS, SCRAP_REASONS
from mes_core.modules.execution.live import LiveBus
from mes_core.modules.execution.models import OperationEvent, OperationRun
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.org.public import get_line, get_terminal, list_equipment, work_centers
from mes_core.modules.planning.public import get_work_order_brief, operation_queue, operations_brief
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, request_id, require

router = APIRouter(tags=["execution"])


def _live(request: Request) -> LiveBus:
    return request.app.state.live


# ------------------------------------------------------------------------------------------------ schemas


class ReasonCode(BaseModel):
    code: str
    label: str


class ReasonCodes(BaseModel):
    pause: list[ReasonCode]
    scrap: list[ReasonCode]


class RunInspection(BaseModel):
    id: UUID
    number: str
    status: str
    blocking: bool


class RunOut(BaseModel):
    id: UUID
    plant_id: UUID
    work_order_id: UUID
    erp_order_number: str
    material_number: str | None
    material_description: str | None
    operation_id: UUID
    op_no: str
    op_description: str
    op_status: str
    op_planned_qty: Decimal
    op_yield_qty: Decimal
    op_scrap_qty: Decimal
    uom: str
    is_last_operation: bool
    equipment_id: UUID
    equipment_code: str | None
    line_id: UUID | None
    terminal_id: UUID | None
    operator_id: UUID
    operator_name: str
    status: str
    paused_from: str | None
    pause_reason: str | None
    started_at: datetime
    phase_started_at: datetime
    ended_at: datetime | None
    setup_s: int
    run_s: int
    paused_s: int
    yield_qty: Decimal
    scrap_qty: Decimal
    wo_status: str
    inspection: RunInspection | None = None
    serials: list[str] = []
    replayed: bool = False


class ActionMeta(BaseModel):
    client_event_id: UUID | None = Field(None, description="Terminal-generated id; replays are ignored")
    occurred_at: datetime | None = Field(None, description="Terminal time of the action (offline replays)")


class StartIn(ActionMeta):
    equipment_id: UUID
    phase: Literal["SETUP", "RUN"] = "SETUP"
    run_id: UUID | None = Field(None, description="Optional terminal-generated run id")


class PauseIn(ActionMeta):
    reason_code: str
    note: str | None = None


class ReportIn(ActionMeta):
    yield_qty: Decimal = Field(ge=0)
    scrap_qty: Decimal = Field(Decimal(0), ge=0)
    scrap_reason: str | None = None
    final: bool = False
    note: str | None = None


class StopIn(ActionMeta):
    note: str | None = None


class MachineOut(BaseModel):
    id: UUID
    code: str
    name: str
    work_center_id: UUID
    work_center_code: str
    status: str
    busy_run_id: UUID | None


class QueueItemOut(BaseModel):
    work_order_id: UUID
    erp_order_number: str
    material_number: str | None
    material_description: str | None
    priority: int
    due: str
    wo_status: str
    operation_id: UUID
    op_no: str
    op_description: str
    op_status: str
    work_center_id: UUID
    planned_qty: Decimal
    yield_qty: Decimal
    scrap_qty: Decimal
    uom: str
    setup_min: Decimal
    run_min_per_unit: Decimal
    is_last_operation: bool
    previous_op_status: str | None
    planned_equipment_id: UUID | None
    planned_start: datetime | None
    planned_end: datetime | None
    active_run: RunOut | None


class QueueOut(BaseModel):
    line_id: UUID
    line_code: str
    machines: list[MachineOut]
    items: list[QueueItemOut]


# ------------------------------------------------------------------------------------------------ reference data


@router.get("/execution/reason-codes", response_model=ReasonCodes)
async def reason_codes(_=require(Perm.EXECUTION_READ)) -> dict:  # type: ignore[no-untyped-def]
    return {
        "pause": [{"code": k, "label": v} for k, v in PAUSE_REASONS.items()],
        "scrap": [{"code": k, "label": v} for k, v in SCRAP_REASONS.items()],
    }


# ------------------------------------------------------------------------------------------------ terminal queue


@router.get("/execution/queue", response_model=QueueOut)
async def queue(db: DbDep, user=require(Perm.EXECUTION_READ), line_id: UUID | None = None) -> dict:  # type: ignore[no-untyped-def]
    """Work for one line: its machines and the open operations of dispatched orders, in planned order.

    On a terminal the line is the terminal's; elsewhere pass `line_id`.
    """
    async with db.read() as s:
        if user.terminal_id:
            terminal = await get_terminal(s, user.terminal_id)
            line_id, line_code, plant_id = terminal.line_id, terminal.line_code, terminal.plant_id
        elif line_id is None:
            raise HTTPException(422, "line_id is required outside a terminal session")
        else:
            line = await get_line(s, line_id)
            line_code, plant_id = line.code, line.plant_id
        user.ensure_plant(plant_id)
        machines_all = [m for m in await list_equipment(s, plant_id) if m.line_id == line_id]
        wc_ids = {m.work_center_id for m in machines_all}
        items = await operation_queue(s, plant_id, wc_ids)
        active = {
            r.operation_id: r
            for r in (
                await s.scalars(select(OperationRun).where(OperationRun.plant_id == plant_id, OperationRun.status != "ENDED"))
            ).all()
        }
        busy = {r.equipment_id: r.id for r in active.values()}
        mats = await get_materials(s, {q.work_order.material_id for q in items})
        out_items = []
        for q in items:
            mat = mats.get(q.work_order.material_id)
            run = active.get(q.operation.id)
            out_items.append(
                {
                    "work_order_id": q.work_order.id,
                    "erp_order_number": q.work_order.erp_order_number,
                    "material_number": mat.material_number if mat else None,
                    "material_description": mat.description if mat else None,
                    "priority": q.work_order.priority,
                    "due": q.work_order.erp_basic_end.isoformat(),
                    "wo_status": q.work_order.status,
                    "operation_id": q.operation.id,
                    "op_no": q.operation.op_no,
                    "op_description": q.operation.description,
                    "op_status": q.operation.status,
                    "work_center_id": q.operation.work_center_id,
                    "planned_qty": q.operation.planned_qty,
                    "yield_qty": q.operation.yield_qty,
                    "scrap_qty": q.operation.scrap_qty,
                    "uom": q.operation.uom,
                    "setup_min": q.operation.setup_min,
                    "run_min_per_unit": q.operation.run_min_per_unit,
                    "is_last_operation": q.operation.is_last,
                    "previous_op_status": q.previous_op_status,
                    "planned_equipment_id": q.planned_equipment_id,
                    "planned_start": q.planned_start,
                    "planned_end": q.planned_end,
                    "active_run": await rs.run_view(s, run) if run else None,
                }
            )
    return {
        "line_id": line_id,
        "line_code": line_code,
        "machines": [
            {"id": m.id, "code": m.code, "name": m.name, "work_center_id": m.work_center_id,
             "work_center_code": m.work_center_code, "status": m.status, "busy_run_id": busy.get(m.id)}
            for m in machines_all
        ],
        "items": out_items,
    }


# ------------------------------------------------------------------------------------------------ run actions


async def _finish(request: Request, db, run: OperationRun, replayed: bool) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """After commit: build the response and push the machine's new state to the live bus."""
    async with db.read() as s:
        fresh = await rs.get_run(s, run.id)
        view = await rs.run_view(s, fresh, replayed=replayed)
        state = await rs.state_for_run(s, fresh)
    await _live(request).push(fresh.plant_id, state)
    return view


@router.post("/execution/work-orders/{wo_id}/operations/{op_id}/runs", response_model=RunOut, status_code=201)
async def start(wo_id: UUID, op_id: UUID, body: StartIn, request: Request, db: DbDep, user=require(Perm.EXECUTION_OPERATE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        run, replayed = await rs.start_run(
            s, user, wo_id=wo_id, op_id=op_id, equipment_id=body.equipment_id, phase=body.phase, run_id=body.run_id,
            meta=rs.Meta(body.client_event_id, body.occurred_at),
        )
    return await _finish(request, db, run, replayed)


@router.post("/execution/runs/{run_id}/start-production", response_model=RunOut)
async def start_production(run_id: UUID, body: ActionMeta, request: Request, db: DbDep, user=require(Perm.EXECUTION_OPERATE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        run, replayed = await rs.start_production(s, user, run_id, rs.Meta(body.client_event_id, body.occurred_at))
    return await _finish(request, db, run, replayed)


@router.post("/execution/runs/{run_id}/pause", response_model=RunOut)
async def pause(run_id: UUID, body: PauseIn, request: Request, db: DbDep, user=require(Perm.EXECUTION_OPERATE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=PAUSE_REASONS.get(body.reason_code)) as s:
        run, replayed = await rs.pause(s, user, run_id, body.reason_code, body.note, rs.Meta(body.client_event_id, body.occurred_at))
    return await _finish(request, db, run, replayed)


@router.post("/execution/runs/{run_id}/resume", response_model=RunOut)
async def resume(run_id: UUID, body: ActionMeta, request: Request, db: DbDep, user=require(Perm.EXECUTION_OPERATE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        run, replayed = await rs.resume(s, user, run_id, rs.Meta(body.client_event_id, body.occurred_at))
    return await _finish(request, db, run, replayed)


@router.post("/execution/runs/{run_id}/report", response_model=RunOut)
async def report(run_id: UUID, body: ReportIn, request: Request, db: DbDep, user=require(Perm.EXECUTION_OPERATE)) -> dict:  # type: ignore[no-untyped-def]
    if body.yield_qty > 0:
        async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
            await rs.prepare_report(s, run_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.note) as s:
        run, replayed = await rs.report(
            s, user, run_id, yield_qty=body.yield_qty, scrap_qty=body.scrap_qty, scrap_reason=body.scrap_reason,
            final=body.final, note=body.note, meta=rs.Meta(body.client_event_id, body.occurred_at),
        )
    return await _finish(request, db, run, replayed)


@router.post("/execution/runs/{run_id}/stop", response_model=RunOut)
async def stop(run_id: UUID, body: StopIn, request: Request, db: DbDep, user=require(Perm.EXECUTION_OPERATE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.note) as s:
        run, replayed = await rs.stop(s, user, run_id, body.note, rs.Meta(body.client_event_id, body.occurred_at))
    return await _finish(request, db, run, replayed)


@router.get("/execution/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: UUID, db: DbDep, user=require(Perm.EXECUTION_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        run = await rs.get_run(s, run_id)
        user.ensure_plant(run.plant_id)
        return await rs.run_view(s, run)


# ------------------------------------------------------------------------------------------------ history


class EventOut(BaseModel):
    id: UUID
    run_id: UUID
    operation_id: UUID
    equipment_id: UUID
    equipment_code: str | None
    operator_name: str
    event_type: str
    reason_code: str | None
    note: str | None
    details: dict[str, Any] | None
    occurred_at: datetime
    recorded_at: datetime
    terminal_id: UUID | None


class WorkOrderExecution(BaseModel):
    runs: list[RunOut]
    events: list[EventOut]


@router.get("/work-orders/{wo_id}/execution", response_model=WorkOrderExecution)
async def work_order_execution(wo_id: UUID, db: DbDep, user=require(Perm.EXECUTION_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        wo = await get_work_order_brief(s, wo_id)
        user.ensure_plant(wo.plant_id)
        runs = (await s.scalars(select(OperationRun).where(OperationRun.work_order_id == wo_id).order_by(OperationRun.started_at))).all()
        events = (
            await s.scalars(select(OperationEvent).where(OperationEvent.work_order_id == wo_id).order_by(OperationEvent.occurred_at.desc()))
        ).all()
        codes = {e.id: e.code for e in await list_equipment(s, wo.plant_id)}
        return {
            "runs": [await rs.run_view(s, r) for r in runs],
            "events": [
                {**{c.key: getattr(e, c.key) for c in OperationEvent.__table__.columns}, "equipment_code": codes.get(e.equipment_id)}
                for e in events
            ],
        }


# ------------------------------------------------------------------------------------------------ live board + WIP


class MachineState(BaseModel):
    equipment_id: str
    equipment_code: str
    equipment_name: str
    work_center_code: str
    line_id: str
    line_code: str
    equipment_status: str
    state: str  # IDLE | SETUP | RUNNING | PAUSED
    updated_at: str
    run_id: str | None = None
    work_order_id: str | None = None
    erp_order_number: str | None = None
    op_no: str | None = None
    op_description: str | None = None
    material_number: str | None = None
    operator_name: str | None = None
    since: str | None = None
    started_at: str | None = None
    pause_reason: str | None = None
    op_planned_qty: str | None = None
    op_yield_qty: str | None = None
    op_scrap_qty: str | None = None
    uom: str | None = None


class LiveOut(BaseModel):
    source: str  # "redis" | "database"
    machines: list[MachineState]


@router.get("/execution/live", response_model=LiveOut)
async def live(plant_id: UUID, request: Request, db: DbDep, user=require(Perm.EXECUTION_READ)) -> dict:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    cached = await _live(request).snapshot(plant_id)
    if cached:
        return {"source": "redis", "machines": sorted(cached, key=lambda m: (m["line_code"], m["work_center_code"], m["equipment_code"]))}
    async with db.read() as s:
        states = await rs.equipment_states(s, plant_id)
    if _live(request).enabled:
        await _live(request).rebuild(plant_id, states)
    return {"source": "database", "machines": states}


@router.get("/execution/live/stream")
async def live_stream(plant_id: UUID, request: Request, user=require(Perm.EXECUTION_READ)) -> StreamingResponse:  # type: ignore[no-untyped-def]
    """Server-sent events: one `machine` event per state change, comments as heartbeats."""
    user.ensure_plant(plant_id)
    bus = _live(request)

    async def events() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        if not bus.enabled:
            yield "event: unavailable\ndata: {}\n\n"
            return
        async for data in bus.stream(plant_id):
            if await request.is_disconnected():
                break
            yield f"event: machine\ndata: {data}\n\n" if data else ": ping\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


class WorkCenterWip(BaseModel):
    work_center_id: UUID
    work_center_code: str
    line_id: UUID
    waiting_qty: Decimal  # good parts from previous operations waiting at this work center
    waiting_orders: list[str]
    running: int  # active runs


@router.get("/execution/wip", response_model=list[WorkCenterWip])
async def wip(plant_id: UUID, db: DbDep, user=require(Perm.EXECUTION_READ)) -> list:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        wcs = await work_centers(s, plant_id)
        wc_ids = set(wcs)
        items = await operation_queue(s, plant_id, wc_ids)
        waiting: dict[UUID, Decimal] = defaultdict(Decimal)
        orders: dict[UUID, set[str]] = defaultdict(set)
        seen: set[UUID] = set()
        for q in items:
            if q.work_order.id in seen:
                continue
            seen.add(q.work_order.id)
            prev = None
            for op in await operations_brief(s, q.work_order.id):
                if prev is not None:
                    w = prev.yield_qty - op.yield_qty - op.scrap_qty
                    if w > 0 and op.status != "CONFIRMED":
                        waiting[op.work_center_id] += w
                        orders[op.work_center_id].add(q.work_order.erp_order_number)
                prev = op
        running: dict[UUID, int] = defaultdict(int)
        eq_wc = {e.id: e.work_center_id for e in await list_equipment(s, plant_id)}
        for r in (await s.scalars(select(OperationRun).where(OperationRun.plant_id == plant_id, OperationRun.status != "ENDED"))).all():
            running[eq_wc.get(r.equipment_id)] += 1  # type: ignore[index]
    return [
        {"work_center_id": wid, "work_center_code": wc.code, "line_id": wc.line_id, "waiting_qty": waiting.get(wid, Decimal(0)),
         "waiting_orders": sorted(orders.get(wid, set())), "running": running.get(wid, 0)}
        for wid, wc in sorted(wcs.items(), key=lambda kv: kv[1].code)
    ]
