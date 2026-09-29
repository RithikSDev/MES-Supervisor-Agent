"""Mock SAP S/4HANA On-Prem (PP) exposing:

  API_PRODUCTION_ORDER_2_SRV        A_ProductionOrder_2 (+ operations, components)
  API_PRODUCT_SRV                   A_Product (+ descriptions, plant data)
  API_PROD_ORDER_CONFIRMATION_2_SRV ProdnOrdConf2 (GET / POST)
  API_MATERIAL_DOCUMENT_SRV         A_MaterialDocumentHeader (GET / POST; 261 GI / 101 GR)

Gateway behaviour copied on purpose: basic auth, x-csrf-token Fetch handshake bound to a session cookie,
OData V2 JSON ("d" wrapper, /Date()/ literals, decimals as strings), SAP-style error bodies.

/_mock/* is an unauthenticated control API for demos: generate/release/TECO orders and inject faults.
Confirmation postings are NOT idempotent, just like real SAP; that is the point of the adapter's reconciliation.
"""

import asyncio
import base64
import os
import secrets
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mock_sap.odata import (
    FilterError,
    apply_query,
    collection,
    dec,
    flag,
    odata_error,
    sap_date,
    sap_datetimeoffset,
)
from mock_sap.pm import register_pm
from mock_sap.store import PLANT, PRODUCTS, Confirmation, MaterialDocument, ProductionOrder, Store, now_utc

ROOT = "/sap/opu/odata/sap"
CSRF_COOKIE = "sap-XSRF_MCK_100"
LOCK_CODE = "CO/469"
ORDER_MOVEMENTS = ("261", "262", "101", "102")  # need a production order
# transfer posting, receipt without order/PO, stock type changes (321 QI->free, 350 QI->blocked, 344 free->blocked,
# 343 blocked->free), scrapping 555 and return delivery 122 (mock: without PO reference)
FREE_MOVEMENTS = ("311", "501", "321", "350", "344", "343", "555", "122")

SAP_USER = os.environ.get("MOCK_SAP_USER", "MES_INTEG")
SAP_PASSWORD = os.environ.get("MOCK_SAP_PASSWORD", "mes-integ-secret")


def create_app(store: Store | None = None, *, seed_orders: int | None = None) -> FastAPI:
    store = store or Store(os.environ.get("MOCK_SAP_STATE_FILE"))
    if not store.load():
        n = seed_orders if seed_orders is not None else int(os.environ.get("MOCK_SAP_SEED_ORDERS", "40"))
        if n:
            store.generate_orders(n)

    app = FastAPI(title="Mock SAP S/4HANA (PP)", version="1.0")
    app.state.store = store

    # ------------------------------------------------------------------ gateway middleware
    @app.middleware("http")
    async def gateway(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not request.url.path.startswith(ROOT):
            return await call_next(request)
        if not _basic_auth_ok(request):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="SAP NetWeaver"'})
        if store.faults.latency_ms:
            await asyncio.sleep(store.faults.latency_ms / 1000)
        token_hdr = request.headers.get("x-csrf-token", "")
        if request.method in ("POST", "PUT", "PATCH", "DELETE", "MERGE"):
            cookie = request.cookies.get(CSRF_COOKIE)
            if not token_hdr or token_hdr not in store.csrf_tokens or cookie != token_hdr:
                return Response(
                    content="CSRF token validation failed",
                    status_code=403,
                    headers={"x-csrf-token": "Required"},
                )
            return await call_next(request)
        response = await call_next(request)
        if token_hdr.lower() == "fetch":
            token = secrets.token_urlsafe(18)
            store.csrf_tokens.add(token)
            response.headers["x-csrf-token"] = token
            response.set_cookie(CSRF_COOKIE, token, httponly=True)
        return response

    # ------------------------------------------------------------------ service documents
    @app.get(ROOT + "/{service}/")
    async def service_document(service: str) -> dict:
        sets = {
            "API_PRODUCTION_ORDER_2_SRV": ["A_ProductionOrder_2", "A_ProductionOrderOperation_2",
                                           "A_ProductionOrderComponent_4"],
            "API_PRODUCT_SRV": ["A_Product", "A_ProductDescription", "A_ProductPlant"],
            "API_PROD_ORDER_CONFIRMATION_2_SRV": ["ProdnOrdConf2"],
            "API_MATERIAL_DOCUMENT_SRV": ["A_MaterialDocumentHeader", "A_MaterialDocumentItem"],
            "API_MAINTNOTIFICATION": ["MaintenanceNotification"],
            "API_MAINTENANCEORDER": ["MaintenanceOrder", "MaintenanceOrderOperation"],
        }.get(service)
        if sets is None:
            return odata_error(404, "/IWFND/MED/170", f"No service found for namespace '', name '{service}'")
        return {"d": {"EntitySets": sets}}

    # ------------------------------------------------------------------ production orders
    po_base = ROOT + "/API_PRODUCTION_ORDER_2_SRV"

    @app.get(po_base + "/A_ProductionOrder_2")
    async def list_orders(request: Request) -> Any:
        q = request.query_params
        expand = _expand(q.get("$expand"))
        with store.lock:
            rows = list(store.orders.values())
        try:
            page, total = apply_query(
                rows,
                getter=lambda o, f: _order_fields(o).get(f),
                filter_expr=q.get("$filter"),
                orderby=q.get("$orderby"),
                top=_int(q.get("$top")),
                skip=_int(q.get("$skip")),
            )
        except FilterError as e:
            return odata_error(400, "/IWBEP/CM_MGW_RT/022", str(e))
        count = total if q.get("$inlinecount") == "allpages" else None
        return collection([_order_entity(o, request, expand) for o in page], count)

    @app.get(po_base + "/A_ProductionOrder_2('{order}')")
    async def get_order(order: str, request: Request) -> Any:
        po = store.orders.get(order)
        if po is None:
            return odata_error(404, "RU/001", f"Order {order} does not exist")
        return {"d": _order_entity(po, request, _expand(request.query_params.get("$expand")))}

    # ------------------------------------------------------------------ products
    @app.get(ROOT + "/API_PRODUCT_SRV/A_Product('{product}')")
    async def get_product(product: str, request: Request) -> Any:
        p = PRODUCTS.get(product)
        if p is None:
            return odata_error(404, "M3/305", f"Material {product} does not exist")
        expand = _expand(request.query_params.get("$expand"))
        base = f"{_host(request)}{ROOT}/API_PRODUCT_SRV"
        d: dict[str, Any] = {
            "__metadata": {"uri": f"{base}/A_Product('{p.product}')", "type": "API_PRODUCT_SRV.A_ProductType"},
            "Product": p.product,
            "ProductType": p.product_type,
            "BaseUnit": p.base_unit,
            "IsMarkedForDeletion": False,
        }
        desc = [{"Product": p.product, "Language": "EN", "ProductDescription": p.description}]
        plant = [{"Product": p.product, "Plant": PLANT, "SerialNumberProfile": p.serial_profile}]
        d["to_Description"] = {"results": desc} if "to_Description" in expand else _deferred(base, "to_Description")
        d["to_Plant"] = {"results": plant} if "to_Plant" in expand else _deferred(base, "to_Plant")
        return {"d": d}

    # ------------------------------------------------------------------ confirmations
    conf_base = ROOT + "/API_PROD_ORDER_CONFIRMATION_2_SRV"

    @app.get(conf_base + "/ProdnOrdConf2")
    async def list_confirmations(request: Request) -> Any:
        q = request.query_params
        with store.lock:
            rows = list(store.confirmations)
        try:
            page, total = apply_query(
                rows,
                getter=lambda c, f: _conf_fields(c).get(f),
                filter_expr=q.get("$filter"),
                orderby=q.get("$orderby"),
                top=_int(q.get("$top")),
                skip=_int(q.get("$skip")),
            )
        except FilterError as e:
            return odata_error(400, "/IWBEP/CM_MGW_RT/022", str(e))
        return collection([_conf_entity(c) for c in page], total if q.get("$inlinecount") == "allpages" else None)

    @app.post(conf_base + "/ProdnOrdConf2")
    async def post_confirmation(request: Request) -> Any:
        if (resp := _injected_failure(store)) is not None:
            return resp
        body = await request.json()
        order_no = str(body.get("OrderID", "")).lstrip("0") or ""
        op_no = str(body.get("OrderOperation", "")).zfill(4)
        with store.lock:
            po = store.orders.get(order_no)
            if po is None:
                return odata_error(400, "RU/001", f"Order {body.get('OrderID')} does not exist")
            if not po.released:
                return odata_error(400, "RU/003", f"Order {order_no} is not released")
            if po.technically_completed or po.deletion_flag:
                return odata_error(400, "RU/004", f"Order {order_no} is technically completed or deleted")
            op = next((o for o in po.operations if o.op == op_no), None)
            if op is None:
                return odata_error(400, "RU/002", f"Operation {op_no} does not exist in order {order_no}")
            if op.final_confirmed:
                return odata_error(400, "RU/005", f"Operation {order_no}/{op_no} is already finally confirmed")
            try:
                yld = Decimal(str(body.get("ConfirmationYieldQuantity", "0")))
                scrap = Decimal(str(body.get("ConfirmationScrapQuantity", "0")))
                rework = Decimal(str(body.get("ConfirmationReworkQuantity", "0")))
            except InvalidOperation:
                return odata_error(400, "RU/006", "Quantity is not numeric")
            if yld < 0 or scrap < 0 or rework < 0:
                return odata_error(400, "RU/006", "Quantities must not be negative")

            group = f"{store.next_conf_group:010d}"
            existing = [c for c in store.confirmations if c.order == order_no and c.op == op_no]
            if existing:
                group = existing[0].group
            else:
                store.next_conf_group += 1
            conf = Confirmation(
                group=group,
                count=f"{len(existing) + 1:08d}",
                order=order_no,
                op=op_no,
                work_center=op.work_center,
                plant=po.plant,
                yield_qty=str(yld),
                scrap_qty=str(scrap),
                rework_qty=str(rework),
                unit=body.get("ConfirmationUnit") or po.unit,
                final=bool(body.get("IsFinalConfirmation", False)),
                setup_min=str(body.get("OpConfirmedWorkQuantity1", "0")),
                machine_min=str(body.get("OpConfirmedWorkQuantity2", "0")),
                labor_min=str(body.get("OpConfirmedWorkQuantity3", "0")),
                text=str(body.get("ConfirmationText", ""))[:40],
                posting_date=date.today().isoformat(),
                entered_at=now_utc().isoformat(),
                entered_by=SAP_USER,
            )
            store.confirmations.append(conf)
            op.confirmed_yield = str(Decimal(op.confirmed_yield) + yld)
            op.confirmed_scrap = str(Decimal(op.confirmed_scrap) + scrap)
            op.final_confirmed = op.final_confirmed or conf.final
            po.touch()
            store.save()
            entity = _conf_entity(conf)
        await _maybe_hang_after_commit(store, "confirmation")
        return JSONResponse(status_code=201, content={"d": entity})

    # ------------------------------------------------------------------ material documents
    md_base = ROOT + "/API_MATERIAL_DOCUMENT_SRV"

    @app.get(md_base + "/A_MaterialDocumentHeader")
    async def list_material_documents(request: Request) -> Any:
        q = request.query_params
        with store.lock:
            rows = list(store.material_documents)
        try:
            page, total = apply_query(
                rows,
                getter=lambda m, f: {
                    "MaterialDocument": m.document,
                    "MaterialDocumentYear": m.year,
                    "MaterialDocumentHeaderText": m.header_text,
                    "GoodsMovementCode": m.movement_code,
                }.get(f),
                filter_expr=q.get("$filter"),
                orderby=q.get("$orderby"),
                top=_int(q.get("$top")),
                skip=_int(q.get("$skip")),
            )
        except FilterError as e:
            return odata_error(400, "/IWBEP/CM_MGW_RT/022", str(e))
        return collection([_matdoc_entity(m) for m in page], total if q.get("$inlinecount") == "allpages" else None)

    @app.post(md_base + "/A_MaterialDocumentHeader")
    async def post_material_document(request: Request) -> Any:
        if (resp := _injected_failure(store)) is not None:
            return resp
        body = await request.json()
        items_raw = body.get("to_MaterialDocumentItem") or []
        if isinstance(items_raw, dict):
            items_raw = items_raw.get("results", [])
        if not items_raw:
            return odata_error(400, "M7/022", "No items transferred")
        with store.lock:
            for it in items_raw:
                mvt = str(it.get("GoodsMovementType", ""))
                if mvt not in ORDER_MOVEMENTS + FREE_MOVEMENTS:
                    return odata_error(400, "M7/023", f"Movement type {mvt} not supported by mock")
                if not it.get("Material") or it.get("Material") not in PRODUCTS:
                    return odata_error(400, "M7/021", f"Material {it.get('Material')} does not exist")
                if Decimal(str(it.get("QuantityInEntryUnit", "0"))) <= 0:
                    return odata_error(400, "M7/026", "Quantity must be positive")
                if mvt == "311" and not it.get("IssuingOrReceivingStorageLoc"):
                    return odata_error(400, "M7/027", "Receiving storage location missing for transfer 311")
                serials = it.get("to_SerialNumbers") or {}
                serials = serials.get("results", []) if isinstance(serials, dict) else serials
                if serials and len(serials) != Decimal(str(it.get("QuantityInEntryUnit", "0"))):
                    return odata_error(400, "IO/220", "Number of serial numbers does not match the quantity")
                if mvt in FREE_MOVEMENTS:
                    continue
                order_no = str(it.get("ManufacturingOrder", "")).lstrip("0")
                if mvt == "261" and order_no in store.maint_orders:
                    continue  # spare parts to a maintenance order
                po = store.orders.get(order_no)
                if po is None:
                    return odata_error(400, "M7/024", f"Order {order_no} does not exist")
                if mvt in ("101", "102") and it.get("Material") != po.material:
                    return odata_error(400, "M7/025", f"Material {it.get('Material')} is not the order material")
            doc = MaterialDocument(
                document=str(store.next_matdoc),
                year=str(date.today().year),
                movement_code=str(body.get("GoodsMovementCode", "")),
                posting_date=date.today().isoformat(),
                header_text=str(body.get("MaterialDocumentHeaderText", "")),
                entered_at=now_utc().isoformat(),
            )
            store.next_matdoc += 1
            for n, it in enumerate(items_raw, start=1):
                mvt = str(it["GoodsMovementType"])
                if mvt in FREE_MOVEMENTS or str(it.get("ManufacturingOrder", "")).lstrip("0") in store.maint_orders:
                    doc.items.append({**it, "MaterialDocumentItem": f"{n:04d}"})
                    continue
                po = store.orders[str(it["ManufacturingOrder"]).lstrip("0")]
                qty = Decimal(str(it.get("QuantityInEntryUnit", "0")))
                sign = -1 if mvt in ("262", "102") else 1
                if mvt in ("261", "262"):
                    comp = next((c for c in po.components if c.material == it.get("Material")), None)
                    if comp:
                        comp.withdrawn_qty = str(Decimal(comp.withdrawn_qty) + sign * qty)
                else:
                    po.delivered_qty = str(Decimal(po.delivered_qty) + sign * qty)
                po.touch()
                doc.items.append({**it, "MaterialDocumentItem": f"{n:04d}"})
            store.material_documents.append(doc)
            store.save()
            entity = _matdoc_entity(doc)
        await _maybe_hang_after_commit(store, "material_document")
        return JSONResponse(status_code=201, content={"d": entity})

    register_pm(app, store, _injected_failure, _maybe_hang_after_commit)

    # ------------------------------------------------------------------ demo control API
    class GenerateRequest(BaseModel):
        count: int = 10
        release_ratio: float = 0.8

    class FaultSettings(BaseModel):
        latency_ms: int = 0
        fail_next_503: int = 0
        fail_next_lock: int = 0
        commit_then_timeout_next: int = 0
        hang_seconds: float = 15.0
        fault_scope: str = ""

    @app.get("/_mock/health")
    async def health() -> dict:
        return {"status": "UP"}

    @app.post("/_mock/orders/generate")
    async def generate(req: GenerateRequest) -> dict:
        return {"created": store.generate_orders(max(1, min(req.count, 200)), req.release_ratio)}

    @app.get("/_mock/orders")
    async def mock_orders() -> list[dict]:
        with store.lock:
            return [
                {
                    "order": o.order,
                    "material": o.material,
                    "description": PRODUCTS[o.material].description,
                    "planned_qty": o.planned_qty,
                    "unit": o.unit,
                    "basic_start": o.basic_start,
                    "basic_end": o.basic_end,
                    "status": _status_text(o),
                    "last_change": o.last_change,
                    "operations": [
                        {"op": op.op, "work_center": op.work_center, "confirmed_yield": op.confirmed_yield,
                         "confirmed_scrap": op.confirmed_scrap, "final": op.final_confirmed}
                        for op in o.operations
                    ],
                }
                for o in sorted(store.orders.values(), key=lambda o: o.order, reverse=True)
            ]

    def _mutate(order: str, fn) -> Any:  # type: ignore[no-untyped-def]
        with store.lock:
            po = store.orders.get(order)
            if po is None:
                return JSONResponse(status_code=404, content={"detail": f"order {order} not found"})
            fn(po)
            po.touch()
            store.save()
            return {"order": po.order, "status": _status_text(po)}

    @app.post("/_mock/orders/{order}/release")
    async def release(order: str) -> Any:
        return _mutate(order, lambda po: setattr(po, "released", True))

    @app.post("/_mock/orders/{order}/teco")
    async def teco(order: str) -> Any:
        return _mutate(order, lambda po: setattr(po, "technically_completed", True))

    @app.post("/_mock/orders/{order}/delete-flag")
    async def delete_flag(order: str) -> Any:
        return _mutate(order, lambda po: setattr(po, "deletion_flag", True))

    class OrderChange(BaseModel):
        planned_qty: Decimal | None = None
        basic_end: date | None = None

    @app.patch("/_mock/orders/{order}")
    async def change_order(order: str, change: OrderChange) -> Any:
        def apply(po: ProductionOrder) -> None:
            if change.planned_qty is not None:
                po.planned_qty = str(change.planned_qty)
                for op in po.operations:
                    op.planned_qty = str(change.planned_qty)
            if change.basic_end is not None:
                po.basic_end = change.basic_end.isoformat()

        return _mutate(order, apply)

    @app.get("/_mock/confirmations")
    async def mock_confirmations() -> list[dict]:
        with store.lock:
            return [asdict(c) for c in reversed(store.confirmations)]

    @app.get("/_mock/material-documents")
    async def mock_material_documents() -> list[dict]:
        with store.lock:
            return [asdict(m) for m in reversed(store.material_documents)]

    @app.get("/_mock/faults")
    async def get_faults() -> dict:
        return asdict(store.faults)

    @app.put("/_mock/faults")
    async def put_faults(f: FaultSettings) -> dict:
        for k, v in f.model_dump().items():
            setattr(store.faults, k, v)
        return asdict(store.faults)

    class ResetRequest(BaseModel):
        seed_orders: int = 0

    @app.post("/_mock/reset")
    async def reset(req: ResetRequest) -> dict:
        store.reset()
        if req.seed_orders:
            store.generate_orders(req.seed_orders)
        return {"orders": len(store.orders)}

    return app


# ---------------------------------------------------------------------- helpers
def _basic_auth_ok(request: Request) -> bool:
    hdr = request.headers.get("authorization", "")
    if not hdr.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(user, SAP_USER) and secrets.compare_digest(pw, SAP_PASSWORD)


def _injected_failure(store: Store) -> Response | None:
    f = store.faults
    with store.lock:
        if f.fail_next_503 > 0:
            f.fail_next_503 -= 1
            return Response(status_code=503, content="Service Unavailable (injected)")
        if f.fail_next_lock > 0:
            f.fail_next_lock -= 1
            return odata_error(400, LOCK_CODE, "Order is currently being processed by user PLANNER01")
    return None


async def _maybe_hang_after_commit(store: Store, scope: str = "pm") -> None:
    """Simulates the nasty case: SAP committed, but the caller never hears back in time."""
    with store.lock:
        hang = store.faults.commit_then_timeout_next > 0 and store.faults.fault_scope in ("", scope)
        if hang:
            store.faults.commit_then_timeout_next -= 1
    if hang:
        await asyncio.sleep(store.faults.hang_seconds)


def _int(v: str | None) -> int | None:
    return int(v) if v else None


def _expand(v: str | None) -> set[str]:
    return {p.strip() for p in v.split(",")} if v else set()


def _host(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _deferred(base: str, nav: str) -> dict:
    return {"__deferred": {"uri": f"{base}/{nav}"}}


def _status_text(po: ProductionOrder) -> str:
    parts = ["REL" if po.released else "CRTD"]
    if po.confirmed:
        parts.append("CNF")
    elif po.partially_confirmed:
        parts.append("PCNF")
    if Decimal(po.delivered_qty) > 0:
        parts.append("PDLV")
    if po.technically_completed:
        parts.append("TECO")
    if po.deletion_flag:
        parts.append("DLFL")
    return " ".join(parts)


def _order_fields(po: ProductionOrder) -> dict[str, Any]:
    return {
        "ManufacturingOrder": po.order,
        "ManufacturingOrderType": po.order_type,
        "Material": po.material,
        "ProductionPlant": po.plant,
        "MfgOrderPlannedTotalQty": Decimal(po.planned_qty),
        "ProductionUnit": po.unit,
        "MfgOrderPlannedStartDate": datetime.fromisoformat(po.basic_start).replace(tzinfo=UTC),
        "MfgOrderPlannedEndDate": datetime.fromisoformat(po.basic_end).replace(tzinfo=UTC),
        "LastChangeDateTime": datetime.fromisoformat(po.last_change),
        "OrderIsReleased": flag(po.released),
        "OrderIsTechnicallyCompleted": flag(po.technically_completed),
    }


def _order_entity(po: ProductionOrder, request: Request, expand: set[str]) -> dict:
    base = f"{_host(request)}{ROOT}/API_PRODUCTION_ORDER_2_SRV"
    uri = f"{base}/A_ProductionOrder_2('{po.order}')"
    d: dict[str, Any] = {
        "__metadata": {"id": uri, "uri": uri, "type": "API_PRODUCTION_ORDER_2_SRV.A_ProductionOrder_2Type"},
        "ManufacturingOrder": po.order,
        "ManufacturingOrderType": po.order_type,
        "Material": po.material,
        "ProductionPlant": po.plant,
        "MfgOrderPlannedTotalQty": dec(Decimal(po.planned_qty)),
        "ProductionUnit": po.unit,
        "MfgOrderPlannedStartDate": sap_date(date.fromisoformat(po.basic_start)),
        "MfgOrderPlannedEndDate": sap_date(date.fromisoformat(po.basic_end)),
        "ActualDeliveredQuantity": dec(Decimal(po.delivered_qty)),
        "OrderIsCreated": "X",
        "OrderIsReleased": flag(po.released),
        "OrderIsPartiallyConfirmed": flag(po.partially_confirmed),
        "OrderIsConfirmed": flag(po.confirmed),
        "OrderIsTechnicallyCompleted": flag(po.technically_completed),
        "OrderIsMarkedForDeletion": flag(po.deletion_flag),
        "LastChangeDateTime": sap_datetimeoffset(datetime.fromisoformat(po.last_change)),
    }
    if "to_ProductionOrderOperation" in expand:
        d["to_ProductionOrderOperation"] = {
            "results": [
                {
                    "ManufacturingOrder": po.order,
                    "ManufacturingOrderSequence": "0",
                    "ManufacturingOrderOperation": op.op,
                    "MfgOrderOperationText": op.text,
                    "WorkCenter": op.work_center,
                    "ProductionPlant": po.plant,
                    "OperationControlProfile": "PP01",
                    "OrderInternalBillOfOperations": op.routing_no,
                    "OrderIntBillOfOperationsItem": op.routing_counter,
                    "OpPlannedTotalQuantity": dec(Decimal(op.planned_qty)),
                    "OperationUnit": po.unit,
                    "OperationReferenceQuantity": dec(1),
                    "StandardWorkQuantity1": dec(op.setup_min),
                    "StandardWorkQuantityUnit1": "MIN",
                    "StandardWorkQuantity2": dec(op.machine_min_per_unit),
                    "StandardWorkQuantityUnit2": "MIN",
                    "StandardWorkQuantity3": dec(op.labor_min_per_unit),
                    "StandardWorkQuantityUnit3": "MIN",
                    "OpTotalConfirmedYieldQty": dec(Decimal(op.confirmed_yield)),
                    "OpTotalConfirmedScrapQty": dec(Decimal(op.confirmed_scrap)),
                    "OperationIsConfirmed": flag(op.final_confirmed),
                }
                for op in po.operations
            ]
        }
    else:
        d["to_ProductionOrderOperation"] = _deferred(uri, "to_ProductionOrderOperation")
    if "to_ProductionOrderComponent" in expand:
        d["to_ProductionOrderComponent"] = {
            "results": [
                {
                    "Reservation": c.reservation,
                    "ReservationItem": c.reservation_item,
                    "ManufacturingOrder": po.order,
                    "ManufacturingOrderOperation": c.op,
                    "Material": c.material,
                    "Plant": po.plant,
                    "StorageLocation": "0001",
                    "RequiredQuantity": dec(Decimal(c.required_qty)),
                    "BaseUnit": c.unit,
                    "WithdrawnQuantity": dec(Decimal(c.withdrawn_qty)),
                    "GoodsMovementType": "261",
                    "MaterialComponentIsBackflushed": flag(c.backflush),
                }
                for c in po.components
            ]
        }
    else:
        d["to_ProductionOrderComponent"] = _deferred(uri, "to_ProductionOrderComponent")
    return d


def _conf_fields(c: Confirmation) -> dict[str, Any]:
    return {
        "ConfirmationGroup": c.group,
        "ConfirmationCount": c.count,
        "OrderID": c.order,
        "OrderOperation": c.op,
        "ConfirmationText": c.text,
        "Plant": c.plant,
    }


def _conf_entity(c: Confirmation) -> dict:
    return {
        "__metadata": {"type": "API_PROD_ORDER_CONFIRMATION_2_SRV.ProdnOrdConf2Type"},
        "ConfirmationGroup": c.group,
        "ConfirmationCount": c.count,
        "OrderID": c.order,
        "Sequence": "0",
        "OrderOperation": c.op,
        "WorkCenter": c.work_center,
        "Plant": c.plant,
        "ConfirmationUnit": c.unit,
        "ConfirmationYieldQuantity": dec(Decimal(c.yield_qty)),
        "ConfirmationScrapQuantity": dec(Decimal(c.scrap_qty)),
        "ConfirmationReworkQuantity": dec(Decimal(c.rework_qty)),
        "IsFinalConfirmation": c.final,
        "OpConfirmedWorkQuantity1": c.setup_min,
        "OpWorkQuantityUnit1": "MIN",
        "OpConfirmedWorkQuantity2": c.machine_min,
        "OpWorkQuantityUnit2": "MIN",
        "OpConfirmedWorkQuantity3": c.labor_min,
        "OpWorkQuantityUnit3": "MIN",
        "ConfirmationText": c.text,
        "PostingDate": sap_date(date.fromisoformat(c.posting_date)),
        "EnteredByUser": c.entered_by,
        "ConfirmationEntryDateTime": sap_datetimeoffset(datetime.fromisoformat(c.entered_at)),
    }


def _matdoc_entity(m: MaterialDocument) -> dict:
    return {
        "__metadata": {"type": "API_MATERIAL_DOCUMENT_SRV.A_MaterialDocumentHeaderType"},
        "MaterialDocument": m.document,
        "MaterialDocumentYear": m.year,
        "GoodsMovementCode": m.movement_code,
        "PostingDate": sap_date(date.fromisoformat(m.posting_date)),
        "MaterialDocumentHeaderText": m.header_text,
        "to_MaterialDocumentItem": {"results": m.items},
    }
