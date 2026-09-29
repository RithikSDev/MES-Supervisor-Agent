from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from mes_core.modules.execution.models import ProductionConfirmation
from mes_core.modules.execution.service import ConfirmationInput, create_confirmation
from mes_core.shared.errors import NotFound
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, request_id, require

router = APIRouter(tags=["execution"])


class ConfirmationCommand(BaseModel):
    yield_qty: Decimal = Field(ge=0)
    scrap_qty: Decimal = Field(Decimal(0), ge=0)
    rework_qty: Decimal = Field(Decimal(0), ge=0)
    setup_min: Decimal = Field(Decimal(0), ge=0)
    run_min: Decimal = Field(Decimal(0), ge=0)
    labor_min: Decimal = Field(Decimal(0), ge=0)
    final: bool = False
    equipment_id: UUID | None = None
    comment: str | None = None


class ConfirmationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plant_id: UUID
    work_order_id: UUID
    operation_id: UUID
    equipment_id: UUID | None
    yield_qty: Decimal
    scrap_qty: Decimal
    rework_qty: Decimal
    uom: str
    setup_min: Decimal
    run_min: Decimal
    labor_min: Decimal
    final: bool
    comment: str | None
    source: str
    reported_at: datetime
    reported_by: str
    reported_by_name: str | None
    erp_sync_status: str
    erp_confirmation_group: str | None
    erp_confirmation_count: str | None
    erp_error: str | None
    erp_attempts: int | None
    erp_reconciled: bool
    erp_synced_at: datetime | None
    run_id: UUID | None = None
    terminal_id: UUID | None = None
    scrap_reason: str | None = None


@router.post(
    "/work-orders/{wo_id}/operations/{op_id}/confirmations", response_model=ConfirmationOut, status_code=201
)
async def confirm(  # type: ignore[no-untyped-def]
    wo_id: UUID, op_id: UUID, cmd: ConfirmationCommand, request: Request, db: DbDep,
    user=require(Perm.CONFIRMATION_CREATE),
):
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=cmd.comment) as s:
        return await create_confirmation(s, user, wo_id, op_id, ConfirmationInput(**cmd.model_dump()))


@router.get("/work-orders/{wo_id}/confirmations", response_model=list[ConfirmationOut])
async def list_for_work_order(wo_id: UUID, db: DbDep, user=require(Perm.CONFIRMATION_READ)) -> list:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        rows = list(
            (
                await s.scalars(
                    select(ProductionConfirmation)
                    .where(ProductionConfirmation.work_order_id == wo_id)
                    .order_by(ProductionConfirmation.reported_at.desc())
                )
            ).all()
        )
    if rows and not user.can_access_plant(rows[0].plant_id):
        raise NotFound("work order not found")
    return rows


@router.get("/confirmations", response_model=list[ConfirmationOut])
async def list_confirmations(  # type: ignore[no-untyped-def]
    db: DbDep,
    user=require(Perm.CONFIRMATION_READ),
    plant_id: UUID | None = None,
    erp_sync_status: Annotated[list[str] | None, Query()] = None,
    limit: int = Query(100, le=500),
) -> list:
    q = select(ProductionConfirmation).order_by(ProductionConfirmation.reported_at.desc()).limit(limit)
    if plant_id:
        user.ensure_plant(plant_id)
        q = q.where(ProductionConfirmation.plant_id == plant_id)
    elif user.plant_ids is not None:
        q = q.where(ProductionConfirmation.plant_id.in_(user.plant_ids))
    if erp_sync_status:
        q = q.where(ProductionConfirmation.erp_sync_status.in_(erp_sync_status))
    async with db.read() as s:
        return list((await s.scalars(q)).all())
