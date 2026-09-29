"""In-memory SAP state with optional JSON persistence, plus demo master data (plant 1000)."""

import json
import random
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

PLANT = "1000"


@dataclass
class Product:
    product: str
    description: str
    product_type: str  # ROH raw, HALB semi-finished, FERT finished
    base_unit: str
    serial_profile: str = ""


@dataclass
class RoutingStep:
    op: str
    text: str
    work_center: str
    setup_min: float
    machine_min_per_unit: float
    labor_min_per_unit: float


@dataclass
class BomItem:
    material: str
    qty_per_unit: float
    unit: str
    op: str
    backflush: bool = False


PRODUCTS: dict[str, Product] = {
    p.product: p
    for p in [
        Product("RM-AL-6061", "Aluminium bar 6061 D80", "ROH", "KG"),
        Product("RM-ST-4140", "Steel bar 4140 D40", "ROH", "KG"),
        Product("CP-SEAL-12", "O-ring seal 12 mm", "ROH", "PC"),
        Product("CP-SPRING-8", "Compression spring 8 mm", "ROH", "PC"),
        Product("CP-BOLT-M6", "Hex bolt M6x20", "ROH", "PC"),
        Product("SF-1003", "Valve body VB-12", "HALB", "PC"),
        Product("FG-1001", "Pump housing PH-100", "FERT", "PC", "ZMES01"),
        Product("FG-1002", "Drive shaft SH-20", "FERT", "PC"),
        Product("FG-2001", "Valve assembly VA-200", "FERT", "PC", "ZMES01"),
        Product("FG-2002", "Actuator AC-50", "FERT", "PC", "ZMES01"),
        # spare parts (plant maintenance)
        Product("SP-BEARING-6205", "Deep groove ball bearing 6205", "ERSA", "PC"),
        Product("SP-BELT-A42", "V-belt A42", "ERSA", "PC"),
        Product("SP-FILTER-HYD", "Hydraulic filter element 10 um", "ERSA", "PC"),
        Product("SP-FILTER-WASH", "Washer filter bag 50 um", "ERSA", "PC"),
        Product("SP-SEAL-KIT-CYL", "Cylinder seal kit D50", "ERSA", "PC"),
    ]
}

ROUTINGS: dict[str, list[RoutingStep]] = {
    "FG-1001": [
        RoutingStep("0010", "Turn housing blank", "WC-TURN", 30, 4.0, 1.0),
        RoutingStep("0020", "Mill ports and flange", "WC-MILL", 45, 6.0, 1.5),
        RoutingStep("0030", "Deburr and wash", "WC-DEBURR", 10, 1.5, 1.5),
    ],
    "FG-1002": [
        RoutingStep("0010", "Turn shaft", "WC-TURN", 20, 3.0, 0.5),
        RoutingStep("0020", "Mill keyway", "WC-MILL", 25, 2.5, 0.5),
    ],
    "SF-1003": [
        RoutingStep("0010", "Turn valve body", "WC-TURN", 25, 2.5, 0.5),
        RoutingStep("0020", "Drill and mill bores", "WC-MILL", 30, 3.5, 0.8),
        RoutingStep("0030", "Deburr", "WC-DEBURR", 10, 1.0, 1.0),
    ],
    "FG-2001": [
        RoutingStep("0010", "Assemble valve", "WC-ASSY", 15, 4.0, 4.0),
        RoutingStep("0020", "Pressure test", "WC-TEST", 10, 2.0, 2.0),
    ],
    "FG-2002": [
        RoutingStep("0010", "Pre-assembly", "WC-ASSY", 15, 3.0, 3.0),
        RoutingStep("0020", "Final assembly", "WC-ASSY", 10, 5.0, 5.0),
        RoutingStep("0030", "Function test", "WC-TEST", 10, 2.5, 2.5),
    ],
}

BOMS: dict[str, list[BomItem]] = {
    "FG-1001": [BomItem("RM-AL-6061", 1.8, "KG", "0010")],
    "FG-1002": [BomItem("RM-ST-4140", 0.9, "KG", "0010")],
    "SF-1003": [BomItem("RM-AL-6061", 0.6, "KG", "0010")],
    "FG-2001": [
        BomItem("SF-1003", 1, "PC", "0010"),
        BomItem("CP-SEAL-12", 2, "PC", "0010", backflush=True),
        BomItem("CP-SPRING-8", 1, "PC", "0010", backflush=True),
    ],
    "FG-2002": [
        BomItem("CP-BOLT-M6", 4, "PC", "0020", backflush=True),
        BomItem("CP-SEAL-12", 1, "PC", "0010", backflush=True),
    ],
}

ORDER_MIX = [("FG-1001", 3), ("FG-1002", 3), ("SF-1003", 2), ("FG-2001", 3), ("FG-2002", 2)]


@dataclass
class Operation:
    op: str
    text: str
    work_center: str
    routing_no: str
    routing_counter: str
    planned_qty: str
    setup_min: float
    machine_min_per_unit: float
    labor_min_per_unit: float
    confirmed_yield: str = "0"
    confirmed_scrap: str = "0"
    final_confirmed: bool = False


@dataclass
class Component:
    reservation: str
    reservation_item: str
    material: str
    op: str
    required_qty: str
    unit: str
    backflush: bool
    withdrawn_qty: str = "0"


@dataclass
class ProductionOrder:
    order: str
    order_type: str
    material: str
    plant: str
    planned_qty: str
    unit: str
    basic_start: str  # ISO date
    basic_end: str
    released: bool
    technically_completed: bool
    deletion_flag: bool
    last_change: str  # ISO datetime UTC
    operations: list[Operation] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    delivered_qty: str = "0"

    def touch(self) -> None:
        self.last_change = now_utc().isoformat()

    @property
    def confirmed(self) -> bool:
        return bool(self.operations) and all(o.final_confirmed for o in self.operations)

    @property
    def partially_confirmed(self) -> bool:
        return not self.confirmed and any(
            Decimal(o.confirmed_yield) > 0 or Decimal(o.confirmed_scrap) > 0 for o in self.operations
        )


@dataclass
class Confirmation:
    group: str
    count: str
    order: str
    op: str
    work_center: str
    plant: str
    yield_qty: str
    scrap_qty: str
    rework_qty: str
    unit: str
    final: bool
    setup_min: str
    machine_min: str
    labor_min: str
    text: str
    posting_date: str
    entered_at: str
    entered_by: str


@dataclass
class MaterialDocument:
    document: str
    year: str
    movement_code: str
    posting_date: str
    header_text: str
    entered_at: str
    items: list[dict] = field(default_factory=list)


@dataclass
class MaintNotification:
    """PM notification (API_MAINTNOTIFICATION). Numbers come from MES (external number range)."""

    number: str
    notif_type: str  # M1 request, M2 breakdown
    priority: str
    text: str
    long_text: str
    equipment: str
    breakdown: bool
    malfunction_start: str
    reported_by: str
    status: str  # OSNO outstanding, NOPR in process, NOCO completed
    created_at: str
    last_change: str


@dataclass
class MaintOrder:
    """PM order (API_MAINTENANCEORDER). Numbers come from MES (external number range)."""

    number: str
    order_type: str  # PM01 corrective, PM02 preventive
    description: str
    equipment: str
    work_center: str | None
    notification: str | None
    priority: str
    planned_start: str
    duration_min: int
    system_status: str  # CRTD REL TECO
    labor_min: str | None
    findings: str | None
    created_at: str
    last_change: str
    history: list[dict] = field(default_factory=list)


@dataclass
class Faults:
    latency_ms: int = 0
    fail_next_503: int = 0
    fail_next_lock: int = 0
    commit_then_timeout_next: int = 0
    hang_seconds: float = 15.0
    # which postings the commit-then-timeout fault hits: "" = any, or confirmation / material_document / pm
    fault_scope: str = ""


def now_utc() -> datetime:
    # Millisecond precision: OData /Date()/ literals carry millis, so round-trips compare equal.
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


class Store:
    def __init__(self, state_file: str | None = None) -> None:
        self.lock = threading.RLock()
        self._file = Path(state_file) if state_file else None
        self.orders: dict[str, ProductionOrder] = {}
        self.confirmations: list[Confirmation] = []
        self.material_documents: list[MaterialDocument] = []
        self.maint_notifications: dict[str, MaintNotification] = {}
        self.maint_orders: dict[str, MaintOrder] = {}
        self.faults = Faults()
        self.next_order = 1000001
        self.next_conf_group = 100001
        self.next_matdoc = 4900000001
        self.next_reservation = 5000001
        self.csrf_tokens: set[str] = set()

    # ------------------------------------------------------------ persistence
    def load(self) -> bool:
        if not self._file or not self._file.exists():
            return False
        raw = json.loads(self._file.read_text())
        self.orders = {}
        for o in raw["orders"]:
            ops = [Operation(**x) for x in o.pop("operations")]
            comps = [Component(**x) for x in o.pop("components")]
            self.orders[o["order"]] = ProductionOrder(**o, operations=ops, components=comps)
        self.confirmations = [Confirmation(**c) for c in raw["confirmations"]]
        self.material_documents = [MaterialDocument(**m) for m in raw.get("material_documents", [])]
        self.maint_notifications = {n["number"]: MaintNotification(**n) for n in raw.get("maint_notifications", [])}
        self.maint_orders = {o["number"]: MaintOrder(**o) for o in raw.get("maint_orders", [])}
        self.next_order = raw["next_order"]
        self.next_conf_group = raw["next_conf_group"]
        self.next_matdoc = raw.get("next_matdoc", self.next_matdoc)
        self.next_reservation = raw.get("next_reservation", self.next_reservation)
        return True

    def save(self) -> None:
        if not self._file:
            return
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "orders": [asdict(o) for o in self.orders.values()],
                    "confirmations": [asdict(c) for c in self.confirmations],
                    "material_documents": [asdict(m) for m in self.material_documents],
                    "maint_notifications": [asdict(n) for n in self.maint_notifications.values()],
                    "maint_orders": [asdict(o) for o in self.maint_orders.values()],
                    "next_order": self.next_order,
                    "next_conf_group": self.next_conf_group,
                    "next_matdoc": self.next_matdoc,
                    "next_reservation": self.next_reservation,
                }
            )
        )
        tmp.replace(self._file)

    def reset(self) -> None:
        with self.lock:
            self.__init__(str(self._file) if self._file else None)  # type: ignore[misc]
            self.save()

    # ------------------------------------------------------------ orders
    def generate_orders(self, count: int, release_ratio: float = 0.8, rng: random.Random | None = None) -> list[str]:
        rng = rng or random.Random()
        materials = [m for m, w in ORDER_MIX for _ in range(w)]
        created = []
        with self.lock:
            for _ in range(count):
                mat = rng.choice(materials)
                qty = Decimal(rng.choice(range(10, 61, 5)))
                start = date.today() + timedelta(days=rng.randint(0, 5))
                end = start + timedelta(days=rng.randint(2, 6))
                order_no = str(self.next_order)
                self.next_order += 1
                routing_no = f"{self.next_order:010d}"
                reservation = f"{self.next_reservation:010d}"
                self.next_reservation += 1
                po = ProductionOrder(
                    order=order_no,
                    order_type="PP01",
                    material=mat,
                    plant=PLANT,
                    planned_qty=str(qty),
                    unit=PRODUCTS[mat].base_unit,
                    basic_start=start.isoformat(),
                    basic_end=end.isoformat(),
                    released=rng.random() < release_ratio,
                    technically_completed=False,
                    deletion_flag=False,
                    last_change=now_utc().isoformat(),
                )
                for i, step in enumerate(ROUTINGS[mat], start=1):
                    po.operations.append(
                        Operation(
                            op=step.op,
                            text=step.text,
                            work_center=step.work_center,
                            routing_no=routing_no,
                            routing_counter=f"{i:08d}",
                            planned_qty=str(qty),
                            setup_min=step.setup_min,
                            machine_min_per_unit=step.machine_min_per_unit,
                            labor_min_per_unit=step.labor_min_per_unit,
                        )
                    )
                for i, item in enumerate(BOMS[mat], start=1):
                    po.components.append(
                        Component(
                            reservation=reservation,
                            reservation_item=f"{i:04d}",
                            material=item.material,
                            op=item.op,
                            required_qty=str((Decimal(str(item.qty_per_unit)) * qty).quantize(Decimal("0.001"))),
                            unit=item.unit,
                            backflush=item.backflush,
                        )
                    )
                self.orders[order_no] = po
                created.append(order_no)
            self.save()
        return created
