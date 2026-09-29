"""Plant maintenance (PM) look-alike of the S/4HANA standard OData APIs API_MAINTNOTIFICATION and
API_MAINTENANCEORDER (V2 style). Numbers are supplied by MES (external number range), so a repeated create with
the same number is rejected as a duplicate, exactly what the adapter's reconcile step relies on.

Status change is a function import: POST .../ChangeMaintOrderStatus?MaintenanceOrder='..'&SystemStatus='TECO'.
"""

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request

from mock_sap.odata import FilterError, apply_query, collection, odata_error, sap_datetimeoffset
from mock_sap.store import PLANT, MaintNotification, MaintOrder, Store, now_utc

ROOT = "/sap/opu/odata/sap"
NOTIF_SRV = "API_MAINTNOTIFICATION"
ORDER_SRV = "API_MAINTENANCEORDER"
ORDER_STATUSES = ("CRTD", "REL", "TECO")
NOTIF_STATUS = {"CRTD": "OSNO", "REL": "NOPR", "TECO": "NOCO"}


def _notif_entity(n: MaintNotification) -> dict[str, Any]:
    return {
        "__metadata": {"type": f"{NOTIF_SRV}.MaintenanceNotificationType"},
        "MaintenanceNotification": n.number,
        "NotificationType": n.notif_type,
        "MaintPriority": n.priority,
        "NotificationText": n.text,
        "MaintNotifLongTextForEdit": n.long_text,
        "TechnicalObject": n.equipment,
        "TechObjIsEquipOrFuncnlLoc": "EAMS_EQUI",
        "MaintenancePlanningPlant": PLANT,
        "MaintNotifIsBreakdown": n.breakdown,
        "MalfunctionStartDateTime": sap_datetimeoffset(datetime.fromisoformat(n.malfunction_start)),
        "ReportedByUser": n.reported_by,
        "NotifProcessingPhase": n.status,
        "LastChangeDateTime": sap_datetimeoffset(datetime.fromisoformat(n.last_change)),
    }


def _order_entity(o: MaintOrder) -> dict[str, Any]:
    return {
        "__metadata": {"type": f"{ORDER_SRV}.MaintenanceOrderType"},
        "MaintenanceOrder": o.number,
        "MaintenanceOrderType": o.order_type,
        "MaintenanceOrderDesc": o.description,
        "Equipment": o.equipment,
        "MainWorkCenter": o.work_center,
        "MaintenanceNotification": o.notification,
        "MaintPriority": o.priority,
        "MaintenancePlanningPlant": PLANT,
        "MaintOrdBasicStartDateTime": sap_datetimeoffset(datetime.fromisoformat(o.planned_start)),
        "PlannedDuration": o.duration_min,
        "MaintOrdProcessingStatus": o.system_status,
        "ActualWorkQuantity": o.labor_min,
        "MaintOrderConfirmationText": o.findings,
        "LastChangeDateTime": sap_datetimeoffset(datetime.fromisoformat(o.last_change)),
    }


def register_pm(app: FastAPI, store: Store, injected_failure: Any, maybe_hang: Any) -> None:
    notif_base = f"{ROOT}/{NOTIF_SRV}"
    order_base = f"{ROOT}/{ORDER_SRV}"

    @app.post(notif_base + "/MaintenanceNotification")
    async def create_notification(request: Request) -> Any:
        if (resp := injected_failure(store)) is not None:
            return resp
        b = await request.json()
        number = str(b.get("MaintenanceNotification") or "")
        if not number.isdigit() or len(number) > 12:
            return odata_error(400, "IM/024", "Notification number must be numeric (external number range)")
        if b.get("NotificationType") not in ("M1", "M2"):
            return odata_error(400, "IM/015", f"Notification type {b.get('NotificationType')} not allowed")
        if not b.get("TechnicalObject"):
            return odata_error(400, "IM/022", "Enter a technical object (equipment)")
        with store.lock:
            if number in store.maint_notifications:
                return odata_error(400, "IM/025", f"Notification {number} already exists")
            now = now_utc().isoformat()
            n = MaintNotification(
                number=number, notif_type=b["NotificationType"], priority=str(b.get("MaintPriority") or "3"),
                text=str(b.get("NotificationText") or "")[:40], long_text=str(b.get("MaintNotifLongTextForEdit") or ""),
                equipment=b["TechnicalObject"], breakdown=bool(b.get("MaintNotifIsBreakdown")),
                malfunction_start=_iso(b.get("MalfunctionStartDateTime")) or now,
                reported_by=str(b.get("ReportedByUser") or "MES"), status="OSNO", created_at=now, last_change=now,
            )
            store.maint_notifications[number] = n
            store.save()
            entity = _notif_entity(n)
        await maybe_hang(store)
        return {"d": entity}

    @app.get(notif_base + "/MaintenanceNotification('{number}')")
    async def get_notification(number: str) -> Any:
        n = store.maint_notifications.get(number)
        if n is None:
            return odata_error(404, "IM/031", f"Notification {number} does not exist")
        return {"d": _notif_entity(n)}

    @app.post(order_base + "/MaintenanceOrder")
    async def create_order(request: Request) -> Any:
        if (resp := injected_failure(store)) is not None:
            return resp
        b = await request.json()
        number = str(b.get("MaintenanceOrder") or "")
        if not number.isdigit() or len(number) > 12:
            return odata_error(400, "IW/049", "Order number must be numeric (external number range)")
        if b.get("MaintenanceOrderType") not in ("PM01", "PM02"):
            return odata_error(400, "IW/020", f"Order type {b.get('MaintenanceOrderType')} not allowed")
        with store.lock:
            if number in store.maint_orders:
                return odata_error(400, "IW/050", f"Order {number} already exists")
            now = now_utc().isoformat()
            notif = b.get("MaintenanceNotification")
            o = MaintOrder(
                number=number, order_type=b["MaintenanceOrderType"], description=str(b.get("MaintenanceOrderDesc") or "")[:40],
                equipment=str(b.get("Equipment") or ""), work_center=b.get("MainWorkCenter"), notification=notif,
                priority=str(b.get("MaintPriority") or "3"), planned_start=_iso(b.get("MaintOrdBasicStartDateTime")) or now,
                duration_min=int(b.get("PlannedDuration") or 60), system_status="CRTD", labor_min=None, findings=None,
                created_at=now, last_change=now, history=[{"at": now, "status": "CRTD"}],
            )
            store.maint_orders[number] = o
            if notif and notif in store.maint_notifications:
                store.maint_notifications[notif].status = "NOPR"
                store.maint_notifications[notif].last_change = now
            store.save()
            entity = _order_entity(o)
        await maybe_hang(store)
        return {"d": entity}

    @app.get(order_base + "/MaintenanceOrder('{number}')")
    async def get_order(number: str) -> Any:
        o = store.maint_orders.get(number)
        if o is None:
            return odata_error(404, "IW/031", f"Order {number} does not exist")
        return {"d": _order_entity(o)}

    @app.get(order_base + "/MaintenanceOrder")
    async def list_orders(request: Request) -> Any:
        q = request.query_params
        with store.lock:
            rows = list(store.maint_orders.values())
        try:
            page, total = apply_query(
                rows,
                getter=lambda o, f: {"MaintenanceOrder": o.number, "MaintOrdProcessingStatus": o.system_status,
                                     "MaintenancePlanningPlant": PLANT,
                                     "LastChangeDateTime": datetime.fromisoformat(o.last_change)}.get(f),
                filter_expr=q.get("$filter"), orderby=q.get("$orderby"), top=_int(q.get("$top")), skip=_int(q.get("$skip")),
            )
        except FilterError as e:
            return odata_error(400, "/IWBEP/CM_MGW_RT/022", str(e))
        return collection([_order_entity(o) for o in page], total if q.get("$inlinecount") == "allpages" else None)

    @app.post(order_base + "/ChangeMaintOrderStatus")
    async def change_status(request: Request) -> Any:
        """Function import. Idempotent in effect: setting the status an order already has is accepted."""
        if (resp := injected_failure(store)) is not None:
            return resp
        q = request.query_params
        number = (q.get("MaintenanceOrder") or "").strip("'")
        status = (q.get("SystemStatus") or "").strip("'")
        body: dict[str, Any] = {}
        if request.headers.get("content-length") not in (None, "0"):
            body = await request.json()
        if status not in ORDER_STATUSES:
            return odata_error(400, "IW/066", f"Status {status} not allowed")
        with store.lock:
            o = store.maint_orders.get(number)
            if o is None:
                return odata_error(404, "IW/031", f"Order {number} does not exist")
            if ORDER_STATUSES.index(status) < ORDER_STATUSES.index(o.system_status):
                return odata_error(400, "IW/067", f"Order {number} is already {o.system_status}")
            _set_status(store, o, status, body)
            store.save()
            entity = _order_entity(o)
        await maybe_hang(store)
        return {"d": entity}

    # ------------------------------------------------------------------ demo control
    @app.get("/_mock/maintenance")
    async def mock_maintenance() -> dict:
        with store.lock:
            return {
                "notifications": [_notif_entity(n) | {"created_at": n.created_at} for n in
                                  sorted(store.maint_notifications.values(), key=lambda x: x.created_at, reverse=True)],
                "orders": [_order_entity(o) | {"created_at": o.created_at, "history": o.history} for o in
                           sorted(store.maint_orders.values(), key=lambda x: x.created_at, reverse=True)],
            }

    @app.post("/_mock/maintenance/orders/{number}/teco")
    async def mock_teco(number: str) -> Any:
        """A planner technically completes the order in SAP (the change flows back to MES via the poller)."""
        with store.lock:
            o = store.maint_orders.get(number)
            if o is None:
                return odata_error(404, "IW/031", f"Order {number} does not exist")
            _set_status(store, o, "TECO", {"MaintOrderConfirmationText": "technically completed in SAP"})
            store.save()
            return _order_entity(o)


def _set_status(store: Store, o: MaintOrder, status: str, body: dict[str, Any]) -> None:
    if o.system_status == status:
        return
    now = now_utc().isoformat()
    o.system_status, o.last_change = status, now
    if body.get("ActualWorkQuantity") is not None:
        o.labor_min = str(body["ActualWorkQuantity"])
    if body.get("MaintOrderConfirmationText"):
        o.findings = str(body["MaintOrderConfirmationText"])[:132]
    o.history.append({"at": now, "status": status})
    if o.notification and o.notification in store.maint_notifications:
        n = store.maint_notifications[o.notification]
        n.status, n.last_change = NOTIF_STATUS[status], now


def _iso(v: Any) -> str | None:
    """Accept an ISO string or an OData /Date(ms)/ literal."""
    if not v:
        return None
    s = str(v)
    if m := re.match(r"/Date\((-?\d+)", s):
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC).isoformat()
    return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()


def _int(v: str | None) -> int | None:
    return int(v) if v else None
