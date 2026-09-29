"""Events -> alerts -> deliveries, and the escalation chain.

An alert names rota roles (e.g. L1 + SUPERVISOR). Level 0 goes to each role's primary, level 1 to the backups,
level 2 to the supervisor; an alert nobody acknowledges within the rule's `ack_minutes` moves one level up. An alert
closes itself when its cause goes away (ticket acknowledged or resolved, proposal decided, agent healthy again).
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mes_common.events import EventEnvelope
from mes_common.ids import new_id
from notifier.db import delivery, notification, rota, rule
from notifier.settings import Settings

log = logging.getLogger(__name__)

MAX_LEVEL = 2
OPEN = ("NEW", "SENT")


@dataclass(frozen=True)
class RuleSpec:
    label: str
    channels: tuple[str, ...]
    ack_minutes: int


RULES: dict[str, RuleSpec] = {
    "P1_RAISED": RuleSpec("A P1 ticket (line stopped) was raised", ("email", "teams"), 10),
    "SLA_AT_RISK": RuleSpec("An SLA target is at risk (75% of the time used)", ("email", "teams"), 15),
    "SLA_BREACHED": RuleSpec("An SLA target was missed", ("email", "teams"), 10),
    "ESCALATED": RuleSpec("A ticket was escalated to L2 or L3", ("email", "teams"), 20),
    "APPROVAL_WAITING": RuleSpec("The AI agent proposes an action that needs approval", ("email",), 30),
    "AI_HANDOVER": RuleSpec("The AI agent handed a ticket over to people", ("email", "teams"), 15),
    "AGENT_DOWN": RuleSpec("The AI support agent is paused or unavailable", ("email", "teams"), 30),
}
ROLES = ("L1", "L2", "L3", "SUPERVISOR")
DEFAULT_ROTA = [  # demo on-call people (edit on the Notifications page)
    ("L1", 1, "Lena (L1 on call)", "l1.oncall@plant.local"), ("L1", 2, "Luca (L1 backup)", "l1.backup@plant.local"),
    ("L2", 1, "Lars (L2 on call)", "l2.oncall@plant.local"), ("L2", 2, "Lea (L2 backup)", "l2.backup@plant.local"),
    ("L3", 1, "Dana (L3 developer)", "l3.oncall@plant.local"),
    ("SUPERVISOR", 1, "Sam Supervisor", "supervisor@plant.local"),
]


async def ensure_defaults(sf: async_sessionmaker[AsyncSession]) -> None:
    async with sf() as s, s.begin():
        for key, spec in RULES.items():
            await s.execute(insert(rule).values(key=key, enabled=True, channels=list(spec.channels),
                                                ack_minutes=spec.ack_minutes, updated_by="default")
                            .on_conflict_do_nothing(index_elements=["key"]))
        if not (await s.execute(select(rota.c.id).limit(1))).first():
            for role, pos, name, email in DEFAULT_ROTA:
                await s.execute(rota.insert().values(id=new_id(), role=role, position=pos, name=name, email=email))


def _ticket_url(settings: Settings, ticket_id: str) -> str:
    return f"{settings.mes_ui_url}/tickets?ticket={ticket_id}"


def _tier_roles(tier: str | None) -> list[str]:
    return [tier] if tier in ("L1", "L2", "L3") else ["L1"]


# ------------------------------------------------------------------------------------------------ intake


class Intake:
    """Kafka handler for mes.tickets.v1 and ai.support.v1: opens alerts, closes alerts whose cause went away. Events
    older than `max_age_min` are ignored: a new consumer group replays the whole topic, and nobody should be paged
    about history."""

    def __init__(self, settings: Settings, max_age_min: int = 30) -> None:
        self.s = settings
        self._max_age = timedelta(minutes=max_age_min)

    async def __call__(self, s: AsyncSession, env: EventEnvelope) -> None:
        if datetime.now(UTC) - env.occurred_at > self._max_age:
            return
        p = env.payload
        if p.get("demo") == "history":  # backdated demo tickets, already closed: nothing to alert about
            return
        et = env.event_type
        tid = p.get("ticket_id")
        num = p.get("number")
        url = _ticket_url(self.s, tid) if tid else self.s.mes_ui_url
        title_of = f"{num} {p.get('title') or ''}".strip()
        # a ticket created already acknowledged (demo data, a late import) needs no "P1 raised" call-out
        if et == "mes.ticket.created" and p.get("severity") == "P1" and p.get("status", "NEW") == "NEW":
            await open_alert(s, "P1_RAISED", f"P1:{tid}", p, _tier_roles(p.get("tier")) + ["SUPERVISOR"],
                             f"P1 raised: {title_of}",
                             f"{num} ({p.get('category')}) reported by {p.get('reported_by')} on "
                             f"{p.get('line_code') or 'the plant'}{' / ' + p['equipment_code'] if p.get('equipment_code') else ''}. "
                             f"Response due {p.get('response_due_at')}.\n{url}")
        elif et == "mes.ticket.updated":
            changes = set(p.get("changes") or [])
            if "tier" in changes and p.get("tier") in ("L2", "L3"):
                await open_alert(s, "ESCALATED", f"ESC:{tid}:{p['tier']}", p, [p["tier"]],
                                 f"{num} escalated to {p['tier']}: {p.get('title')}",
                                 f"Severity {p.get('severity')}, status {p.get('status')}. "
                                 f"{(p.get('comment') or {}).get('text') or ''}\n{url}")
            if "status" in changes:
                status = p.get("status")
                if status in ("RESOLVED", "CLOSED", "CANCELLED"):
                    await close_alerts(s, tid, None, f"ticket {status.lower()}")
                elif status in ("ACKNOWLEDGED", "IN_PROGRESS"):
                    await close_alerts(s, tid, ["P1_RAISED"], "ticket acknowledged")
        elif et in ("mes.ticket.sla_at_risk", "mes.ticket.sla_breached"):
            breached = et.endswith("breached")
            which = p.get("which")
            roles = _tier_roles(p.get("tier")) + (["SUPERVISOR"] if breached else [])
            if breached:
                await close_alerts(s, tid, ["SLA_AT_RISK"], f"{which} target missed")
            due_text = f"was due {p.get('due')}" if breached else f"is due {p.get('due')} ({p.get('minutes_left')} min left)"
            await open_alert(
                s, "SLA_BREACHED" if breached else "SLA_AT_RISK", f"SLA:{tid}:{which}:{'breach' if breached else 'risk'}",
                p, roles, f"{num} {which} SLA {'missed' if breached else 'at risk'} ({p.get('severity')})",
                f"{p.get('title') or ''}\n{str(which).capitalize()} target {p.get('severity')} {due_text}.\n{url}")
        elif et == "ai.proposal.created":
            await open_alert(s, "APPROVAL_WAITING", f"PROP:{p['proposal_id']}", p, ["SUPERVISOR"],
                             f"Approval needed on {num}: {p.get('action')}",
                             f"The AI support agent proposes: {p.get('summary')}\nWhy: {p.get('rationale') or '-'}\n"
                             f"Approve or reject: {self.s.mes_ui_url}/ai-support?tab=approvals")
        elif et == "ai.proposal.decided":
            await close_alerts(s, tid, ["APPROVAL_WAITING"], f"proposal {p.get('decision', '').lower()} by {p.get('decided_by')}",
                               dedup=f"PROP:{p['proposal_id']}")
        elif et == "ai.run.handed_over":
            await open_alert(s, "AI_HANDOVER", f"HAND:{p.get('run_id')}", p, _tier_roles(p.get("tier")),
                             f"AI agent handed {num} over to people",
                             f"Reason: {p.get('reason')}\n{url}")
        elif et == "ai.agent.health_changed":
            if p.get("ok"):
                await close_alerts(s, None, ["AGENT_DOWN"], "agent healthy again")
            else:
                await open_alert(s, "AGENT_DOWN", f"AGENT:{env.event_id}", p, ["SUPERVISOR", "L2"],
                                 "AI support agent paused",
                                 f"{p.get('error')}\nTickets are not being worked by the AI; they stay with people.\n"
                                 f"{self.s.mes_ui_url}/ai-support")


async def open_alert(s: AsyncSession, rule_key: str, dedup: str, p: dict[str, Any], roles: list[str], title: str,
                     body: str, *, force: bool = False) -> None:
    r = (await s.execute(select(rule).where(rule.c.key == rule_key))).first()
    if r is not None and not r.enabled and not force:
        return
    await s.execute(insert(notification).values(
        id=new_id(), rule=rule_key, dedup_key=dedup, severity=p.get("severity"),
        ticket_id=UUID(p["ticket_id"]) if p.get("ticket_id") else None, ticket_number=p.get("number"),
        title=title[:300], body=body[:4000], roles=list(dict.fromkeys(roles)), status="NEW",
    ).on_conflict_do_nothing(index_elements=["dedup_key"]))


async def close_alerts(s: AsyncSession, ticket_id: str | None, rules: list[str] | None, reason: str,
                       dedup: str | None = None) -> None:
    conds = [notification.c.status.in_(OPEN)]
    if ticket_id:
        conds.append(notification.c.ticket_id == UUID(ticket_id))
    if rules:
        conds.append(notification.c.rule.in_(rules))
    if dedup:
        conds.append(notification.c.dedup_key == dedup)
    await s.execute(update(notification).where(*conds).values(status="CLOSED", closed_reason=reason[:300]))


# ------------------------------------------------------------------------------------------------ delivery

Sender = Callable[[str, list[dict[str, Any]], str, str, dict[str, Any]], Awaitable[list[tuple[str, bool, str | None]]]]


class Dispatcher:
    """Sends NEW alerts to the people of their current level, and moves unacknowledged ones up the chain."""

    def __init__(self, sf: async_sessionmaker[AsyncSession], senders: dict[str, Sender]) -> None:
        self.sf = sf
        self.senders = senders

    async def recipients(self, s: AsyncSession, roles: list[str], level: int) -> list[dict[str, Any]]:
        people = [dict(r._mapping) for r in await s.execute(select(rota).where(rota.c.active).order_by(rota.c.position))]
        chosen: list[dict[str, Any]] = []
        for role in roles:
            mine = [p for p in people if p["role"] == role]
            if level == 0:
                chosen += mine[:1]
            elif level == 1:
                chosen += mine[1:2] or mine[:1]  # no backup: the primary again
        if level >= 2 or not chosen:
            chosen += [p for p in people if p["role"] == "SUPERVISOR"][:1]
        seen: set[str] = set()
        return [p for p in chosen if not (p["email"] in seen or seen.add(p["email"]))]  # type: ignore[func-returns-value]

    async def run_once(self) -> int:
        async with self.sf() as s, s.begin():
            rows = (await s.execute(select(notification).where(notification.c.status == "NEW")
                                    .order_by(notification.c.seq).limit(20).with_for_update(skip_locked=True))).all()
            rules = {r.key: r for r in await s.execute(select(rule))}
            for n in rows:
                people = await self.recipients(s, list(n.roles), n.level)
                r = rules.get(n.rule)
                channels = list(r.channels) if r else ["email"]
                alert = {"id": str(n.id), "title": n.title, "body": n.body, "severity": n.severity, "rule": n.rule,
                         "ticket_number": n.ticket_number, "level": n.level}
                for ch in channels:
                    sender = self.senders.get(ch)
                    if sender is None:
                        continue
                    for recipient, ok, error in await sender(ch, people, n.title, n.body, alert):
                        await s.execute(delivery.insert().values(notification_id=n.id, level=n.level, channel=ch,
                                                                 recipient=recipient, ok=ok, error=error))
                await s.execute(update(notification).where(notification.c.id == n.id).values(
                    status="SENT", sent_at=datetime.now(UTC),
                    recipients=list(n.recipients or []) + [{"name": p["name"], "email": p["email"], "role": p["role"],
                                                            "level": n.level} for p in people]))
        return len(rows)

    async def escalate(self) -> int:
        """Alerts nobody acknowledged in time go one level up (backup, then supervisor)."""
        now = datetime.now(UTC)
        async with self.sf() as s, s.begin():
            rules = {r.key: r.ack_minutes for r in await s.execute(select(rule))}
            rows = (await s.execute(select(notification.c.id, notification.c.rule, notification.c.sent_at,
                                           notification.c.level)
                                    .where(notification.c.status == "SENT", notification.c.level < MAX_LEVEL)
                                    .with_for_update(skip_locked=True))).all()
            due = [r.id for r in rows if r.sent_at and now - r.sent_at >= timedelta(minutes=rules.get(r.rule, 15))]
            if due:
                await s.execute(update(notification).where(notification.c.id.in_(due))
                                .values(level=notification.c.level + 1, status="NEW"))
        return len(due)


async def acknowledge(s: AsyncSession, notification_id: UUID, by: str) -> bool:
    res = await s.execute(update(notification).where(notification.c.id == notification_id,
                                                     notification.c.status.in_(OPEN))
                          .values(status="ACKED", acked_by=by, acked_at=datetime.now(UTC)))
    return bool(res.rowcount)
