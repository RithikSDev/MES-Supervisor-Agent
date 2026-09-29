from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkOrderSummary(BaseModel):
    id: UUID
    plant_id: UUID
    line_id: UUID | None
    erp_order_number: str
    order_type: str
    material_id: UUID
    material_number: str | None
    material_description: str | None
    planned_qty: Decimal
    uom: str
    yield_qty: Decimal
    scrap_qty: Decimal
    erp_basic_start: date
    erp_basic_end: date
    sched_start: datetime | None
    sched_end: datetime | None
    status: str
    erp_status: str
    priority: int
    row_version: int
    op_count: int
    ops_confirmed: int
    updated_at: datetime


class WorkOrderPage(BaseModel):
    items: list[WorkOrderSummary]
    total: int
    offset: int
    limit: int


class OperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    op_no: str
    sequence: int
    description: str
    work_center_id: UUID
    work_center_code: str | None = None
    control_key: str | None
    planned_qty: Decimal
    uom: str
    setup_min: Decimal
    run_min_per_unit: Decimal
    labor_min_per_unit: Decimal
    status: str
    yield_qty: Decimal
    scrap_qty: Decimal
    duration_min: int = 0
    row_version: int


class ComponentOut(BaseModel):
    id: UUID
    material_id: UUID
    material_number: str | None
    material_description: str | None
    operation_id: UUID | None
    op_no: str | None
    required_qty: Decimal
    uom: str
    storage_location: str | None
    backflush: bool


class StatusHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    from_status: str | None
    to_status: str
    reason: str | None
    changed_at: datetime
    changed_by: str
    changed_by_name: str | None


class WorkOrderDetail(WorkOrderSummary):
    hold_prev_status: str | None
    source: str
    erp_last_changed_at: datetime | None
    operations: list[OperationOut]
    components: list[ComponentOut]
    history: list[StatusHistoryOut]


class VersionedCommand(BaseModel):
    row_version: int | None = Field(None, description="Optimistic lock: the row_version the client last saw")


class ReasonCommand(VersionedCommand):
    reason: str = ""


class PriorityCommand(VersionedCommand):
    priority: int


# ------------------------------------------------------------------------------------------------ schedules


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plant_id: UUID
    line_id: UUID | None
    name: str
    version: int
    horizon_start: datetime
    horizon_end: datetime
    status: str
    based_on_id: UUID | None
    published_at: datetime | None
    published_by: str | None
    signature_id: UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime


class ResourceOut(BaseModel):
    id: UUID
    code: str
    name: str
    equipment_type: str
    status: str
    work_center_id: UUID
    work_center_code: str
    line_id: UUID
    line_code: str


class OpInfo(BaseModel):
    operation_id: UUID
    work_order_id: UUID
    erp_order_number: str
    material_number: str | None
    material_description: str | None
    op_no: str
    op_description: str
    work_center_id: UUID
    duration_min: int
    planned_qty: Decimal
    uom: str
    due: datetime
    priority: int
    wo_status: str
    op_status: str


class EntryOut(OpInfo):
    id: UUID
    equipment_id: UUID
    planned_start: datetime
    planned_end: datetime
    locked: bool
    row_version: int


class ConflictOut(BaseModel):
    kind: str
    severity: str
    message: str
    work_order_id: UUID
    operation_id: UUID | None
    entry_id: UUID | None


class OffShiftOut(BaseModel):
    line_id: UUID
    start: datetime
    end: datetime


class BlockedOut(BaseModel):
    """A machine not available for production: maintenance window or down (until the expected repair time)."""

    equipment_id: UUID
    start: datetime
    end: datetime


class ScheduleView(BaseModel):
    schedule: ScheduleOut
    plant_timezone: str
    resources: list[ResourceOut]
    entries: list[EntryOut]
    unscheduled: list[OpInfo]
    conflicts: list[ConflictOut]
    off_shift: list[OffShiftOut]
    blocked: list[BlockedOut] = []


class CreateScheduleCommand(BaseModel):
    plant_id: UUID
    horizon_days: int = Field(14, ge=1, le=60)


class AddEntryCommand(BaseModel):
    operation_id: UUID
    equipment_id: UUID | None = None
    planned_start: datetime | None = None  # omitted = earliest free slot


class MoveEntryCommand(VersionedCommand):
    equipment_id: UUID
    planned_start: datetime
    locked: bool | None = None


class LockCommand(BaseModel):
    locked: bool


class AutoSequenceCommand(BaseModel):
    reset_unlocked: bool = False


class UnplacedOut(BaseModel):
    operation_id: UUID
    work_order_id: UUID
    erp_order_number: str
    op_no: str
    reason: str


class AutoSequenceResult(BaseModel):
    placed: int
    unplaced: list[UnplacedOut]


class PublishCommand(VersionedCommand):
    reason: str = Field(..., min_length=1, description="E-signature reason / meaning of approval")
