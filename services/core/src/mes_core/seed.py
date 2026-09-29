"""Demo master data. Idempotent, in steps, so an existing database picks up later phases' data:

1. plant 1000 with two lines, five work centers, nine machines, shifts and user scopes
2. (Phase 2) terminals, operator PINs, storage locations, materials and opening stock

Run: `python -m mes_core.seed`.
Work centers carry the same codes as mock SAP (ERP is master for routings; MES owns the physical layout).
"""

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select

from mes_common.logs import setup_logging
from mes_core import models  # noqa: F401
from mes_core.modules.identity.models import AppUser, UserScope
from mes_core.modules.identity.terminal_auth import set_pin
from mes_core.modules.inventory.models import StorageLocation
from mes_core.modules.inventory.public import receive
from mes_core.modules.maintenance.models import MaintenancePlan
from mes_core.modules.masterdata.models import Material
from mes_core.modules.masterdata.public import upsert_material
from mes_core.modules.org.models import Area, Equipment, Line, Plant, ShiftPattern, Terminal, WorkCenter
from mes_core.modules.quality.models import InspectionPlan
from mes_core.modules.quality.service import CharacteristicInput, upsert_plan
from mes_core.modules.ticketing.models import SupportMember
from mes_core.modules.ticketing.public import SUPPORT_TEAM
from mes_core.settings import Settings, get_settings
from mes_core.shared.db.context import Actor
from mes_core.shared.db.session import Database

log = logging.getLogger("seed")

LAYOUT = {
    # area, line: [(work center code, name, [(machine code, name, type)])]
    ("MACH", "Machining", "L1", "Machining line 1"): [
        ("WC-TURN", "Turning", [("TURN-01", "CNC lathe 1", "CNC_LATHE"), ("TURN-02", "CNC lathe 2", "CNC_LATHE")]),
        ("WC-MILL", "Milling", [
            ("MILL-01", "5-axis mill 1", "CNC_MILL"),
            ("MILL-02", "5-axis mill 2", "CNC_MILL"),
            ("MILL-03", "3-axis mill", "CNC_MILL"),
        ]),
        ("WC-DEBURR", "Deburr and wash", [("DEB-01", "Deburr/wash cell", "WASHER")]),
    ],
    ("ASSY", "Assembly", "L2", "Assembly line 2"): [
        ("WC-ASSY", "Assembly", [("ASSY-01", "Assembly station 1", "ASSEMBLY"), ("ASSY-02", "Assembly station 2", "ASSEMBLY")]),
        ("WC-TEST", "Test", [("TEST-01", "Pressure/function tester", "TESTER")]),
    ],
}

DEMO_USERS = [
    ("operator1", "Olga Operator"),
    ("supervisor1", "Sam Supervisor"),
    ("quality1", "Quinn Quality"),
    ("maint1", "Max Maintenance"),
    ("admin1", "Ada Admin"),
    ("support1", "Sid Support"),  # support engineer (Keycloak role supervisor)
]


# Phase 2 --------------------------------------------------------------------------------------------------

# username, display name, PIN; operator2/3 exist only in MES (shop-floor staff without a Keycloak account)
PIN_USERS = [
    ("operator1", "Olga Operator", "1111"),
    ("operator2", "Oscar Operator", "2222"),
    ("operator3", "Omar Operator", "3333"),
    ("supervisor1", "Sam Supervisor", "4444"),
]

TERMINALS = [("T-L1", "Machining line terminal", "L1"), ("T-L2", "Assembly line terminal", "L2")]

# code, name, kind, line
LOCATIONS = [
    ("0001", "Raw material store", "STORE", None),
    ("0002", "Line-side L1 (machining)", "LINESIDE", "L1"),
    ("0003", "Line-side L2 (assembly)", "LINESIDE", "L2"),
    ("0020", "Semi-finished store", "SEMI", None),
    ("0010", "Finished goods store", "FG", None),
]

# Same catalogue as SAP (SAP stays master; the ERP sync overwrites descriptions if they differ)
MATERIALS = [
    ("RM-AL-6061", "Aluminium bar 6061 D80", "KG", "ROH", None),
    ("RM-ST-4140", "Steel bar 4140 D40", "KG", "ROH", None),
    ("CP-SEAL-12", "O-ring seal 12 mm", "PC", "ROH", None),
    ("CP-SPRING-8", "Compression spring 8 mm", "PC", "ROH", None),
    ("CP-BOLT-M6", "Hex bolt M6x20", "PC", "ROH", None),
    ("SF-1003", "Valve body VB-12", "PC", "HALB", None),
    ("FG-1001", "Pump housing PH-100", "PC", "FERT", "ZMES01"),
    ("FG-1002", "Drive shaft SH-20", "PC", "FERT", None),
    ("FG-2001", "Valve assembly VA-200", "PC", "FERT", "ZMES01"),
    ("FG-2002", "Actuator AC-50", "PC", "FERT", "ZMES01"),
]

# material, location, quantity (opening balance, movement 561: already in SAP, not posted back)
OPENING_STOCK = [
    ("RM-AL-6061", "0001", 4000), ("RM-AL-6061", "0002", 600),
    ("RM-ST-4140", "0001", 1500), ("RM-ST-4140", "0002", 250),
    ("CP-SEAL-12", "0001", 3000), ("CP-SEAL-12", "0003", 400),
    ("CP-SPRING-8", "0001", 2000), ("CP-SPRING-8", "0003", 200),
    ("CP-BOLT-M6", "0001", 8000), ("CP-BOLT-M6", "0003", 600),
    ("SF-1003", "0020", 60),
]


D = Decimal
QN, QL = "QUANTITATIVE", "QUALITATIVE"
# material, type, op_no, name, sample size, characteristics (name, kind, target, lower, upper, uom)
INSPECTION_PLANS = [
    ("FG-1001", "IN_PROCESS", "0020", "First-off: milled flange", 1, [
        ("Flange diameter", QN, D("120.00"), D("119.95"), D("120.05"), "mm"),
        ("Port thread G1/2 (go/no-go)", QL, None, None, None, None),
    ]),
    ("FG-1001", "FINAL", None, "Final inspection pump housing", 1, [
        ("Visual: no burrs, no scratches", QL, None, None, None, None),
        ("Overall length", QN, D("85.0"), D("84.9"), D("85.1"), "mm"),
    ]),
    ("SF-1003", "IN_PROCESS", "0020", "First-off: valve bores", 1, [
        ("Main bore diameter", QN, D("22.00"), D("22.00"), D("22.02"), "mm"),
    ]),
    ("FG-2001", "FINAL", None, "Final inspection valve assembly", 1, [
        ("Pressure test 10 bar, 60 s: no leak", QL, None, None, None, None),
    ]),
    ("CP-SEAL-12", "INCOMING", None, "Incoming: O-ring 12 mm", 2, [
        ("Inner diameter", QN, D("12.0"), D("11.9"), D("12.1"), "mm"),
        ("Surface free of cracks", QL, None, None, None, None),
    ]),
]


SPARE_PARTS = [
    ("SP-BEARING-6205", "Deep groove ball bearing 6205", 40),
    ("SP-BELT-A42", "V-belt A42", 12),
    ("SP-FILTER-HYD", "Hydraulic filter element 10 um", 20),
    ("SP-FILTER-WASH", "Washer filter bag 50 um", 30),
    ("SP-SEAL-KIT-CYL", "Cylinder seal kit D50", 8),
]
# machine, name, CALENDAR days | RUNTIME hours, lead days, duration min, checklist
MAINTENANCE_PLANS = [
    ("TURN-01", "Monthly lubrication and chip conveyor check", ("CALENDAR", 30), 3, 45,
     ["Check way-lube level, top up", "Clean and inspect chip conveyor", "Check coolant concentration (6-8 %)"]),
    ("TURN-02", "Monthly lubrication and chip conveyor check", ("CALENDAR", 30), 3, 45,
     ["Check way-lube level, top up", "Clean and inspect chip conveyor", "Check coolant concentration (6-8 %)"]),
    ("MILL-01", "Spindle and axis inspection (running hours)", ("RUNTIME", 40), 0, 90,
     ["Spindle run-out <= 5 um", "Check axis backlash", "Inspect tool changer grippers", "Replace hydraulic filter if dP > 2 bar"]),
    ("MILL-02", "Spindle and axis inspection (running hours)", ("RUNTIME", 40), 0, 90,
     ["Spindle run-out <= 5 um", "Check axis backlash", "Inspect tool changer grippers", "Replace hydraulic filter if dP > 2 bar"]),
    ("DEB-01", "Weekly washer filter change", ("CALENDAR", 7), 2, 30,
     ["Drain and clean tank strainer", "Replace filter bag", "Check spray nozzles"]),
    ("TEST-01", "Quarterly tester calibration check", ("CALENDAR", 90), 7, 60,
     ["Check reference pressure gauge", "Leak test with master part", "Record calibration values"]),
]
TECHNICIAN_PINS = [("maint1", "Max Maintenance", "5555")]
# Keycloak client-credentials account of the MES MCP server (role ai_agent); pre-created so it has a plant scope
MCP_SERVICE_USER = ("service-account-mes-mcp", "MES MCP agent")
# the AI support agent (support-agent service, Keycloak client mes-support-agent); its name is what ServiceNow
# work notes and the ticket activity show
SUPPORT_AGENT_USER = ("service-account-mes-support-agent", "AI Support Agent")


async def seed(db: Database, timezone: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    created = await seed_plant(db, timezone)
    created |= await seed_phase2(db, settings)
    created |= await seed_phase3(db)
    created |= await seed_phase4(db, settings)
    created |= await seed_phase5(db)
    created |= await seed_support_team(db)
    return created


async def seed_support_team(db: Database) -> bool:
    """The support engineers tickets can be assigned to (matched to ServiceNow users by e-mail)."""
    async with db.unit_of_work(Actor.service("seed"), reason="demo seed data (support team)") as s:
        plant = (await s.scalars(select(Plant).where(Plant.code == "P1000"))).one()
        if (await s.scalars(select(SupportMember).where(SupportMember.plant_id == plant.id))).first():
            log.info("support team already present")
            return False
        for username, name, email, tier in SUPPORT_TEAM:
            s.add(SupportMember(plant_id=plant.id, username=username, display_name=name, email=email, tier=tier,
                                active=True))
    log.info("seeded the support team (%d engineers)", len(SUPPORT_TEAM))
    return True


async def seed_phase5(db: Database) -> bool:
    async with db.unit_of_work(Actor.service("seed"), reason="demo seed data (phase 5)") as s:
        if (await s.scalars(select(AppUser).where(AppUser.username == SUPPORT_AGENT_USER[0]))).first():
            log.info("phase 5 seed data already present")
            return False
        plant = (await s.scalars(select(Plant).where(Plant.code == "P1000"))).one()
        agent = AppUser(username=SUPPORT_AGENT_USER[0], display_name=SUPPORT_AGENT_USER[1], active=True)
        s.add(agent)
        await s.flush()
        s.add(UserScope(user_id=agent.id, plant_id=plant.id, line_id=None))
    log.info("seeded phase 5 data: AI support agent service user")
    return True


async def seed_phase4(db: Database, settings: Settings) -> bool:
    async with db.unit_of_work(Actor.service("seed"), reason="demo seed data (phase 4)") as s:
        plant = (await s.scalars(select(Plant).where(Plant.code == "P1000"))).one()
        if (await s.scalars(select(MaintenancePlan).where(MaintenancePlan.plant_id == plant.id))).first():
            log.info("phase 4 seed data already present")
            return False
        store = (await s.scalars(select(StorageLocation).where(StorageLocation.plant_id == plant.id,
                                                               StorageLocation.code == "0001"))).one()
        for number, desc, qty in SPARE_PARTS:
            mat = await upsert_material(s, material_number=number, description=desc, base_uom="PC", material_type="ERSA",
                                        serial_profile=None)
            await receive(s, plant_id=plant.id, material_id=mat.id, quantity=Decimal(qty), location_id=store.id,
                          opening_balance=True, reason="opening balance")
        eqs = {e.code: e for e in (await s.scalars(select(Equipment).where(Equipment.plant_id == plant.id))).all()}
        for code, name, (itype, interval), lead, duration, checklist in MAINTENANCE_PLANS:
            s.add(MaintenancePlan(
                plant_id=plant.id, equipment_id=eqs[code].id, name=name, interval_type=itype,
                interval_days=interval if itype == "CALENDAR" else None,
                interval_hours=Decimal(interval) if itype == "RUNTIME" else None,
                lead_days=lead, duration_min=duration, checklist=checklist, active=True,
                # demo: the washer filter was last changed 6 days ago, so a preventive order is due right away
                last_done_at=datetime.now(UTC) - timedelta(days=6) if code == "DEB-01" else None,
            ))
        for username, name, pin in TECHNICIAN_PINS:
            user = (await s.scalars(select(AppUser).where(AppUser.username == username))).first()
            if user is None:
                user = AppUser(username=username, display_name=name, active=True)
                s.add(user)
                await s.flush()
                s.add(UserScope(user_id=user.id, plant_id=plant.id, line_id=None))
                await s.flush()
            await set_pin(s, settings, user_id=user.id, plant_id=plant.id, pin=pin, technician=True)
        if not (await s.scalars(select(AppUser).where(AppUser.username == MCP_SERVICE_USER[0]))).first():
            agent = AppUser(username=MCP_SERVICE_USER[0], display_name=MCP_SERVICE_USER[1], active=True)
            s.add(agent)
            await s.flush()
            s.add(UserScope(user_id=agent.id, plant_id=plant.id, line_id=None))
    log.info("seeded phase 4 data: spare parts, preventive plans, technician PIN, MCP service user")
    return True


async def seed_phase3(db: Database) -> bool:
    async with db.unit_of_work(Actor.service("seed"), reason="demo seed data (phase 3)") as s:
        plant = (await s.scalars(select(Plant).where(Plant.code == "P1000"))).one()
        if (await s.scalars(select(InspectionPlan).where(InspectionPlan.plant_id == plant.id))).first():
            log.info("phase 3 seed data already present")
            return False
        mats = {m.material_number: m for m in (await s.scalars(select(Material))).all()}
        for number, itype, op_no, name, sample, chars in INSPECTION_PLANS:
            await upsert_plan(
                s, plant_id=plant.id, material_id=mats[number].id, inspection_type=itype, op_no=op_no, name=name,
                sample_size=sample, blocking=True, active=True, chars=[CharacteristicInput(*c) for c in chars],
            )
    log.info("seeded phase 3 data: inspection plans")
    return True


async def seed_phase2(db: Database, settings: Settings) -> bool:
    async with db.unit_of_work(Actor.service("seed"), reason="demo seed data (phase 2)") as s:
        plant = (await s.scalars(select(Plant).where(Plant.code == "P1000"))).one()
        if (await s.scalars(select(Terminal).where(Terminal.plant_id == plant.id))).first():
            log.info("phase 2 seed data already present")
            return False
        lines = {ln.code: ln for ln in (await s.scalars(select(Line).where(Line.plant_id == plant.id))).all()}
        for code, name, line_code in TERMINALS:
            s.add(Terminal(plant_id=plant.id, line_id=lines[line_code].id, code=code, name=name))
        for username, name, pin in PIN_USERS:
            user = (await s.scalars(select(AppUser).where(AppUser.username == username))).first()
            if user is None:
                user = AppUser(username=username, display_name=name, active=True)
                s.add(user)
                await s.flush()
                s.add(UserScope(user_id=user.id, plant_id=plant.id, line_id=None))
                await s.flush()
            await set_pin(s, settings, user_id=user.id, plant_id=plant.id, pin=pin)
        locs = {}
        for code, name, kind, line_code in LOCATIONS:
            loc = StorageLocation(
                plant_id=plant.id, code=code, name=name, kind=kind, line_id=lines[line_code].id if line_code else None
            )
            s.add(loc)
            locs[code] = loc
        await s.flush()
        mats = {}
        for number, desc, uom, mtype, serial in MATERIALS:
            mats[number] = await upsert_material(
                s, material_number=number, description=desc, base_uom=uom, material_type=mtype, serial_profile=serial
            )
        for number, loc_code, qty in OPENING_STOCK:
            await receive(
                s, plant_id=plant.id, material_id=mats[number].id, quantity=Decimal(qty), location_id=locs[loc_code].id,
                opening_balance=True, reason="opening balance",
            )
    log.info("seeded phase 2 data: terminals, PINs, storage locations, opening stock")
    return True


async def seed_plant(db: Database, timezone: str) -> bool:
    async with db.unit_of_work(Actor.service("seed"), reason="demo seed data") as s:
        if (await s.scalars(select(Plant).where(Plant.code == "P1000"))).first():
            log.info("plant seed data already present")
            return False
        plant = Plant(code="P1000", name="Demo Plant 1000", timezone=timezone, erp_plant_code="1000")
        s.add(plant)
        await s.flush()
        lines = {}
        for (area_code, area_name, line_code, line_name), wcs in LAYOUT.items():
            area = Area(plant_id=plant.id, code=area_code, name=area_name)
            s.add(area)
            await s.flush()
            line = Line(plant_id=plant.id, area_id=area.id, code=line_code, name=line_name)
            s.add(line)
            await s.flush()
            lines[line_code] = line
            for wc_code, wc_name, machines in wcs:
                wc = WorkCenter(plant_id=plant.id, line_id=line.id, code=wc_code, name=wc_name, erp_work_center=wc_code)
                s.add(wc)
                await s.flush()
                for m_code, m_name, m_type in machines:
                    s.add(Equipment(plant_id=plant.id, line_id=line.id, work_center_id=wc.id, code=m_code,
                                    name=m_name, equipment_type=m_type))
        weekdays = [1, 2, 3, 4, 5]
        s.add_all([
            ShiftPattern(plant_id=plant.id, line_id=None, code="EARLY", name="Early shift",
                         start_time=time(6), end_time=time(14), days_of_week=weekdays),
            ShiftPattern(plant_id=plant.id, line_id=None, code="LATE", name="Late shift",
                         start_time=time(14), end_time=time(22), days_of_week=weekdays),
            ShiftPattern(plant_id=plant.id, line_id=lines["L1"].id, code="SAT", name="Saturday early (machining)",
                         start_time=time(6), end_time=time(14), days_of_week=[6]),
        ])
        for username, name in DEMO_USERS:
            user = AppUser(username=username, display_name=name, active=True)
            s.add(user)
            await s.flush()
            s.add(UserScope(user_id=user.id, plant_id=plant.id, line_id=None))
    log.info("seeded plant P1000 (%s)", timezone)
    return True


async def main() -> None:
    setup_logging("mes-core-seed")
    settings = get_settings()
    db = Database(settings.migration_database_url, pool_size=1)
    try:
        await seed(db, settings.seed_plant_timezone, settings)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
