"""Demo support tickets: eight weeks of backdated history plus a set of open tickets "right now".

History has a realistic shape so forecasts and SLA reports have something to work with: weekdays busier than
weekends, a slowly rising weekly volume, one SAP-interface outage day, most targets met and some missed, severity
changes, escalations (a few to L3 / Jira) and assignees from the support team. The open tickets cover P1-P4, every
status, breached / at-risk clocks and different assignees.

Every ticket is published once (mes.ticket.created, in its final state, flagged `demo`), so the itsm-adapter creates
the ServiceNow incident (and Jira issue at L3) already in that state. The AI agent ignores demo tickets (hand one to
it with "Run the AI again"), and the notifier ignores history. Demo tickets are recognised by reported_by
`demo:<user>`; loading twice is refused.
"""

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.org.public import get_plant, list_equipment
from mes_core.modules.ticketing import domain
from mes_core.modules.ticketing.models import TICKET_SEQ, SupportMember, Ticket, TicketActivity
from mes_core.modules.ticketing.service import EVENT_TICKET_CREATED, _emit
from mes_core.shared.errors import DomainError

# (username, name, e-mail, usual tier; None = any)
SUPPORT_TEAM = [
    ("l1.priya", "Priya Nair", "priya.nair@mes.local", "L1"),
    ("l1.tom", "Tom Becker", "tom.becker@mes.local", "L1"),
    ("l2.wei", "Wei Chen", "wei.chen@mes.local", "L2"),
    ("l2.maria", "Maria Rossi", "maria.rossi@mes.local", "L2"),
    ("l3.arjun", "Arjun Rao", "arjun.rao@mes.local", "L3"),
    ("supervisor1", "Sam Supervisor", "supervisor1@mes.local", "L2"),
    ("admin1", "Ada Admin", "admin1@mes.local", None),
    ("support1", "Sid Support", "support1@mes.local", None),
]
REPORTERS = ["Olga Operator", "Oscar Operator", "Omar Operator", "Sam Supervisor", "Quinn Quality", "Max Maintenance"]


@dataclass(frozen=True)
class Tpl:
    category: str
    impact: str
    title: str
    description: str
    resolution: str
    machine: str | None = None


TEMPLATES: list[tuple[float, Tpl]] = [
    (5, Tpl("MES_APP", "DEGRADED", "Terminal freezes when confirming quantity",
            "The line terminal hangs for about a minute after pressing Confirm; operators re-enter the quantity.",
            "Stale browser cache on the kiosk after the last UI release; cleared and kiosk restarted.")),
    (4, Tpl("MES_APP", "MINOR", "Work queue shows finished operation",
            "An operation that was confirmed yesterday still appears in the machine's work queue.",
            "Queue view cached an old state; refreshed and confirmed the operation status is correct.")),
    (3, Tpl("MES_APP", "LINE_STOP", "Terminal cannot start runs on {m}",
            "Start run is refused with 'machine not available' although the machine is running fine.",
            "Machine status was left DOWN after a cleared breakdown; reported back up and runs start again.", "machine")),
    (3, Tpl("MES_APP", "DEGRADED", "Operator logged out repeatedly on {t}",
            "The terminal session ends every few minutes; operators have to enter the PIN again and again.",
            "A second tablet was enrolled with the same terminal code; the stray device was removed.")),
    (4, Tpl("HARDWARE", "MINOR", "Label printer prints blank labels",
            "The Zebra printer at the packing station feeds labels but prints nothing on them.",
            "Ribbon was inserted the wrong way round after a ribbon change; re-threaded and test print OK.")),
    (3, Tpl("HARDWARE", "DEGRADED", "Barcode scanner not reading lot labels on {m}",
            "The hand scanner beeps but the lot number field stays empty; lots are typed in by hand.",
            "Scanner had lost its keyboard-wedge configuration; reset to the plant profile.", "machine")),
    (2, Tpl("HARDWARE", "LINE_STOP", "Terminal touch screen dead on {t}",
            "The terminal screen shows the app but does not react to touch; no confirmations possible.",
            "Touch controller USB cable loose; reseated and fixed with a cable clip.")),
    (4, Tpl("INTEGRATION", "DEGRADED", "Confirmations not reaching SAP",
            "Confirmations are stuck in the SAP queue with timeouts; order progress in SAP is behind.",
            "SAP gateway was overloaded during month-end; queue drained automatically after the retry, no duplicates.")),
    (3, Tpl("INTEGRATION", "MINOR", "Order changed in SAP not updated in MES",
            "Planner changed the order quantity in SAP two hours ago; MES still shows the old quantity.",
            "Order re-read from SAP; the change poller had skipped it during a restart.")),
    (2, Tpl("INTEGRATION", "DEGRADED", "Goods movement rejected by SAP (deficit of stock)",
            "Backflush posting for component was rejected by SAP with a stock deficit message.",
            "Stock transfer to the line location was missing in SAP; posted by the store, retry succeeded.")),
    (3, Tpl("DATA", "MINOR", "Lot stuck in quality inspection",
            "A released lot still shows as 'in inspection' and cannot be consumed.",
            "Inspection lot usage decision was saved without closing the lot; closed with the quality engineer.")),
    (3, Tpl("DATA", "DEGRADED", "Wrong good quantity on order after confirmation",
            "Good quantity doubled after an operator confirmed twice while the terminal was offline.",
            "Duplicate offline confirmation reversed; client event dedup confirmed for this terminal.")),
    (5, Tpl("ACCESS", "SERVICE_REQUEST", "New operator needs terminal PIN",
            "New starter on the late shift needs a PIN for the assembly line terminal.",
            "PIN issued and tested at the terminal with the supervisor.")),
    (3, Tpl("ACCESS", "MINOR", "PIN locked after failed attempts",
            "Operator's PIN is locked after three wrong entries at the start of the shift.",
            "PIN reset by the supervisor after identity check.")),
    (3, Tpl("CHANGE_REQUEST", "SERVICE_REQUEST", "Add scrap reason 'tool breakage'",
            "Quality asks for a new scrap reason so tool breakages can be reported separately.",
            "Scrap reason added to master data and available on the terminals.")),
    (2, Tpl("CHANGE_REQUEST", "SERVICE_REQUEST", "Shift report should include downtime minutes",
            "Supervisors want the downtime minutes per machine on the end-of-shift report.",
            "Report updated and released after review with the supervisors.")),
    (1, Tpl("OTHER", "MINOR", "Question about rework booking",
            "Operator unsure how to book a reworked part back into the order.",
            "Explained the rework flow on the terminal; MES user guide link shared.")),
]
OUTAGE = Tpl("INTEGRATION", "DEGRADED", "SAP interface down: postings failing",
             "All confirmations and goods movements fail with connection errors to the SAP gateway.",
             "SAP gateway certificate expired; renewed by basis, queued postings sent after the retry without duplicates.")

# weekday weights (Mon..Sun): busier early in the week, very little at the weekend
WEEKDAY = [1.2, 1.3, 1.2, 1.1, 1.0, 0.3, 0.1]
# when the open tickets below were reported (hours ago): history leaves room for them on those days, so the last
# week's volume stays on the trend
CURRENT_AGO_H = [3.17, 0.13, 5.08, 7, 1.08, 0.83, 4, 1.5, 9, 30, 6, 50, 26]


def _poisson(rng: random.Random, lam: float) -> int:
    n, p, limit = 0, 1.0, pow(2.718281828, -lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return n
        n += 1


class _Builder:
    def __init__(self, s: AsyncSession, plant_id: UUID, now: datetime, rng: random.Random,
                 team: dict[str, SupportMember], machines: dict[str, Any]) -> None:
        self.s, self.plant_id, self.now, self.rng, self.team, self.machines = s, plant_id, now, rng, team, machines
        self.tickets: list[tuple[Ticket, str]] = []

    def member(self, tier: str) -> SupportMember:
        pool = [m for m in self.team.values() if m.tier == tier]
        return self.rng.choice(pool)

    async def new(self, tpl: Tpl, at: datetime, *, severity: str | None = None, reporter: str | None = None,
                  machine: str | None = None) -> Ticket:
        rng = self.rng
        m_code = machine or (rng.choice(list(self.machines)) if tpl.machine or "{m}" in tpl.title else None)
        term = "T-L1" if not m_code or self.machines[m_code].line_code == "L1" else "T-L2"
        title = tpl.title.format(m=m_code or "", t=term)
        suggested = domain.suggest_severity(tpl.impact, tpl.category)
        sev = severity or suggested
        seq = (await self.s.execute(select(TICKET_SEQ.next_value()))).scalar_one()
        resp_due, fix_due = domain.due_dates(sev, at)
        eq = self.machines.get(m_code) if m_code else None
        name = reporter or rng.choice(REPORTERS)
        t = Ticket(plant_id=self.plant_id, line_id=eq.line_id if eq else None, number=f"MES-{seq:06d}", title=title,
                   description=tpl.description, category=tpl.category, impact=tpl.impact, severity=sev,
                   suggested_severity=suggested, tier="L1", status="NEW",
                   source="TERMINAL" if tpl.category in ("MES_APP", "HARDWARE") else "ADMIN",
                   reported_at=at, reported_by=f"demo:{name.split()[0].lower()}", reported_by_name=name,
                   equipment_id=eq.id if eq else None, response_due_at=resp_due, resolution_due_at=fix_due, held_s=0,
                   itsm_sync_status="PENDING")
        self.s.add(t)
        await self.s.flush()
        self.act(t, at, "CREATED", f"severity {sev}" + (f" (suggested {suggested})" if sev != suggested else "") + ", tier L1",
                 name, data={"severity": sev, "suggested": suggested})
        return t

    def act(self, t: Ticket, at: datetime, kind: str, text: str | None, actor: str | None, *, origin: str = "MES",
            data: dict[str, Any] | None = None) -> None:
        self.s.add(TicketActivity(plant_id=t.plant_id, ticket_id=t.id, at=at, kind=kind, origin=origin,
                                  actor_name=actor, text=text, data=data))

    def assign(self, t: Ticket, m: SupportMember | None, at: datetime, by: str) -> None:
        old = t.assignee_name
        t.assignee_id, t.assignee_name, t.assigned_at = (m.id, m.display_name, at) if m else (None, None, None)
        self.act(t, at, "ASSIGNED", (f"Assigned to {m.display_name}" if m else "Unassigned") + (f" (was {old})" if old else ""),
                 by, data={"from": old, "to": t.assignee_name})

    def status(self, t: Ticket, at: datetime, target: str, by: str, note: str | None = None) -> None:
        old = t.status
        if target in ("ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD", "RESOLVED") and t.acknowledged_at is None:
            t.acknowledged_at = at
        if t.status == "ON_HOLD" and t.on_hold_since:
            t.held_s += int((at - t.on_hold_since).total_seconds())
            t.on_hold_since = None
        if target == "ON_HOLD":
            t.on_hold_since = at
        if target == "RESOLVED":
            t.resolved_at, t.resolution_note = at, note
        if target in ("CLOSED", "CANCELLED"):
            t.closed_at = at
        t.status = target
        self.act(t, at, "STATUS", note, by, data={"from": old, "to": target})

    def severity(self, t: Ticket, at: datetime, sev: str, by: str, reason: str) -> None:
        old = t.severity
        t.severity = sev
        t.response_due_at, t.resolution_due_at = domain.due_dates(sev, t.reported_at)
        self.act(t, at, "SEVERITY", reason, by, data={"from": old, "to": sev})

    def escalate(self, t: Ticket, at: datetime, by: str, reason: str) -> None:
        old = t.tier
        t.tier = domain.next_tier(t.tier) or t.tier
        was = t.assignee_name
        t.assignee_id, t.assignee_name, t.assigned_at = None, None, None
        if t.status == "NEW":
            self.status(t, at, "IN_PROGRESS", by)
        self.act(t, at, "ESCALATED", reason, by, data={"from": old, "to": t.tier, **({"unassigned": was} if was else {})})

    def breaches(self, t: Ticket) -> None:
        """SLA flags and activities as the worker would have set them, up to now."""
        for which, due, met in (("response", t.response_due_at, t.acknowledged_at),
                                ("resolution", t.resolution_due_at, t.resolved_at)):
            if due is None:
                continue
            due = due + timedelta(seconds=t.held_s) if which == "resolution" else due
            if (met or self.now) > due and due <= self.now and t.status != "CANCELLED":
                setattr(t, f"{which}_breached", True)
                setattr(t, f"{which}_warned", True)
                self.act(t, due, "SLA_BREACH", f"{which} target {t.severity} missed (due {due:%d %b %H:%M} UTC)",
                         None, origin="SYSTEM")

    # ------------------------------------------------------------------ history
    async def history_ticket(self, tpl: Tpl, at: datetime, *, force_l3: bool = False) -> None:
        rng = self.rng
        t = await self.new(tpl, at)
        if rng.random() < 0.03 and not force_l3:  # raised by mistake / duplicate
            self.status(t, at + timedelta(minutes=rng.randint(10, 90)), "CANCELLED", self.member("L1").display_name,
                        "Duplicate of an earlier ticket / raised by mistake")
            self.tickets.append((t, "history"))
            return
        resp, fix = domain.SEVERITY_TARGETS[t.severity]
        l1 = self.member("L1")
        late = rng.random() < 0.14
        ack = at + (resp * rng.uniform(1.2, 2.5) if late else resp * rng.uniform(0.15, 0.9))
        self.assign(t, l1, ack - timedelta(minutes=1), l1.display_name)
        self.status(t, ack, "ACKNOWLEDGED", l1.display_name)
        cursor = ack + timedelta(minutes=rng.randint(3, 30))
        self.status(t, cursor, "IN_PROGRESS", l1.display_name)
        if rng.random() < 0.10 and t.severity in ("P2", "P3"):
            up = rng.random() < 0.6
            new = ("P1" if t.severity == "P2" else "P2") if up else ("P3" if t.severity == "P2" else "P4")
            self.severity(t, cursor + timedelta(minutes=5), new, l1.display_name,
                          "More machines affected than first reported" if up else "Workaround in place, production continues")
            resp, fix = domain.SEVERITY_TARGETS[t.severity]
        owner = l1
        if force_l3 or rng.random() < 0.2 or t.category in ("INTEGRATION", "DATA") and rng.random() < 0.4:
            cursor += timedelta(minutes=rng.randint(15, 90))
            self.escalate(t, cursor, l1.display_name, "Needs technical analysis (logs / database) beyond L1 checks")
            owner = self.member("L2")
            self.assign(t, owner, cursor + timedelta(minutes=5), owner.display_name)
            if force_l3:
                cursor += timedelta(hours=rng.uniform(1, 3))
                self.escalate(t, cursor, owner.display_name, "Root cause is a code defect; needs a fix by development")
                owner = self.team["l3.arjun"]
                self.assign(t, owner, cursor + timedelta(minutes=10), owner.display_name)
        if fix is None:  # P4: scheduled work, done within a few days
            done = at + timedelta(hours=rng.uniform(4, 96))
        else:
            missed = rng.random() < (0.22 if t.severity in ("P1", "P2") else 0.12) or force_l3
            done = at + (fix * rng.uniform(1.1, 2.2) if missed else fix * rng.uniform(0.3, 0.95))
        done = max(done, cursor + timedelta(minutes=10))
        if tpl.category == "INTEGRATION" and rng.random() < 0.25:
            hold = cursor + (done - cursor) / 3
            self.status(t, hold, "ON_HOLD", owner.display_name, "Waiting for SAP basis")
            back = hold + timedelta(minutes=rng.randint(20, 120))
            self.status(t, back, "IN_PROGRESS", owner.display_name)
            done = max(done, back + timedelta(minutes=15)) + (back - hold)
        if done >= self.now - timedelta(minutes=30):
            done = self.now - timedelta(minutes=rng.randint(30, 120))
        self.status(t, done, "RESOLVED", owner.display_name, tpl.resolution)
        if done < self.now - timedelta(days=3):
            self.status(t, done + timedelta(days=3), "CLOSED", "MES (automatic)", "Closed 3 days after resolution")
        self.breaches(t)
        self.tickets.append((t, "history"))


async def load_demo_tickets(s: AsyncSession, plant_id: UUID, *, weeks: int = 8, seed: int = 20260928) -> dict[str, Any]:
    if (await s.scalars(select(Ticket.id).where(Ticket.plant_id == plant_id, Ticket.reported_by.like("demo:%"))
                        .limit(1))).first():
        raise DomainError("demo tickets are already loaded (reset the demo data first to load them again)",
                          code="demo_loaded")
    team = {m.username: m for m in (await s.scalars(select(SupportMember).where(SupportMember.plant_id == plant_id))).all()}
    missing = [u for u, *_ in SUPPORT_TEAM if u not in team]
    for username, name, email, tier in SUPPORT_TEAM:
        if username in missing:
            team[username] = SupportMember(plant_id=plant_id, username=username, display_name=name, email=email,
                                           tier=tier, active=True)
            s.add(team[username])
    await s.flush()
    machines = {e.code: e for e in await list_equipment(s, plant_id)}
    now = datetime.now(UTC).replace(microsecond=0)
    rng = random.Random(seed)
    b = _Builder(s, plant_id, now, rng, team, machines)

    # ---- history: `weeks` full weeks up to yesterday, in plant days (shift hours are local)
    tz = (await get_plant(s, plant_id)).tz
    today = now.astimezone(tz).replace(hour=0, minute=0, second=0)
    first = today - timedelta(days=7 * weeks)
    weights, tpls = zip(*TEMPLATES, strict=True)
    l3_days = {first + timedelta(days=d) for d in (9, 30, 47)}
    l3_due = 0  # L3 escalations still to place (on the next ticket from their day on)
    outage_day = first + timedelta(days=7 * (weeks - 3) + 1)  # a Tuesday-ish three weeks ago
    for i in range(7 * weeks):
        day = first + timedelta(days=i)
        week_level = 11 + 1.0 * (i // 7)  # tickets per week, slowly rising
        taken = sum(1 for h in CURRENT_AGO_H if (now - timedelta(hours=h)).astimezone(tz).date() == day.date())
        lam = max(0.0, week_level * WEEKDAY[day.weekday()] / sum(WEEKDAY) - taken)
        times = sorted((day + timedelta(hours=rng.uniform(6, 22))).astimezone(UTC) for _ in range(_poisson(rng, lam)))
        l3_due += day in l3_days
        for at in times:
            await b.history_ticket(rng.choices(tpls, weights)[0], at, force_l3=l3_due > 0)
            l3_due = max(0, l3_due - 1)
        if day == outage_day:
            for k in range(6):
                await b.history_ticket(OUTAGE, (day + timedelta(hours=9, minutes=12 * k)).astimezone(UTC))

    # ---- open tickets right now
    by = team

    def ago(**kw: float) -> datetime:
        return now - timedelta(**kw)

    async def current(tpl: Tpl, at: datetime, *, machine: str | None = None, severity: str | None = None) -> Ticket:
        t = await b.new(tpl, at, machine=machine, severity=severity)
        b.tickets.append((t, "current"))
        return t

    t = await current(Tpl("MES_APP", "LINE_STOP", "Line 1 terminal cannot confirm operations on MILL-01",
                          "Since the shift start every confirmation on MILL-01 fails with an error; the machine keeps "
                          "running but no quantities are booked. Operators write counts on paper.", ""),
                      ago(hours=3, minutes=10), machine="MILL-01")
    b.assign(t, by["l1.priya"], ago(hours=3, minutes=2), "Priya Nair")
    b.status(t, ago(hours=3), "ACKNOWLEDGED", "Priya Nair")
    b.escalate(t, ago(hours=2, minutes=20), "Priya Nair", "Terminal and network fine; error comes from the confirmation "
               "service. Needs log analysis.")
    b.assign(t, by["l2.wei"], ago(hours=2, minutes=15), "Wei Chen")

    t = await current(Tpl("HARDWARE", "LINE_STOP", "Assembly station ASSY-02 terminal screen black",
                          "Terminal at ASSY-02 shows a black screen after a power dip; the station cannot start runs.", ""),
                      ago(minutes=8), machine="ASSY-02")

    t = await current(Tpl("INTEGRATION", "DEGRADED", "Goods receipts for line 2 orders not posted to SAP",
                          "Last-operation goods receipts from line 2 are waiting in the SAP queue with timeouts since "
                          "this morning; SAP stock is behind.", ""), ago(hours=5, minutes=5))
    b.assign(t, by["l1.priya"], ago(hours=4, minutes=58), "Priya Nair")
    b.status(t, ago(hours=4, minutes=55), "IN_PROGRESS", "Priya Nair")

    t = await current(Tpl("DATA", "DEGRADED", "Serial numbers missing on finished pumps",
                          "Goods receipt booked 40 pumps but only 32 serial numbers were assigned.", ""),
                      ago(hours=7), severity="P2")
    b.assign(t, by["l1.tom"], ago(hours=6, minutes=50), "Tom Becker")
    b.status(t, ago(hours=6, minutes=50), "ACKNOWLEDGED", "Tom Becker")
    b.escalate(t, ago(hours=6), "Tom Becker", "Serial assignment data problem; needs DB analysis")
    b.assign(t, by["l2.maria"], ago(hours=5, minutes=55), "Maria Rossi")
    b.status(t, ago(hours=3), "ON_HOLD", "Maria Rossi", "Waiting for the quality engineer to count the pumps in stock")

    t = await current(Tpl("MES_APP", "DEGRADED", "Schedule board does not load for supervisors",
                          "The schedule board keeps loading; supervisors plan from an exported list meanwhile.", ""),
                      ago(hours=1, minutes=5))
    b.assign(t, by["l1.tom"], ago(minutes=55), "Tom Becker")
    b.status(t, ago(minutes=55), "ACKNOWLEDGED", "Tom Becker")

    t = await current(Tpl("HARDWARE", "DEGRADED", "Scanner on TURN-02 not reading lot labels",
                          "The hand scanner at TURN-02 reads nothing since this morning; lots are typed by hand.", ""),
                      ago(minutes=50), machine="TURN-02")

    t = await current(Tpl("HARDWARE", "MINOR", "Label printer at packing prints faded labels",
                          "Labels at the packing station are faded; the barcode is hard to scan.", ""), ago(hours=4))
    b.assign(t, by["admin1"], ago(hours=3, minutes=40), "Ada Admin")
    b.status(t, ago(hours=3, minutes=40), "IN_PROGRESS", "Ada Admin")

    t = await current(Tpl("MES_APP", "MINOR", "Wrong shift shown on the terminal header",
                          "Terminal T-L2 shows 'Early shift' during the late shift.", ""), ago(hours=1, minutes=30))
    b.assign(t, by["l1.tom"], ago(hours=1, minutes=10), "Tom Becker")

    t = await current(Tpl("DATA", "MINOR", "WIP count on line 1 does not match the floor",
                          "MES shows 14 parts in WIP at WC-MILL; the supervisor counted 9 on the floor.", ""),
                      ago(hours=9))
    b.assign(t, by["l1.priya"], ago(hours=8, minutes=30), "Priya Nair")
    b.status(t, ago(hours=8, minutes=30), "ACKNOWLEDGED", "Priya Nair")
    b.escalate(t, ago(hours=7), "Priya Nair", "Data correction needs L2 (runs and confirmations to check)")
    b.assign(t, by["l2.wei"], ago(hours=6, minutes=50), "Wei Chen")

    t = await current(Tpl("INTEGRATION", "MINOR", "Order quantity change from SAP not visible in MES",
                          "Order quantity was reduced in SAP yesterday afternoon; MES still plans the old quantity.", ""),
                      ago(hours=30))
    b.assign(t, by["l2.maria"], ago(hours=29), "Maria Rossi")
    b.status(t, ago(hours=29), "IN_PROGRESS", "Maria Rossi")

    t = await current(Tpl("ACCESS", "SERVICE_REQUEST", "Two new operators need PINs for line 2",
                          "Two new starters on the late shift need PINs for the assembly line terminal.", ""), ago(hours=6))
    b.assign(t, by["l1.tom"], ago(hours=5), "Tom Becker")
    b.status(t, ago(hours=5), "ACKNOWLEDGED", "Tom Becker")

    t = await current(Tpl("CHANGE_REQUEST", "SERVICE_REQUEST", "Add downtime reason 'waiting for crane'",
                          "Supervisors want a separate downtime reason for waiting on the crane at line 1.", ""),
                      ago(days=2, hours=2))
    b.assign(t, by["supervisor1"], ago(days=2), "Sam Supervisor")
    b.status(t, ago(days=2), "IN_PROGRESS", "Sam Supervisor")

    t = await current(Tpl("MES_APP", "DEGRADED", "Confirmation totals doubled after offline sync",
                          "After the terminal came back online, confirmations made offline were booked twice on two "
                          "orders.", ""), ago(hours=26), severity="P2")
    b.assign(t, by["l1.priya"], ago(hours=25, minutes=50), "Priya Nair")
    b.status(t, ago(hours=25, minutes=50), "ACKNOWLEDGED", "Priya Nair")
    b.escalate(t, ago(hours=24), "Priya Nair", "Duplicates despite client event ids; needs L2 analysis")
    b.assign(t, by["l2.wei"], ago(hours=23, minutes=50), "Wei Chen")
    b.escalate(t, ago(hours=8), "Wei Chen", "Root cause: offline queue resends with a new client event id after an app "
               "update. Code fix needed in the terminal's offline queue.")
    b.assign(t, by["l3.arjun"], ago(hours=7, minutes=45), "Arjun Rao")

    # open tickets: the worker's SLA check flags their at-risk / breached clocks within a minute (and alerts on-call)
    await s.flush()
    for t_, kind in b.tickets:
        await _emit(s, t_, EVENT_TICKET_CREATED, ["created"], demo=kind)
    history = [t_ for t_, k in b.tickets if k == "history"]
    return {"history": len(history), "open": len(b.tickets) - len(history),
            "from": first.date().isoformat(), "l3": sum(1 for t_, _ in b.tickets if t_.tier == "L3"),
            "team_added": missing}
