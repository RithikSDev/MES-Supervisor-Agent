"""Read-only SAP access over the standard S/4HANA OData APIs (GET only, a technical user; the mock SAP in the demo).

Only named collections / entities can be read (no free URLs), so the tools can't reach anything else in SAP.
OData V2 envelopes and navigation stubs (__metadata, __deferred) are removed from the results.
"""

import time
from typing import Any
from urllib.parse import quote

import httpx

from data_mcp.settings import Settings

ROOT = "/sap/opu/odata/sap"
COLLECTIONS = {
    "production_orders": ("API_PRODUCTION_ORDER_2_SRV/A_ProductionOrder_2", "production orders (ManufacturingOrder, "
                          "Material, TotalQuantity, OrderIsReleased, OrderIsTechnicallyCompleted, basic dates, ...)"),
    "order_confirmations": ("API_PROD_ORDER_CONFIRMATION_2_SRV/ProdnOrdConf2", "confirmations posted to orders "
                            "(OrderID, OrderOperation, ConfirmationYieldQuantity, ConfirmationText = MES:<id>, ...)"),
    "material_documents": ("API_MATERIAL_DOCUMENT_SRV/A_MaterialDocumentHeader", "goods movements (header; expand "
                           "to_MaterialDocumentItem for items)"),
    "maintenance_orders": ("API_MAINTENANCEORDER/MaintenanceOrder", "PM orders (MaintenanceOrder, type, "
                           "TechnicalObject/Equipment, status)"),
}
ENTITIES = {
    "production_order": "API_PRODUCTION_ORDER_2_SRV/A_ProductionOrder_2('{key}')",
    "maintenance_order": "API_MAINTENANCEORDER/MaintenanceOrder('{key}')",
    "maintenance_notification": "API_MAINTNOTIFICATION/MaintenanceNotification('{key}')",
    "product": "API_PRODUCT_SRV/A_Product('{key}')",
}
EXPANDS = {"to_ProductionOrderOperation", "to_ProductionOrderComponent", "to_MaterialDocumentItem", "to_Description",
           "to_Plant"}


class SapError(Exception):
    pass


def _clean(v: Any) -> Any:
    if isinstance(v, dict):
        if set(v) == {"__deferred"}:
            return None
        if set(v) == {"results"}:
            return _clean(v["results"])
        return {k: c for k, val in v.items() if k != "__metadata" and (c := _clean(val)) is not None}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


class Sap:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self._http = http or httpx.AsyncClient(timeout=settings.sap_timeout_s)

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        started = time.monotonic()
        try:
            r = await self._http.get(f"{self.s.sap_base_url.rstrip('/')}{ROOT}/{path}",
                                     params={**params, "$format": "json"}, auth=(self.s.sap_user, self.s.sap_password))
        except httpx.HTTPError as e:
            raise SapError(f"SAP not reachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]["value"]
            except (ValueError, KeyError, TypeError):
                msg = r.text[:300]
            raise SapError(f"SAP answered {r.status_code}: {msg}")
        d = r.json().get("d", {})
        return _clean(d), int((time.monotonic() - started) * 1000)

    @staticmethod
    def _expand(expand: list[str] | None) -> dict[str, str]:
        bad = [e for e in expand or [] if e not in EXPANDS]
        if bad:
            raise SapError(f"unknown expand {bad}; allowed: {sorted(EXPANDS)}")
        return {"$expand": ",".join(expand)} if expand else {}

    async def query(self, collection: str, *, filter: str | None = None, select: str | None = None,
                    orderby: str | None = None, top: int = 20, expand: list[str] | None = None) -> dict[str, Any]:
        if collection not in COLLECTIONS:
            raise SapError(f"unknown collection; one of {sorted(COLLECTIONS)}")
        params = {"$top": str(max(1, min(top, 50))), "$inlinecount": "allpages", **self._expand(expand)}
        for key, value in (("$filter", filter), ("$select", select), ("$orderby", orderby)):
            if value:
                params[key] = value
        d, ms = await self._get(COLLECTIONS[collection][0], params)
        rows = d if isinstance(d, list) else d.get("results", d) if isinstance(d, dict) else []
        return {"collection": collection, "count": len(rows), "results": rows, "ms": ms}

    async def get(self, entity: str, key: str, expand: list[str] | None = None) -> Any:
        if entity not in ENTITIES:
            raise SapError(f"unknown entity; one of {sorted(ENTITIES)}")
        d, _ms = await self._get(ENTITIES[entity].format(key=quote(key.strip().replace("'", "''"), safe="")),
                                 self._expand(expand))
        return d

    async def aclose(self) -> None:
        await self._http.aclose()
