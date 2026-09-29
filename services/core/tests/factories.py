from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from mes_common.events import EventEnvelope, Topics
from mes_core.modules.erp_sync.handlers import EVENT_WORK_ORDER_UPSERTED

_counter = iter(range(9000001, 9999999))


def material(number: str = "FG-1001", mtype: str = "FERT", serial: str | None = "ZMES01") -> dict:
    return {"number": number, "description": f"Material {number}", "base_uom": "PC", "material_type": mtype,
            "serial_profile": serial}


def work_order_payload(
    order: str | None = None,
    *,
    status: list[str] | None = None,
    changed_at: datetime | None = None,
    qty: str = "20",
    days_until_due: int = 5,
) -> dict:
    order = order or str(next(_counter))
    today = date.today()
    return {
        "erp_order_number": order,
        "erp_plant": "1000",
        "order_type": "PP01",
        "material": material(),
        "planned_qty": qty,
        "uom": "PC",
        "basic_start": today.isoformat(),
        "basic_end": (today + timedelta(days=days_until_due)).isoformat(),
        "erp_status": status or ["REL"],
        "erp_last_changed_at": (changed_at or datetime.now(UTC)).isoformat(),
        "delivered_qty": "0",
        "operations": [
            {"op_no": "0010", "description": "Turn", "work_center": "WC-TURN", "control_key": "PP01",
             "planned_qty": qty, "uom": "PC", "setup_min": "30", "run_min_per_unit": "4", "labor_min_per_unit": "1",
             "erp_routing_no": "0000000001", "erp_routing_counter": "00000001"},
            {"op_no": "0020", "description": "Mill", "work_center": "WC-MILL", "control_key": "PP01",
             "planned_qty": qty, "uom": "PC", "setup_min": "45", "run_min_per_unit": "6", "labor_min_per_unit": "1.5",
             "erp_routing_no": "0000000001", "erp_routing_counter": "00000002"},
        ],
        "components": [
            {"material": material("RM-AL-6061", "ROH", None), "op_no": "0010", "required_qty": "36", "uom": "KG",
             "reservation_no": f"R{order}", "reservation_item": "0001", "storage_location": "0001",
             "backflush": False, "withdrawn_qty": "0"},
        ],
    }


def envelope(payload: dict, event_type: str = EVENT_WORK_ORDER_UPSERTED) -> bytes:
    return EventEnvelope(
        event_type=event_type, source="erp-adapter", key=f"1000:{payload.get('erp_order_number', '')}", payload=payload
    ).model_dump_json().encode()


TOPIC = Topics.ERP_WORK_ORDERS


def assembly_order_payload(order: str | None = None, *, qty: str = "300", days_until_due: int = 5) -> dict:
    """FG-2001 on line L2: backflushed seals + an explicitly issued valve body (semi-finished)."""
    order = order or str(next(_counter))
    today = date.today()
    q = Decimal(qty)
    return {
        "erp_order_number": order,
        "erp_plant": "1000",
        "order_type": "PP01",
        "material": material("FG-2001"),
        "planned_qty": qty,
        "uom": "PC",
        "basic_start": today.isoformat(),
        "basic_end": (today + timedelta(days=days_until_due)).isoformat(),
        "erp_status": ["REL"],
        "erp_last_changed_at": datetime.now(UTC).isoformat(),
        "delivered_qty": "0",
        "operations": [
            {"op_no": "0010", "description": "Assemble valve", "work_center": "WC-ASSY", "control_key": "PP01",
             "planned_qty": qty, "uom": "PC", "setup_min": "15", "run_min_per_unit": "0.2", "labor_min_per_unit": "0.2",
             "erp_routing_no": "0000000009", "erp_routing_counter": "00000001"},
            {"op_no": "0020", "description": "Pressure test", "work_center": "WC-TEST", "control_key": "PP01",
             "planned_qty": qty, "uom": "PC", "setup_min": "10", "run_min_per_unit": "0.1", "labor_min_per_unit": "0.1",
             "erp_routing_no": "0000000009", "erp_routing_counter": "00000002"},
        ],
        "components": [
            {"material": material("SF-1003", "HALB", None), "op_no": "0010", "required_qty": str(q), "uom": "PC",
             "reservation_no": f"R{order}", "reservation_item": "0001", "storage_location": "0003",
             "backflush": False, "withdrawn_qty": "0"},
            {"material": material("CP-SEAL-12", "ROH", None), "op_no": "0010", "required_qty": str(q * 2), "uom": "PC",
             "reservation_no": f"R{order}", "reservation_item": "0002", "storage_location": "0003",
             "backflush": True, "withdrawn_qty": "0"},
        ],
    }
