"""Evaluation harness: run a scenario through the real agent loop (real model, real prompts, real tools and policy)
against a simulated MES built from the scenario's data, then grade what the agent did by rules.

Nothing touches the real MES, ServiceNow or Jira: the scenario world answers every API call, and writes are only
recorded. Proposals and actions are graded the same way (a proposed resolve counts as "resolve").
"""

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from support_agent import prompts
from support_agent.actions import default_policies
from support_agent.clients import Api, ApiError
from support_agent.knowledge import Knowledge
from support_agent.llm import Model, Usage
from support_agent.runner import LimitReached, run_loop
from support_agent.settings import Settings
from support_agent.tools import Context


class WorldApi(Api):
    """The MES a scenario describes. Reads come from `world`; writes are recorded in `writes`."""

    def __init__(self, settings: Settings, world: dict[str, Any], ticket: dict[str, Any]) -> None:
        super().__init__(settings)
        self.w = world
        self.ticket = ticket
        self.writes: list[tuple[str, str, Any]] = []

    async def service_token(self) -> str:
        return "eval"

    async def loki(self, query: str, *, start_ns: int, end_ns: int, limit: int, direction: str = "backward") -> Any:
        lines = [ln for ln in self.w.get("logs", []) if all(part in json.dumps(ln) for part in _needles(query))]
        by: dict[str, list[list[str]]] = {}
        for i, ln in enumerate(lines[:limit]):
            by.setdefault(ln.get("service", "core-api"), []).append([str(1790000000000000000 + i), json.dumps(ln)])
        return [{"stream": {"service": svc}, "values": vals} for svc, vals in by.items()]

    async def health(self, service: str) -> tuple[bool, str]:  # type: ignore[override]
        return True, "HTTP 200"

    async def call(self, service: str, method: str, path: str, *, params: dict[str, Any] | None = None,
                   json: Any = None, token: str | None = None) -> Any:
        w, t = self.w, self.ticket
        if method != "GET":
            self.writes.append((service, path, json))
            if path.endswith("/escalate"):
                return {**t, "tier": {"L1": "L2", "L2": "L3"}.get(t["tier"], "L3"), "status": "IN_PROGRESS"}
            return {**t, "status": (json or {}).get("status", t["status"])} if path.startswith("/tickets/") else {"ok": True}
        if service == "core":
            if path == "/tickets/search":
                q = (params or {}).get("q", "")
                if q.upper().startswith("MES-"):
                    return [x for x in w.get("other_tickets", []) if x["number"] == q.upper()]
                return w.get("other_tickets", [])
            if path.startswith("/tickets/"):
                tid = path.split("/")[2]
                if tid == t["id"]:
                    return t
                return next((x for x in w.get("other_tickets", []) if x["id"] == tid), None) or _404()
            table = {"/maintenance/equipment": "equipment", "/terminals": "terminals", "/maintenance/orders": "maintenance_orders",
                     "/quality/nonconformances": "nonconformances", "/inventory/shortages": "shortages",
                     "/ops/event-failures": "event_failures"}
            if path in table:
                return w.get(table[path], [])
            if path == "/execution/live":
                return {"machines": w.get("live", [])}
            if path == "/work-orders":
                return {"items": w.get("work_orders", [])}
            if path.startswith("/work-orders/"):
                return w.get("work_order_detail", {})
            if path == "/audit":
                return {"items": w.get("audit", []), "total": len(w.get("audit", []))}
        if service == "erp":
            if path == "/outbound":
                return w.get("outbound", [])
            if path.startswith("/outbound/"):
                return w.get("outbound_detail", {}).get(path.split("/")[2]) or _404()
            if path == "/inbound":
                return w.get("inbound", [])
            if path in ("/outbound-stats", "/poll-state"):
                return w.get(path.strip("/").replace("-", "_"), {})
        if service == "itsm":
            if path == "/messages":
                return w.get("itsm_messages", [])
            if path == "/status":
                return {"connections": [{"system": "servicenow", "reachable": True}], "counts": {}}
        return []


def _404() -> Any:
    raise ApiError(404, "not found")


def _needles(query: str) -> list[str]:
    """The quoted literals of a LogQL query (label values and |= filters), to match world log lines."""
    import re

    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', query)
    return [p.replace('\\"', '"') for p in parts if p not in (".+",) and "|" not in p]


class EvalRecorder:
    """The Recorder interface, in memory: evaluation runs leave no trace in the agent's tables."""

    def __init__(self, model: str) -> None:
        self.run_id = uuid4()
        self.model = model
        self.seq = 0
        self.usage = Usage()
        self.cost = Decimal(0)
        self.outcomes: list[str] = []
        self.steps: list[dict[str, Any]] = []
        self.proposals: list[dict[str, Any]] = []
        self.rca: dict[str, Any] | None = None

    async def step(self, kind: str, name: str | None, input: Any = None, output: Any = None, *,
                   is_error: bool = False, started: float | None = None) -> None:
        self.seq += 1
        self.steps.append({"kind": kind, "name": name, "input": input, "is_error": is_error})

    async def add_usage(self, u: Usage, model: str) -> None:
        self.usage.input += u.input
        self.usage.output += u.output
        self.usage.cache_read += u.cache_read
        self.usage.cache_write += u.cache_write
        self.cost += u.cost(model)

    async def propose(self, *, ticket: dict[str, Any], action: str, args: dict[str, Any], summary: str,
                      rationale: str | None) -> UUID:
        self.proposals.append({"action": action, "args": args, "summary": summary})
        return uuid4()

    async def set_rca(self, rca: dict[str, Any]) -> None:
        self.rca = rca

    async def finish(self, *a: Any, **kw: Any) -> None:
        return None


@dataclass
class Scenario:
    id: str
    title: str
    tier: str  # L1 | L2
    ticket: dict[str, Any]
    world: dict[str, Any]
    expect_any: list[str] = field(default_factory=list)  # at least one of these actions (done or proposed)
    expect_all: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)  # none of these
    must_use: list[str] = field(default_factory=list)  # tools that must be called
    rca_categories: list[str] = field(default_factory=list)  # L2: the recorded category must be one of these
    arg_checks: dict[str, dict[str, Any]] = field(default_factory=dict)  # action -> expected args subset


@dataclass
class Result:
    id: str
    title: str
    tier: str
    passed: bool
    checks: list[tuple[str, bool, str]]
    actions: list[str]
    tools: list[str]
    cost_usd: float
    seconds: float
    summary: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "checks": [{"check": c, "ok": ok, "detail": d} for c, ok, d in self.checks]}


def _actions(rec: EvalRecorder, tier: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    done = [o for o in rec.outcomes if not o.startswith(("PROPOSED:", "CHOICES:")) and o not in ("RCA",)]
    proposed = [p["action"] for p in rec.proposals]
    names = [("escalate_l3" if a == "escalate" and tier == "L2" else a) for a in done + proposed]
    args = {p["action"]: p["args"] for p in rec.proposals}
    return list(dict.fromkeys(names)), args


def grade(sc: Scenario, rec: EvalRecorder) -> list[tuple[str, bool, str]]:
    actions, args = _actions(rec, sc.tier)
    tools = [s["name"] for s in rec.steps if s["kind"] == "TOOL"]
    checks: list[tuple[str, bool, str]] = []
    if sc.expect_any:
        checks.append((f"one of {sc.expect_any}", any(a in actions for a in sc.expect_any), f"did {actions}"))
    for a in sc.expect_all:
        checks.append((f"did {a}", a in actions, f"did {actions}"))
    for a in sc.forbid:
        checks.append((f"did not {a}", a not in actions, f"did {actions}"))
    for tname in sc.must_use:
        checks.append((f"used {tname}", tname in tools, f"tools {sorted(set(tools))}"))
    if sc.rca_categories:
        cat = (rec.rca or {}).get("category")
        checks.append((f"root cause category in {sc.rca_categories}", cat in sc.rca_categories, f"recorded {cat}"))
    for action, want in sc.arg_checks.items():
        got = args.get(action, {})
        ok = all(str(got.get(k, "")).upper() == str(v).upper() for k, v in want.items())
        checks.append((f"{action} with {want}", ok, f"proposed {got}"))
    return checks


async def run_scenario(sc: Scenario, model: Model, settings: Settings, knowledge: Knowledge) -> Result:
    ticket = {"id": str(uuid4()), "plant_id": "01a0d247-5aac-7059-beb5-d63add203ce1", "status": "NEW", "tier": sc.tier,
              "activity": [], "source": "TERMINAL", "reported_at": "2026-09-25T10:00:00Z", **sc.ticket}
    api = WorldApi(settings, sc.world, ticket)
    rec = EvalRecorder(model.name)
    l2 = sc.tier == "L2"
    ctx = Context(api=api, knowledge=knowledge, recorder=rec, plant_id=ticket["plant_id"],  # type: ignore[arg-type]
                  mode="l2" if l2 else "ticket", policies=default_policies(),
                  ticket={k: ticket[k] for k in ("id", "number", "plant_id", "status", "tier")})
    system = (prompts.l2_system(knowledge.index(), knowledge.architecture) if l2
              else prompts.ticket_system(knowledge.index()))
    messages = [{"role": "user", "content": prompts.ticket_message(ticket, "ESCALATED" if l2 else "CREATED")}]
    started = time.monotonic()
    error, summary = None, ""
    try:
        res = await run_loop(model=model, system=system, messages=messages, ctx=ctx,
                             effort=settings.effort_l2 if l2 else settings.effort_ticket,
                             max_steps=settings.max_steps + (12 if l2 else 0), budget_usd=Decimal("3"))
        summary = res.text
    except LimitReached as e:
        error = str(e)
    rec.outcomes = ctx.outcomes
    checks = grade(sc, rec) + ([("finished within limits", False, error)] if error else [])
    actions, _ = _actions(rec, sc.tier)
    return Result(sc.id, sc.title, sc.tier, all(ok for _, ok, _ in checks), checks, actions,
                  [s["name"] for s in rec.steps if s["kind"] == "TOOL"], float(rec.cost),
                  round(time.monotonic() - started, 1), summary[:600], error)
