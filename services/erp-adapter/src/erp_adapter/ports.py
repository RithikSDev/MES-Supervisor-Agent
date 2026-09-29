"""The ERP port and the canonical model the rest of MES sees.

Nothing outside `erp_adapter.sap` knows SAP field names. Swapping in an ECC/IDoc adapter means another
ErpPort implementation, and core does not change.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel


class CanonicalMaterial(BaseModel):
    number: str
    description: str
    base_uom: str
    material_type: str  # ROH / HALB / FERT
    serial_profile: str | None = None


class CanonicalOperation(BaseModel):
    op_no: str
    description: str
    work_center: str
    control_key: str
    planned_qty: Decimal
    uom: str
    setup_min: Decimal
    run_min_per_unit: Decimal
    labor_min_per_unit: Decimal
    erp_routing_no: str
    erp_routing_counter: str
    erp_confirmed_yield: Decimal = Decimal(0)
    erp_confirmed_scrap: Decimal = Decimal(0)
    erp_final_confirmed: bool = False


class CanonicalComponent(BaseModel):
    material: CanonicalMaterial
    op_no: str | None
    required_qty: Decimal
    uom: str
    reservation_no: str
    reservation_item: str
    storage_location: str | None = None
    backflush: bool = False
    withdrawn_qty: Decimal = Decimal(0)


class CanonicalWorkOrder(BaseModel):
    erp_order_number: str
    erp_plant: str
    order_type: str
    material: CanonicalMaterial
    planned_qty: Decimal
    uom: str
    basic_start: date
    basic_end: date
    erp_status: list[str]  # CRTD REL PCNF CNF PDLV TECO DLFL
    erp_last_changed_at: datetime
    delivered_qty: Decimal = Decimal(0)
    operations: list[CanonicalOperation]
    components: list[CanonicalComponent]


class ConfirmationCommand(BaseModel):
    confirmation_id: UUID
    erp_plant: str
    erp_order_number: str
    op_no: str
    work_center: str | None = None
    yield_qty: Decimal
    scrap_qty: Decimal = Decimal(0)
    rework_qty: Decimal = Decimal(0)
    uom: str
    setup_min: Decimal = Decimal(0)
    run_min: Decimal = Decimal(0)
    labor_min: Decimal = Decimal(0)
    final: bool = False

    @property
    def marker(self) -> str:
        """Written into the SAP confirmation text so a posting can be found again. Exactly 40 chars."""
        return f"MES:{self.confirmation_id}"


class ConfirmationPosted(BaseModel):
    erp_confirmation_group: str
    erp_confirmation_count: str


class ErpErrorKind(StrEnum):
    RETRYABLE = "RETRYABLE"  # definitely not applied; safe to retry
    AMBIGUOUS = "AMBIGUOUS"  # may or may not have been applied (timeout); must reconcile
    PERMANENT = "PERMANENT"  # business rejection; retrying will not help


class ErpError(Exception):
    def __init__(self, kind: ErpErrorKind, message: str, *, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.code = code
        self.http_status = http_status

    def __str__(self) -> str:
        prefix = f"[{self.code}] " if self.code else ""
        return f"{self.kind}: {prefix}{self.message}"


class MaintNotificationCommand(BaseModel):
    notification_id: UUID
    erp_plant: str
    number: str  # MES number range = SAP notification number (external numbering)
    notif_type: str  # M1 request, M2 breakdown
    priority: str
    text: str
    long_text: str = ""
    equipment: str
    breakdown: bool = False
    malfunction_start: datetime
    reported_by: str | None = None


class MaintOrderCommand(BaseModel):
    order_id: UUID
    erp_plant: str
    number: str  # MES number range = SAP order number (external numbering)
    order_type: str  # PM01 corrective, PM02 preventive
    description: str
    equipment: str
    work_center: str | None = None
    notification_number: str | None = None
    priority: str
    planned_start: datetime
    duration_min: int


class MaintOrderStatusCommand(BaseModel):
    message_id: UUID
    order_id: UUID
    erp_plant: str
    number: str
    status: str  # REL | TECO
    labor_min: Decimal | None = None
    findings: str | None = None


class MaintPosted(BaseModel):
    number: str
    system_status: str | None = None


class CanonicalMaintOrderChange(BaseModel):
    number: str
    erp_plant: str
    system_status: str
    changed_at: datetime


class ErpPort(Protocol):
    async def fetch_changed_orders(self, plant: str, since: datetime, *, skip: int, top: int) -> list[CanonicalWorkOrder]:
        """Orders changed at or after `since`, oldest change first."""
        ...

    async def fetch_order(self, order_number: str) -> CanonicalWorkOrder: ...

    async def post_confirmation(self, cmd: ConfirmationCommand) -> ConfirmationPosted: ...

    async def find_confirmation(self, cmd: ConfirmationCommand) -> ConfirmationPosted | None:
        """Look up a previous posting of this exact command (by its marker)."""
        ...

    async def post_goods_movement(self, cmd: GoodsMovementCommand) -> MaterialDocumentPosted: ...

    async def find_goods_movement(self, cmd: GoodsMovementCommand) -> MaterialDocumentPosted | None: ...

    async def post_maint_notification(self, cmd: MaintNotificationCommand) -> MaintPosted: ...

    async def find_maint_notification(self, cmd: MaintNotificationCommand) -> MaintPosted | None: ...

    async def post_maint_order(self, cmd: MaintOrderCommand) -> MaintPosted: ...

    async def find_maint_order(self, cmd: MaintOrderCommand) -> MaintPosted | None: ...

    async def set_maint_order_status(self, cmd: MaintOrderStatusCommand) -> MaintPosted: ...

    async def find_maint_order_status(self, cmd: MaintOrderStatusCommand) -> MaintPosted | None:
        """Reconcile: the order already has (at least) the requested status."""
        ...

    async def fetch_changed_maint_orders(self, plant: str, since: datetime) -> list[CanonicalMaintOrderChange]: ...


class GoodsMovementCommand(BaseModel):
    movement_id: UUID
    erp_plant: str
    # 261 issue to order, 101 GR from order, 311 transfer, 501 receipt w/o order, 321/350/344/343 stock type
    # changes, 555 scrapping from blocked stock, 122 return delivery
    movement_type: str
    material: str
    quantity: Decimal
    uom: str
    storage_location: str | None = None
    destination_storage_location: str | None = None  # 311 only
    batch: str | None = None
    erp_order_number: str | None = None
    reservation_no: str | None = None
    reservation_item: str | None = None
    posting_date: date
    stock_type: str | None = None  # receipts: UNRESTRICTED | QUALITY (into quality inspection stock)
    serial_numbers: list[str] = []
    reason: str | None = None

    @property
    def marker(self) -> str:
        """Written to MaterialDocumentHeaderText so a posting can be found again. (Real S/4 limits the header
        text to 25 characters; a production adapter would use a shorter key or a custom field.)"""
        return f"MES:{self.movement_id}"


class MaterialDocumentPosted(BaseModel):
    erp_material_document: str
    erp_document_year: str
