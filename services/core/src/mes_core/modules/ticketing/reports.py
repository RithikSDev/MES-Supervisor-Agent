"""Support reports: ticket-volume forecast and the SLA breach / exit report.

The forecast is deliberately simple and explainable (support leads have to trust and explain it):
  - daily ticket counts over the last `weeks` full weeks, by the plant's local day (today, still running, is left
    out),
  - weekly level and trend from a least-squares line through the weekly totals; the trend is damped further out
    (a trend seen over eight weeks is not carried unchanged into month three),
  - a weekday profile (share of the week's tickets per weekday),
  - an 80% range from the scatter of the weeks around the line plus Poisson noise, widening with the distance,
  - severity / category mix of the last four weeks applied to the forecast total,
  - a backtest: the same method, fitted without the last week, predicting it.
"""

import math
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.org.public import get_plant
from mes_core.modules.ticketing import domain
from mes_core.modules.ticketing.models import OPEN_STATUSES, Ticket
from mes_core.modules.ticketing.service import clocks

Z80 = 1.2816  # two-sided 80% range
PHI = 0.9  # trend damping per week ahead
MAX_HORIZON_DAYS = 92  # about three months


def _now() -> datetime:
    return datetime.now(UTC)


def _fit(weekly: list[float]) -> tuple[float, float, float]:
    """Least-squares line through weekly totals -> (level of the last week, slope per week, residual sd)."""
    n = len(weekly)
    if n == 0:
        return 0.0, 0.0, 0.0
    if n == 1:
        return weekly[0], 0.0, 0.0
    xs = range(n)
    mx, my = (n - 1) / 2, sum(weekly) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, weekly, strict=True)) / sxx
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, weekly, strict=True)]
    sd = math.sqrt(sum(r * r for r in resid) / max(1, n - 2))
    return intercept + slope * (n - 1), slope, sd


def _profile(days: dict[date, int]) -> list[float]:
    """Share of a week's tickets per weekday (Mon=0), lightly smoothed so an empty weekday isn't 'never'."""
    per = [0.0] * 7
    for d, c in days.items():
        per[d.weekday()] += c
    total = sum(per)
    if total == 0:
        return [1 / 7] * 7
    smoothed = [(v + 0.5) for v in per]
    return [v / sum(smoothed) for v in smoothed]


def _range(exp: float, extra_sd: float) -> tuple[int, int]:
    spread = Z80 * math.sqrt(exp + extra_sd ** 2)
    return max(0, math.floor(exp - spread)), math.ceil(exp + spread)


def _predict(days: dict[date, int], start: date, horizon: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered = sorted(days)
    weeks = [sum(days[d] for d in ordered[i:i + 7]) for i in range(0, len(ordered) - len(ordered) % 7, 7)]
    level, slope, sd = _fit([float(w) for w in weeks])
    profile = _profile(days)
    out: list[dict[str, Any]] = []
    week_total: dict[int, float] = {}
    for i in range(horizon):
        k = i // 7 + 1  # weeks ahead
        week_total.setdefault(k, max(0.0, level + slope * sum(PHI ** j for j in range(1, k + 1))))
        d = start + timedelta(days=i)
        exp = week_total[k] * profile[d.weekday()]
        low, high = _range(exp, sd * math.sqrt(k) * profile[d.weekday()])
        out.append({"day": d.isoformat(), "weekday": d.strftime("%a"), "expected": round(exp, 1), "low": low, "high": high})
    per_week = []
    for k in range(math.ceil(horizon / 7)):
        chunk = out[k * 7:(k + 1) * 7]
        exp = sum(p["expected"] for p in chunk)
        low, high = _range(exp, sd * math.sqrt(k + 1) * len(chunk) / 7)
        per_week.append({"week": k + 1, "start": chunk[0]["day"], "end": chunk[-1]["day"], "days": len(chunk),
                         "expected": round(exp, 1), "low": low, "high": high})
    total = sum(x["expected"] for x in out)
    # errors of neighbouring weeks go the same way (a wrong level stays wrong): add them up, don't average them out
    low, high = _range(total, sd * sum(math.sqrt(w["week"]) * w["days"] / 7 for w in per_week))
    return out, {"expected": round(total, 1), "low": low, "high": high, "level": level, "slope": slope, "sd": sd,
                 "per_week": per_week}


async def forecast(s: AsyncSession, plant_id: UUID, *, horizon_days: int = 7, weeks: int = 8) -> dict[str, Any]:
    now = _now()
    tz = (await get_plant(s, plant_id)).tz
    today = now.astimezone(tz).date()  # the plant's day: shifts and weekdays are local
    first = today - timedelta(days=7 * weeks)
    tickets = list((await s.scalars(select(Ticket).where(
        Ticket.plant_id == plant_id, Ticket.reported_at >= datetime.combine(first, datetime.min.time(), tz)))).all())
    days = {first + timedelta(days=i): 0 for i in range(7 * weeks)}
    for t in tickets:
        d = t.reported_at.astimezone(tz).date()
        if d in days:
            days[d] += 1
    history_n = sum(days.values())
    points, total = _predict(days, today, horizon_days)

    # backtest: fit without the last week, predict it, compare
    last_week = sorted(days)[-7:]
    back_days = {d: c for d, c in days.items() if d < last_week[0]}
    backtest = None
    if len(back_days) >= 14:
        _, bt = _predict(back_days, last_week[0], 7)
        actual = sum(days[d] for d in last_week)
        backtest = {"week_start": last_week[0].isoformat(), "predicted": bt["expected"], "low": bt["low"],
                    "high": bt["high"], "actual": actual,
                    "error_pct": round(100 * abs(bt["expected"] - actual) / actual, 1) if actual else None,
                    "within_range": bt["low"] <= actual <= bt["high"]}

    recent = [t for t in tickets if t.reported_at.astimezone(tz).date() >= today - timedelta(days=28)]

    def mix(key: str, labels: list[str] | None = None) -> list[dict[str, Any]]:
        c = Counter(getattr(t, key) for t in recent)
        n = sum(c.values()) or 1
        keys = labels or [k for k, _ in c.most_common()]
        return [{"key": k, "share_pct": round(100 * c.get(k, 0) / n, 1), "expected": round(total["expected"] * c.get(k, 0) / n, 1)}
                for k in keys]

    by_severity = mix("severity", list(domain.SEVERITY_TARGETS))
    resolved_4w = sum(1 for t in tickets if t.resolved_at and t.resolved_at >= now - timedelta(days=28))
    open_now = (await s.execute(select(func.count()).select_from(Ticket).where(
        Ticket.plant_id == plant_id, Ticket.status.in_(OPEN_STATUSES)))).scalar_one()
    expected_resolved = round(resolved_4w / 28 * horizon_days, 1)
    weekly_hist = [{"week_start": d.isoformat(), "count": sum(days[d + timedelta(days=i)] for i in range(7))}
                   for d in sorted(days)[::7]]
    peak = max(points, key=lambda p: p["expected"]) if points else None
    last7 = weekly_hist[-1]["count"] if weekly_hist else 0
    return {
        "generated_at": now, "timezone": str(tz), "horizon_days": horizon_days, "history_weeks": weeks, "history_tickets": history_n,
        "method": "Weekly level and trend (least squares over the last weeks) x weekday profile; 80% range from the "
                  "weeks' scatter plus random variation; severity/category mix of the last 4 weeks.",
        "history": [{"day": d.isoformat(), "count": days[d]} for d in sorted(days)[-28:]],
        "granularity": "week" if horizon_days > 21 else "day",  # how the card shows it first
        "weekly_history": weekly_hist,
        "forecast": points,
        "totals": {"expected": total["expected"], "low": total["low"], "high": total["high"],
                   "per_week": total["per_week"],
                   "last_7_days": last7,
                   "change_vs_last_week_pct": round(100 * (total["expected"] * 7 / horizon_days - last7) / last7, 1) if last7 else None},
        "trend_per_week": round(total["slope"], 2),
        "peak_day": peak,
        "by_severity": by_severity,
        "expected_p1_p2": round(sum(x["expected"] for x in by_severity if x["key"] in ("P1", "P2")), 1),
        "by_category": mix("category"),
        "backlog": {"open_now": open_now, "expected_resolved": expected_resolved,
                    "projected_open": max(0, round(open_now + total["expected"] - expected_resolved))},
        "backtest": backtest,
        "enough_history": history_n >= 20 and len([w for w in weekly_hist if w["count"]]) >= 3,
    }


# ------------------------------------------------------------------------------------------------ SLA report


def _minutes(delta: timedelta) -> int:
    return round(delta.total_seconds() / 60)


async def sla_report(s: AsyncSession, plant_id: UUID, since: datetime, until: datetime) -> dict[str, Any]:
    """Breaches: tickets whose response or resolution target fell due in the period and was missed.
    Exits: tickets that left the SLA clock in the period (resolved, closed or cancelled), met or missed."""
    now = _now()
    until = min(until, now)
    lookback = since - timedelta(days=3)  # a ticket reported before the period can fall due / exit in it
    tickets = list((await s.scalars(select(Ticket).where(
        Ticket.plant_id == plant_id,
        (Ticket.reported_at >= lookback) | Ticket.status.in_(OPEN_STATUSES)
        | (Ticket.resolved_at >= since) | (Ticket.closed_at >= since)))).all())
    breaches: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    at_risk: list[dict[str, Any]] = []
    for t in tickets:
        resp, fix = clocks(t, now)
        base: dict[str, Any] = {"ticket_id": str(t.id), "number": t.number, "title": t.title, "severity": t.severity, "tier": t.tier,
                "status": t.status, "category": t.category, "assignee": t.assignee_name, "sn_number": t.sn_number,
                "reported_at": t.reported_at}
        if t.status != "CANCELLED":
            for which, c in (("response", resp), ("resolution", fix)):
                if c.breached and c.due and since <= c.due <= until:
                    end = c.met_at or now
                    breaches.append({**base, "which": which, "due": c.due, "met_at": c.met_at,
                                     "still_open": c.met_at is None,
                                     "late_min": max(1, math.ceil((end - c.due).total_seconds() / 60))})
        exit_at = t.resolved_at or t.closed_at
        if exit_at and since <= exit_at <= until:
            kind = "RESOLVED" if t.resolved_at else t.status  # CLOSED without resolution / CANCELLED
            target_h = domain.SEVERITY_TARGETS[t.severity][1]
            exits.append({**base, "exit": kind, "exit_at": exit_at,
                          "response_met": None if resp.met_at is None and not resp.breached else not resp.breached,
                          "resolution_met": None if kind == "CANCELLED" or fix.due is None else not fix.breached,
                          "time_to_resolve_h": round(((exit_at - t.reported_at).total_seconds() - (t.held_s or 0)) / 3600, 1),
                          "target_h": target_h.total_seconds() / 3600 if target_h else None})
        if t.status in OPEN_STATUSES:
            for which, c in (("response", resp), ("resolution", fix)):
                if domain.at_risk(c, t.reported_at, now):
                    at_risk.append({**base, "which": which, "due": c.due, "minutes_left": _minutes((c.due or now) - now)})
    breaches.sort(key=lambda b: (b["severity"], -b["late_min"]))
    exits.sort(key=lambda e: e["exit_at"])
    at_risk.sort(key=lambda r: r["minutes_left"])

    def pct(ok: int, n: int) -> float | None:
        return round(100 * ok / n, 1) if n else None

    by_sev = []
    for sev in domain.SEVERITY_TARGETS:
        ex = [e for e in exits if e["severity"] == sev]
        fixable = [e for e in ex if e["resolution_met"] is not None]
        br = [b for b in breaches if b["severity"] == sev]
        by_sev.append({"severity": sev, "exits": len(ex), "resolution_met": sum(1 for e in fixable if e["resolution_met"]),
                       "resolution_missed": sum(1 for e in fixable if not e["resolution_met"]),
                       "resolution_met_pct": pct(sum(1 for e in fixable if e["resolution_met"]), len(fixable)),
                       "breaches": len(br), "avg_late_min": round(sum(b["late_min"] for b in br) / len(br)) if br else None})
    by_assignee: dict[str, dict[str, int]] = {}
    for b in breaches:
        by_assignee.setdefault(b["assignee"] or "Unassigned", {"breaches": 0, "still_open": 0})
        by_assignee[b["assignee"] or "Unassigned"]["breaches"] += 1
        by_assignee[b["assignee"] or "Unassigned"]["still_open"] += b["still_open"]
    fixable = [e for e in exits if e["resolution_met"] is not None]
    responded = [e for e in exits if e["response_met"] is not None]
    return {
        "since": since, "until": until, "generated_at": now,
        "summary": {
            "breaches": len(breaches), "response_breaches": sum(1 for b in breaches if b["which"] == "response"),
            "resolution_breaches": sum(1 for b in breaches if b["which"] == "resolution"),
            "breached_still_open": len({b["number"] for b in breaches if b["still_open"]}),
            "exits": len(exits), "resolved": sum(1 for e in exits if e["exit"] == "RESOLVED"),
            "cancelled": sum(1 for e in exits if e["exit"] == "CANCELLED"),
            "resolution_met_pct": pct(sum(1 for e in fixable if e["resolution_met"]), len(fixable)),
            "response_met_pct": pct(sum(1 for e in responded if e["response_met"]), len(responded)),
            "at_risk_now": len(at_risk),
        },
        "by_severity": by_sev,
        "by_assignee": sorted(({"assignee": k, **v} for k, v in by_assignee.items()), key=lambda x: -x["breaches"]),
        "breaches": breaches, "exits": exits, "at_risk": at_risk,
    }

