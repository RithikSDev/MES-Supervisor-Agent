"""ErpPort implementation for S/4HANA On-Prem standard OData APIs (PP discrete)."""

import re
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from erp_adapter.ports import (
    CanonicalComponent,
    CanonicalMaintOrderChange,
    CanonicalMaterial,
    CanonicalOperation,
    CanonicalWorkOrder,
    ConfirmationCommand,
    ConfirmationPosted,
    ErpError,
    ErpErrorKind,
    GoodsMovementCommand,
    MaintNotificationCommand,
    MaintOrderCommand,
    MaintOrderStatusCommand,
    MaintPosted,
    MaterialDocumentPosted,
)
from erp_adapter.sap.client import SapODataClient

PO_SRV = "API_PRODUCTION_ORDER_2_SRV"
PRODUCT_SRV = "API_PRODUCT_SRV"
CONF_SRV = "API_PROD_ORDER_CONFIRMATION_2_SRV"
MATDOC_SRV = "API_MATERIAL_DOCUMENT_SRV"
NOTIF_SRV = "API_MAINTNOTIFICATION"
MAINTORDER_SRV = "API_MAINTENANCEORDER"
PM_STATUS_ORDER = ("CRTD", "REL", "TECO")
# GoodsMovementCode: 02 GR for order, 03 goods issue, 04 transfer posting, 05 other receipts
# GoodsMovementCode: 01 GR for PO, 02 GR for order, 03 goods issue, 04 transfer posting, 05 other receipts
GOODS_MOVEMENT_CODES = {
    "101": "02", "102": "02", "261": "03", "262": "03", "311": "04", "501": "05",
    "321": "04", "350": "04", "344": "04", "343": "04", "555": "03", "122": "01",
}
# InventoryStockType: 01 unrestricted, 02 quality inspection, 07 blocked
STOCK_TYPES = {"UNRESTRICTED": "01", "QUALITY": "02", "BLOCKED": "07"}
# movement types that take stock out of blocked stock
FROM_BLOCKED = ("555", "122")

_DATE_RE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")
_MATERIAL_TTL_S = 600


def parse_sap_datetime(v: str | None) -> datetime | None:
    if not v:
        return None
    m = _DATE_RE.fullmatch(v)
    if not m:
        raise ValueError(f"not an OData date literal: {v!r}")
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC)


def parse_sap_date(v: str | None) -> date | None:
    dt = parse_sap_datetime(v)
    return dt.date() if dt else None


def odata_datetimeoffset(dt: datetime) -> str:
    return "datetimeoffset'" + dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z'"


def _results(v: Any) -> list[dict]:
    if isinstance(v, dict) and "results" in v:
        return v["results"]
    return []


class S4ODataErpAdapter:
    def __init__(self, client: SapODataClient) -> None:
        self._c = client
        self._materials: dict[str, tuple[float, CanonicalMaterial]] = {}

    # ------------------------------------------------------------------ inbound
    async def fetch_changed_orders(self, plant: str, since: datetime, *, skip: int, top: int) -> list[CanonicalWorkOrder]:
        data = await self._c.get(
            PO_SRV,
            "A_ProductionOrder_2",
            {
                "$filter": f"ProductionPlant eq '{plant}' and LastChangeDateTime ge {odata_datetimeoffset(since)}",
                "$orderby": "LastChangeDateTime asc,ManufacturingOrder asc",
                "$expand": "to_ProductionOrderOperation,to_ProductionOrderComponent",
                "$top": str(top),
                "$skip": str(skip),
            },
        )
        return [await self._to_canonical(e) for e in data["d"]["results"]]

    async def fetch_order(self, order_number: str) -> CanonicalWorkOrder:
        data = await self._c.get(
            PO_SRV,
            f"A_ProductionOrder_2('{order_number}')",
            {"$expand": "to_ProductionOrderOperation,to_ProductionOrderComponent"},
        )
        return await self._to_canonical(data["d"])

    async def material(self, number: str) -> CanonicalMaterial:
        cached = self._materials.get(number)
        if cached and time.monotonic() - cached[0] < _MATERIAL_TTL_S:
            return cached[1]
        d = (await self._c.get(PRODUCT_SRV, f"A_Product('{number}')", {"$expand": "to_Description,to_Plant"}))["d"]
        desc = next(
            (x["ProductDescription"] for x in _results(d.get("to_Description")) if x.get("Language") == "EN"),
            number,
        )
        serial = next((x.get("SerialNumberProfile") for x in _results(d.get("to_Plant")) if x.get("SerialNumberProfile")), None)
        mat = CanonicalMaterial(
            number=d["Product"],
            description=desc,
            base_uom=d["BaseUnit"],
            material_type=d["ProductType"],
            serial_profile=serial or None,
        )
        self._materials[number] = (time.monotonic(), mat)
        return mat

    async def _to_canonical(self, e: dict) -> CanonicalWorkOrder:
        status = ["REL" if e.get("OrderIsReleased") == "X" else "CRTD"]
        for field, code in (
            ("OrderIsPartiallyConfirmed", "PCNF"),
            ("OrderIsConfirmed", "CNF"),
            ("OrderIsTechnicallyCompleted", "TECO"),
            ("OrderIsMarkedForDeletion", "DLFL"),
        ):
            if e.get(field) == "X":
                status.append(code)
        if Decimal(e.get("ActualDeliveredQuantity") or "0") > 0:
            status.append("PDLV")

        operations = [
            CanonicalOperation(
                op_no=o["ManufacturingOrderOperation"],
                description=o.get("MfgOrderOperationText", ""),
                work_center=o["WorkCenter"],
                control_key=o.get("OperationControlProfile", ""),
                planned_qty=Decimal(o["OpPlannedTotalQuantity"]),
                uom=o["OperationUnit"],
                setup_min=_minutes(o, 1),
                run_min_per_unit=_minutes(o, 2) / Decimal(o.get("OperationReferenceQuantity") or "1"),
                labor_min_per_unit=_minutes(o, 3) / Decimal(o.get("OperationReferenceQuantity") or "1"),
                erp_routing_no=o["OrderInternalBillOfOperations"],
                erp_routing_counter=o["OrderIntBillOfOperationsItem"],
                erp_confirmed_yield=Decimal(o.get("OpTotalConfirmedYieldQty") or "0"),
                erp_confirmed_scrap=Decimal(o.get("OpTotalConfirmedScrapQty") or "0"),
                erp_final_confirmed=o.get("OperationIsConfirmed") == "X",
            )
            for o in sorted(_results(e.get("to_ProductionOrderOperation")), key=lambda o: o["ManufacturingOrderOperation"])
        ]
        components = [
            CanonicalComponent(
                material=await self.material(c["Material"]),
                op_no=c.get("ManufacturingOrderOperation") or None,
                required_qty=Decimal(c["RequiredQuantity"]),
                uom=c["BaseUnit"],
                reservation_no=c["Reservation"],
                reservation_item=c["ReservationItem"],
                storage_location=c.get("StorageLocation") or None,
                backflush=c.get("MaterialComponentIsBackflushed") == "X",
                withdrawn_qty=Decimal(c.get("WithdrawnQuantity") or "0"),
            )
            for c in _results(e.get("to_ProductionOrderComponent"))
        ]
        last_changed = parse_sap_datetime(e["LastChangeDateTime"])
        start = parse_sap_date(e["MfgOrderPlannedStartDate"])
        end = parse_sap_date(e["MfgOrderPlannedEndDate"])
        if last_changed is None or start is None or end is None:
            raise ErpError(ErpErrorKind.PERMANENT, f"order {e.get('ManufacturingOrder')} lacks dates")
        return CanonicalWorkOrder(
            erp_order_number=e["ManufacturingOrder"],
            erp_plant=e["ProductionPlant"],
            order_type=e["ManufacturingOrderType"],
            material=await self.material(e["Material"]),
            planned_qty=Decimal(e["MfgOrderPlannedTotalQty"]),
            uom=e["ProductionUnit"],
            basic_start=start,
            basic_end=end,
            erp_status=status,
            erp_last_changed_at=last_changed,
            delivered_qty=Decimal(e.get("ActualDeliveredQuantity") or "0"),
            operations=operations,
            components=components,
        )

    # ------------------------------------------------------------------ outbound
    async def post_confirmation(self, cmd: ConfirmationCommand) -> ConfirmationPosted:
        body = {
            "OrderID": cmd.erp_order_number,
            "Sequence": "0",
            "OrderOperation": cmd.op_no,
            "ConfirmationUnit": cmd.uom,
            "ConfirmationYieldQuantity": str(cmd.yield_qty),
            "ConfirmationScrapQuantity": str(cmd.scrap_qty),
            "ConfirmationReworkQuantity": str(cmd.rework_qty),
            "IsFinalConfirmation": cmd.final,
            "OpConfirmedWorkQuantity1": str(cmd.setup_min),
            "OpWorkQuantityUnit1": "MIN",
            "OpConfirmedWorkQuantity2": str(cmd.run_min),
            "OpWorkQuantityUnit2": "MIN",
            "OpConfirmedWorkQuantity3": str(cmd.labor_min),
            "OpWorkQuantityUnit3": "MIN",
            "ConfirmationText": cmd.marker,
        }
        d = (await self._c.post(CONF_SRV, "ProdnOrdConf2", body))["d"]
        return ConfirmationPosted(erp_confirmation_group=d["ConfirmationGroup"], erp_confirmation_count=d["ConfirmationCount"])

    async def find_confirmation(self, cmd: ConfirmationCommand) -> ConfirmationPosted | None:
        data = await self._c.get(
            CONF_SRV,
            "ProdnOrdConf2",
            {"$filter": f"OrderID eq '{cmd.erp_order_number}' and OrderOperation eq '{cmd.op_no}'"},
        )
        for c in data["d"]["results"]:
            if c.get("ConfirmationText") == cmd.marker:
                return ConfirmationPosted(
                    erp_confirmation_group=c["ConfirmationGroup"], erp_confirmation_count=c["ConfirmationCount"]
                )
        return None

    async def post_goods_movement(self, cmd: GoodsMovementCommand) -> MaterialDocumentPosted:
        code = GOODS_MOVEMENT_CODES.get(cmd.movement_type)
        if code is None:
            raise ErpError(ErpErrorKind.PERMANENT, f"movement type {cmd.movement_type} not supported")
        item: dict[str, Any] = {
            "Material": cmd.material,
            "Plant": cmd.erp_plant,
            "StorageLocation": cmd.storage_location,
            "GoodsMovementType": cmd.movement_type,
            "QuantityInEntryUnit": str(cmd.quantity),
            "EntryUnit": cmd.uom,
            "Batch": cmd.batch,
        }
        if cmd.erp_order_number:
            item["ManufacturingOrder"] = cmd.erp_order_number
        if cmd.reservation_no:
            item["Reservation"], item["ReservationItem"] = cmd.reservation_no, cmd.reservation_item
        if cmd.movement_type == "311":
            item["IssuingOrReceivingPlant"] = cmd.erp_plant
            item["IssuingOrReceivingStorageLoc"] = cmd.destination_storage_location
        if cmd.stock_type and cmd.movement_type in ("101", "501"):
            item["InventoryStockType"] = STOCK_TYPES[cmd.stock_type]
        elif cmd.movement_type in FROM_BLOCKED:
            item["InventoryStockType"] = STOCK_TYPES["BLOCKED"]
        if cmd.serial_numbers:
            item["to_SerialNumbers"] = {"results": [{"SerialNumber": sn} for sn in cmd.serial_numbers]}
        if cmd.reason:
            item["MaterialDocumentItemText"] = cmd.reason[:50]
        body = {
            "GoodsMovementCode": code,
            "PostingDate": sap_date_literal(cmd.posting_date),
            "MaterialDocumentHeaderText": cmd.marker,
            "to_MaterialDocumentItem": {"results": [item]},
        }
        d = (await self._c.post(MATDOC_SRV, "A_MaterialDocumentHeader", body))["d"]
        return MaterialDocumentPosted(erp_material_document=d["MaterialDocument"], erp_document_year=d["MaterialDocumentYear"])

    async def find_goods_movement(self, cmd: GoodsMovementCommand) -> MaterialDocumentPosted | None:
        data = await self._c.get(
            MATDOC_SRV, "A_MaterialDocumentHeader", {"$filter": f"MaterialDocumentHeaderText eq '{cmd.marker}'"}
        )
        for d in data["d"]["results"]:
            return MaterialDocumentPosted(erp_material_document=d["MaterialDocument"], erp_document_year=d["MaterialDocumentYear"])
        return None


    # ------------------------------------------------------------------ plant maintenance
    async def _get_or_none(self, service: str, path: str) -> dict | None:
        try:
            return (await self._c.get(service, path))["d"]
        except ErpError as e:
            if e.http_status == 404:
                return None
            raise

    async def post_maint_notification(self, cmd: MaintNotificationCommand) -> MaintPosted:
        body = {
            "MaintenanceNotification": cmd.number,
            "NotificationType": cmd.notif_type,
            "MaintPriority": cmd.priority,
            "NotificationText": cmd.text[:40],
            "MaintNotifLongTextForEdit": cmd.long_text,
            "TechnicalObject": cmd.equipment,
            "TechObjIsEquipOrFuncnlLoc": "EAMS_EQUI",
            "MaintenancePlanningPlant": cmd.erp_plant,
            "MaintNotifIsBreakdown": cmd.breakdown,
            "MalfunctionStartDateTime": sap_datetime_literal(cmd.malfunction_start),
            "ReportedByUser": (cmd.reported_by or "MES")[:12],
        }
        d = (await self._c.post(NOTIF_SRV, "MaintenanceNotification", body))["d"]
        return MaintPosted(number=d["MaintenanceNotification"], system_status=d.get("NotifProcessingPhase"))

    async def find_maint_notification(self, cmd: MaintNotificationCommand) -> MaintPosted | None:
        d = await self._get_or_none(NOTIF_SRV, f"MaintenanceNotification('{cmd.number}')")
        return MaintPosted(number=d["MaintenanceNotification"], system_status=d.get("NotifProcessingPhase")) if d else None

    async def post_maint_order(self, cmd: MaintOrderCommand) -> MaintPosted:
        body = {
            "MaintenanceOrder": cmd.number,
            "MaintenanceOrderType": cmd.order_type,
            "MaintenanceOrderDesc": cmd.description[:40],
            "Equipment": cmd.equipment,
            "MainWorkCenter": cmd.work_center,
            "MaintenanceNotification": cmd.notification_number,
            "MaintPriority": cmd.priority,
            "MaintenancePlanningPlant": cmd.erp_plant,
            "MaintOrdBasicStartDateTime": sap_datetime_literal(cmd.planned_start),
            "PlannedDuration": cmd.duration_min,
        }
        d = (await self._c.post(MAINTORDER_SRV, "MaintenanceOrder", body))["d"]
        return MaintPosted(number=d["MaintenanceOrder"], system_status=d.get("MaintOrdProcessingStatus"))

    async def find_maint_order(self, cmd: MaintOrderCommand) -> MaintPosted | None:
        d = await self._get_or_none(MAINTORDER_SRV, f"MaintenanceOrder('{cmd.number}')")
        return MaintPosted(number=d["MaintenanceOrder"], system_status=d.get("MaintOrdProcessingStatus")) if d else None

    async def set_maint_order_status(self, cmd: MaintOrderStatusCommand) -> MaintPosted:
        body: dict[str, Any] = {}
        if cmd.labor_min is not None:
            body["ActualWorkQuantity"] = str(cmd.labor_min)
        if cmd.findings:
            body["MaintOrderConfirmationText"] = cmd.findings[:132]
        path = f"ChangeMaintOrderStatus?MaintenanceOrder='{cmd.number}'&SystemStatus='{cmd.status}'"
        d = (await self._c.post(MAINTORDER_SRV, path, body))["d"]
        return MaintPosted(number=d["MaintenanceOrder"], system_status=d.get("MaintOrdProcessingStatus"))

    async def find_maint_order_status(self, cmd: MaintOrderStatusCommand) -> MaintPosted | None:
        d = await self._get_or_none(MAINTORDER_SRV, f"MaintenanceOrder('{cmd.number}')")
        if d is None:
            return None
        have = d.get("MaintOrdProcessingStatus")
        if have in PM_STATUS_ORDER and PM_STATUS_ORDER.index(have) >= PM_STATUS_ORDER.index(cmd.status):
            return MaintPosted(number=cmd.number, system_status=have)
        return None

    async def fetch_changed_maint_orders(self, plant: str, since: datetime) -> list[CanonicalMaintOrderChange]:
        data = await self._c.get(MAINTORDER_SRV, "MaintenanceOrder", {
            "$filter": f"MaintenancePlanningPlant eq '{plant}' and LastChangeDateTime ge {odata_datetimeoffset(since)}",
            "$orderby": "LastChangeDateTime asc",
        })
        out = []
        for d in _results(data["d"]):
            changed = parse_sap_datetime(d["LastChangeDateTime"])
            assert changed is not None
            out.append(CanonicalMaintOrderChange(number=d["MaintenanceOrder"], erp_plant=plant,
                                                 system_status=d["MaintOrdProcessingStatus"], changed_at=changed))
        return out


def sap_datetime_literal(dt: datetime) -> str:
    return f"/Date({int(dt.timestamp() * 1000)})/"


def sap_date_literal(d: date) -> str:
    return f"/Date({int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)})/"


def _minutes(o: dict, n: int) -> Decimal:
    value = Decimal(o.get(f"StandardWorkQuantity{n}") or "0")
    unit = (o.get(f"StandardWorkQuantityUnit{n}") or "MIN").upper()
    return value * 60 if unit in ("H", "HR", "STD") else value
