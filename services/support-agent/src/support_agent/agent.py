"""Ticket runs (queued, processed by the worker), operator chat turns and support engineer chat turns (streamed by
the API)."""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import anthropic
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mes_common.ids import new_id
from mes_common.logs import request_id_var
from support_agent import engineer as eng
from support_agent import prompts
from support_agent.actions import ACTIONS, OFF
from support_agent.clients import Api, ApiError
from support_agent.db import chat_session, engineer_chat, proposal, run, ticket_override
from support_agent.knowledge import Knowledge
from support_agent.knowledge_store import KnowledgeStore
from support_agent.llm import Model
from support_agent.mcp_client import McpHub
from support_agent.runner import LimitReached, run_loop
from support_agent.settings import Settings
from support_agent.store import EVENT_HANDOVER, Recorder, load_policies, load_settings, publish, set_health
from support_agent.tools import Context

log = logging.getLogger(__name__)
Emit = Callable[[dict[str, Any]], Awaitable[None]]
OPEN = ("NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD")
# Claude API errors that are about the setup, not the ticket: wrong key, no access, unknown model, invalid request
CONFIG_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError,
                 anthropic.BadRequestError)
# errors that go away by themselves: rate limit, overload, network
TRANSIENT_ERRORS = (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)
GREETING_CHOICES = ["Printer or labels", "Scanner", "Terminal or login", "Order won't confirm", "Something else"]
ENGINEER_SUGGESTIONS = ["Bring up all the open tickets", "Forecast the tickets for the next two weeks",
                        "SLA breach and exit report for this week", "List the tickets assigned to other engineers"]


class ChatError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def outcome_of(outcomes: list[str]) -> str:
    done = set(outcomes)
    for key, label in (("resolve", "RESOLVED"), ("close_duplicate", "DUPLICATE"), ("escalate", "ESCALATED"),
                       ("FIXED_IN_CHAT", "FIXED_IN_CHAT"), ("TICKET_CREATED", "TICKET_CREATED"),
                       ("PROPOSED:resolve", "RESOLVE_PROPOSED")):
        if key in done:
            return label
    if any(o.startswith("PROPOSED:") for o in done):
        return "ACTION_PROPOSED"
    if "RCA" in done:
        return "ROOT_CAUSE"
    return "NOTED" if "add_note" in done else "NO_ACTION"


class Agent:
    def __init__(self, settings: Settings, sf: async_sessionmaker[AsyncSession], api: Api, model: Model | None,
                 knowledge: Knowledge, mcp: McpHub | None = None) -> None:
        self.s = settings
        self.sf = sf
        self.api = api
        self.model = model  # None: no API key configured
        self.knowledge = knowledge
        self._ticket_system = prompts.ticket_system(knowledge.index())
        self._chat_system = prompts.chat_system(knowledge.index())
        self._l2_system = prompts.l2_system(knowledge.index(), knowledge.architecture)
        self._engineer_system = prompts.engineer_system(knowledge.index(), knowledge.architecture)
        self._busy_chats: set[UUID] = set()
        self.mcp = mcp or McpHub(settings, sf)
        self.docs = KnowledgeStore(sf, settings.runbooks_dir.parent)

    # ------------------------------------------------------------------------------------------ ticket runs

    @staticmethod
    async def enqueue(s: AsyncSession, *, ticket_id: UUID, number: str, plant_id: UUID | None, trigger: str,
                      trigger_key: str | None, tier: str = "L1") -> UUID | None:
        rid = new_id()
        row = (await s.execute(insert(run).values(
            id=rid, kind="TICKET", tier="L1" if tier == "L1" else "L2", trigger=trigger, trigger_key=trigger_key,
            status="QUEUED", ticket_id=ticket_id, ticket_number=number, plant_id=plant_id,
        ).on_conflict_do_nothing(index_elements=["trigger_key"]).returning(run.c.id))).first()
        return row[0] if row else None

    async def _claim(self) -> UUID | None:
        now = datetime.now(UTC)
        async with self.sf() as s, s.begin():
            rid = (await s.execute(select(run.c.id).where(run.c.kind == "TICKET", or_(
                and_(run.c.status == "QUEUED", or_(run.c.lease_until.is_(None), run.c.lease_until < now)),
                and_(run.c.status == "RUNNING", run.c.lease_until < now)))
                .order_by(run.c.created_at).limit(1).with_for_update(skip_locked=True))).scalar()
            if rid:
                await s.execute(update(run).where(run.c.id == rid).values(error=None,
                    status="RUNNING", started_at=now, lease_until=now + timedelta(seconds=self.s.run_lease_s)))
        return rid

    async def process_once(self) -> int:
        async with self.sf() as s:
            health = (await load_settings(s))["health"]
        if not health.get("ok", True) and health.get("error", "").startswith("config:"):
            # paused after a setup error (e.g. rejected API key): retry one run every few minutes to notice a fix
            at = datetime.fromisoformat(health["at"]) if health.get("at") else None
            if at and datetime.now(UTC) - at < timedelta(minutes=5):
                return 0
        n = 0
        while (rid := await self._claim()) is not None:
            await self.run_ticket(rid)
            n += 1
            async with self.sf() as s:
                if not (await load_settings(s))["health"].get("ok", True):
                    break  # Claude unavailable or rejected: stop here instead of failing the whole queue
        return n

    async def run_ticket(self, run_id: UUID) -> None:
        token = request_id_var.set(f"agent-run:{run_id}")
        try:
            await self._run_ticket(run_id)
        finally:
            request_id_var.reset(token)

    async def _run_ticket(self, run_id: UUID) -> None:
        async with self.sf() as s:
            r = (await s.execute(select(run).where(run.c.id == run_id))).one()
            policies, conf = await load_policies(s), await load_settings(s)
        rec = Recorder(self.sf, run_id, self.model.name if self.model else "-")
        if conf["kill_switch"] or self.model is None:
            await rec.finish("SKIPPED", summary="AI agent is switched off (kill switch or no API key); left to humans")
            return
        try:
            t = await self.api.get(f"/tickets/{r.ticket_id}")
        except ApiError as e:
            await rec.finish("FAILED", error=f"could not read the ticket: {e}")
            return
        if t["status"] not in OPEN:
            await rec.finish("SKIPPED", summary=f"ticket is already {t['status'].lower()}")
            return
        async with self.sf() as s:
            taken = (await s.execute(select(ticket_override).where(ticket_override.c.ticket_id == r.ticket_id))).first()
        if taken:
            await rec.finish("SKIPPED", summary=f"{taken.taken_by} has taken this ticket over")
            return
        l2 = r.tier == "L2"
        ticket = {k: t[k] for k in ("id", "number", "plant_id", "status", "tier")}
        ctx = Context(api=self.api, knowledge=self.knowledge, recorder=rec, plant_id=t["plant_id"],
                      mode="l2" if l2 else "ticket", policies=policies, ticket=ticket, hub=self.mcp, docs=self.docs,
                      mcp=await self.mcp.toolset("l2" if l2 else "ticket"))
        async with self.sf() as s:
            earlier = [dict(p._mapping) for p in await s.execute(
                select(proposal).where(proposal.c.ticket_id == r.ticket_id).order_by(proposal.c.created_at))]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompts.ticket_message(t, r.trigger, earlier)}]
        try:
            res = await run_loop(model=self.model, system=self._l2_system if l2 else self._ticket_system,
                                 messages=messages, ctx=ctx, effort=self.s.effort_l2 if l2 else self.s.effort_ticket,
                                 max_steps=self.s.max_steps + (12 if l2 else 0),
                                 budget_usd=Decimal(str(conf["max_cost_per_l2_usd" if l2 else "max_cost_per_ticket_usd"])))
        except LimitReached as e:
            await self._handover(ctx, str(e))
            await rec.finish("STOPPED", outcome="HANDED_OVER", error=str(e))
            return
        except CONFIG_ERRORS as e:
            # nothing wrong with the ticket: don't touch it; pause the agent and show why on the AI support page
            log.error("Claude API rejected the request (%s): agent paused", type(e).__name__)
            await set_health(self.sf, False, f"config: {type(e).__name__}: {getattr(e, 'message', str(e))[:300]}")
            await self._requeue(run_id, rec, f"{type(e).__name__}: {e}"[:1000])
            return
        except TRANSIENT_ERRORS as e:
            log.warning("Claude API unavailable (%s): run %s retried later", type(e).__name__, run_id)
            await set_health(self.sf, False, f"transient: {type(e).__name__}")
            await self._requeue(run_id, rec, f"{type(e).__name__}: {e}"[:1000])
            return
        except Exception as e:
            log.exception("ticket run %s failed", run_id)
            await rec.step("ERROR", type(e).__name__, None, {"error": str(e)[:1000]}, is_error=True)
            await self._handover(ctx, f"the AI agent failed ({type(e).__name__})")
            await rec.finish("FAILED", outcome="HANDED_OVER", error=str(e)[:2000])
            return
        if res.stop_reason == "refusal":
            await self._handover(ctx, "the model declined to work on this ticket")
            await rec.finish("STOPPED", outcome="HANDED_OVER", summary=res.text or None, error=f"refusal: {res.refusal}")
            return
        await set_health(self.sf, True)
        await rec.finish("DONE", outcome=outcome_of(ctx.outcomes), summary=res.text[:4000] or None)

    async def _requeue(self, run_id: UUID, rec: Recorder, error: str) -> None:
        """Back to the queue (the ticket is untouched); retried after `retry_after_s`."""
        await rec.step("ERROR", "claude_api", None, {"error": error}, is_error=True)
        async with self.sf() as s, s.begin():
            await s.execute(update(run).where(run.c.id == run_id).values(
                status="QUEUED", error=error, lease_until=datetime.now(UTC) + timedelta(seconds=self.s.retry_after_s)))

    async def _handover(self, ctx: Context, reason: str) -> None:
        """The agent could not finish: make sure a human picks the ticket up."""
        t = ctx.ticket
        assert t is not None
        text = f"AI support agent stopped: {reason}. Handing over to a human engineer."
        try:
            if t["tier"] == "L1" and ctx.policies.get("escalate") != OFF and "escalate" not in ctx.outcomes:
                await ACTIONS["escalate"].run(self.api, t, {"summary": text}, None, None)
            else:
                await ACTIONS["add_note"].run(self.api, t, {"text": text}, None, None)
            await ctx.recorder.step("ACTION", "handover", {"reason": reason}, {"done": True})
            async with self.sf() as s, s.begin():
                await publish(s, EVENT_HANDOVER, str(t["id"]), {
                    "ticket_id": str(t["id"]), "number": t["number"], "plant_id": str(t["plant_id"]), "tier": t["tier"],
                    "reason": reason, "run_id": str(ctx.recorder.run_id)})
        except ApiError as e:
            await ctx.recorder.step("ERROR", "handover", {"reason": reason}, {"error": str(e)}, is_error=True)

    # ------------------------------------------------------------------------------------------ operator chat

    async def _operator(self, token: str) -> dict[str, Any]:
        try:
            me = await self.api.whoami(token)
        except ApiError as e:
            raise ChatError(401, "terminal session not valid; log in again") from e
        if not me.get("terminal_id"):
            raise ChatError(403, "the assistant chat runs on a shop-floor terminal")
        return me

    async def start_chat(self, token: str, context: dict[str, Any]) -> dict[str, Any]:
        me = await self._operator(token)
        async with self.sf() as s:
            conf = await load_settings(s)
        health = conf["health"]
        paused = not health.get("ok", True) and str(health.get("error", "")).startswith("config:")
        available = self.model is not None and not conf["kill_switch"] and not paused
        name = (me.get("display_name") or me["username"]).split(" ")[0]
        greeting = (f"Hi {name}, I'm the support assistant for line {me.get('line_code')}. What's the problem?"
                    if available else "The support assistant is not available right now. Please use the Get help form.")
        sid = new_id()
        entry = {"role": "assistant", "text": greeting, "at": datetime.now(UTC).isoformat(),
                 "choices": GREETING_CHOICES if available else []}
        plant_id = (me.get("plant_ids") or [None])[0] or await self.api.plant_id()
        context = {k: str(v)[:80] for k, v in (context or {}).items() if k in ("equipment_code", "erp_order_number", "op_no",
                                                                              "screen") and v}
        async with self.sf() as s, s.begin():
            await s.execute(chat_session.insert().values(
                id=sid, plant_id=UUID(str(plant_id)), user_id=UUID(me["id"]), user_name=me.get("display_name") or me["username"],
                terminal_id=UUID(me["terminal_id"]), terminal_code=me.get("terminal_code"), line_code=me.get("line_code"),
                context=context, status="OPEN" if available else "CLOSED", messages=[], transcript=[entry]))
        return await self.get_chat(sid)

    async def get_chat(self, session_id: UUID) -> dict[str, Any]:
        async with self.sf() as s:
            row = (await s.execute(select(chat_session).where(chat_session.c.id == session_id))).first()
        if row is None:
            raise ChatError(404, "conversation not found")
        d = dict(row._mapping)
        d.pop("messages")
        return d

    async def chat_turn(self, session_id: UUID, token: str, text: str, emit: Emit) -> None:
        me = await self._operator(token)
        text = text.strip()[:2000]
        if not text:
            raise ChatError(422, "empty message")
        async with self.sf() as s:
            row = (await s.execute(select(chat_session).where(chat_session.c.id == session_id))).first()
            policies, conf = await load_policies(s), await load_settings(s)
        if row is None:
            raise ChatError(404, "conversation not found")
        if str(row.user_id) != me["id"]:
            raise ChatError(403, "this conversation belongs to another operator")
        if row.status not in ("OPEN", "TICKET_CREATED"):  # after the ticket the operator can still add details
            raise ChatError(409, "this conversation is closed")
        if session_id in self._busy_chats:
            raise ChatError(409, "the assistant is still answering")
        if self.model is None or conf["kill_switch"]:
            raise ChatError(503, "the support assistant is switched off; please use the Get help form")
        budget = Decimal(str(conf["max_cost_per_chat_usd"])) - Decimal(row.cost_usd)
        self._busy_chats.add(session_id)
        try:
            await self._chat_turn(row, token, text, emit, policies, budget)
        finally:
            self._busy_chats.discard(session_id)

    async def _chat_turn(self, row: Any, token: str, text: str, emit: Emit, policies: dict[str, str],
                         budget: Decimal) -> None:
        rid = new_id()
        async with self.sf() as s, s.begin():
            await s.execute(run.insert().values(id=rid, kind="CHAT", trigger="CHAT", status="RUNNING", plant_id=row.plant_id,
                                                chat_session_id=row.id, started_at=datetime.now(UTC)))
        rec = Recorder(self.sf, rid, self.model.name)  # type: ignore[union-attr]
        session = {k: getattr(row, k) for k in ("id", "terminal_code", "line_code", "user_name", "context",
                                                 "ticket_id", "ticket_number")}
        ctx = Context(api=self.api, knowledge=self.knowledge, recorder=rec, plant_id=str(row.plant_id), mode="chat",
                      policies=policies, chat=session, operator_token=token, emit=emit, hub=self.mcp, docs=self.docs,
                      mcp=await self.mcp.toolset("chat"))
        history = list(row.messages)
        first = not history
        said_now = prompts.chat_first_message(session, text) if first else prompts.chat_message(text)
        if history and history[-1]["role"] == "user":
            # the last turn ended waiting for the operator (answer buttons): answer inside that same user message
            messages = history[:-1] + [{"role": "user", "content": [*history[-1]["content"],
                                                                    {"type": "text", "text": said_now}]}]
        else:
            messages = history + [{"role": "user", "content": said_now}]
        said: list[str] = []

        async def on_text(delta: str) -> None:
            said.append(delta)
            await emit({"type": "text", "delta": delta})

        now = datetime.now(UTC).isoformat()
        transcript = list(row.transcript) + [{"role": "operator", "text": text, "at": now}]
        status, error = "DONE", None
        try:
            await run_loop(model=self.model, system=self._chat_system, messages=messages, ctx=ctx,  # type: ignore[arg-type]
                           effort=self.s.effort_chat, max_steps=min(self.s.max_steps, 12), budget_usd=budget,
                           on_text=on_text)
        except (LimitReached, Exception) as e:
            if isinstance(e, CONFIG_ERRORS):
                await set_health(self.sf, False, f"config: {type(e).__name__}: {getattr(e, 'message', str(e))[:300]}")
            elif not isinstance(e, LimitReached):
                log.exception("chat turn failed")
            status, error = "FAILED", str(e)[:1000]
            messages = history  # never keep a half-finished turn (tool call without result) in the history
            said.append("\n\nSorry, I couldn't finish that. Please raise it with the Get help form or call your supervisor.")
            await emit({"type": "text", "delta": said[-1]})
        choices = next((o.split(":", 1)[1].split("|") for o in reversed(ctx.outcomes) if o.startswith("CHOICES:")), [])
        entry: dict[str, Any] = {"role": "assistant", "text": "".join(said).strip(), "at": datetime.now(UTC).isoformat(),
                                 "choices": choices}
        if session.get("ticket_number") and ("TICKET_CREATED" in ctx.outcomes or "FIXED_IN_CHAT" in ctx.outcomes):
            entry["ticket"] = session["ticket_number"]
        chat_status = ("FIXED" if "FIXED_IN_CHAT" in ctx.outcomes else
                       "TICKET_CREATED" if session.get("ticket_number") else "OPEN")
        async with self.sf() as s, s.begin():
            await s.execute(update(chat_session).where(chat_session.c.id == row.id).values(
                messages=messages, transcript=transcript + [entry], status=chat_status, last_at=datetime.now(UTC),
                cost_usd=chat_session.c.cost_usd + rec.cost, ticket_id=session.get("ticket_id"),
                ticket_number=session.get("ticket_number")))
        await rec.finish(status, outcome=outcome_of(ctx.outcomes) if status == "DONE" else None,
                         summary=entry["text"][:500] or None, error=error)
        if chat_status == "FIXED":  # a raised ticket keeps the conversation open for more details
            await emit({"type": "closed", "status": chat_status, "ticket": session.get("ticket_number")})
        await emit({"type": "done"})

    async def close_chat(self, session_id: UUID, token: str) -> None:
        me = await self._operator(token)
        async with self.sf() as s, s.begin():
            await s.execute(update(chat_session).where(chat_session.c.id == session_id,
                                                       chat_session.c.user_id == UUID(me["id"]),
                                                       chat_session.c.status == "OPEN").values(status="CLOSED"))

    # ------------------------------------------------------------------------------------------ AI proposals

    async def decide_proposal(self, proposal_id: UUID, approve: bool, token: str, name: str,
                              note: str | None = None) -> dict[str, Any]:
        """Approve (run with the approver's token: MES checks their rights and audits them) or reject a proposal."""
        async with self.sf() as s, s.begin():
            row = (await s.execute(update(proposal).where(proposal.c.id == proposal_id, proposal.c.status == "PENDING")
                                   .values(status="EXECUTING" if approve else "REJECTED", decided_by=name,
                                           decided_at=datetime.now(UTC), decision_note=note).returning(proposal))).first()
            if row is not None:  # the notifier closes the "approval waiting" alert
                await publish(s, "ai.proposal.decided", str(row.ticket_id), {
                    "proposal_id": str(row.id), "ticket_id": str(row.ticket_id), "number": row.ticket_number,
                    "decision": "APPROVED" if approve else "REJECTED", "decided_by": name})
        if row is None:
            raise ChatError(409, "this proposal was already decided")
        if not approve:
            return dict(row._mapping)
        values: dict[str, Any]
        try:
            t = await self.api.get(f"/tickets/{row.ticket_id}")
            ticket = {k: t[k] for k in ("id", "number", "plant_id", "status", "tier")}
            result = await ACTIONS[row.action].run(self.api, ticket, dict(row.args), token, name)
            values = {"status": "EXECUTED", "result": result}
        except ApiError as e:
            values = {"status": "FAILED", "error": str(e)}
        async with self.sf() as s, s.begin():
            done = (await s.execute(update(proposal).where(proposal.c.id == proposal_id).values(**values)
                                    .returning(proposal))).one()
        return dict(done._mapping)

    async def take_over(self, ticket_id: UUID, number: str, name: str, note: str | None = None) -> None:
        """A person works this ticket: the agent stops acting on it (queued runs are skipped) until handed back."""
        async with self.sf() as s, s.begin():
            await s.execute(insert(ticket_override).values(ticket_id=ticket_id, ticket_number=number, taken_by=name,
                                                           note=note).on_conflict_do_nothing(index_elements=["ticket_id"]))
            await s.execute(update(run).where(run.c.ticket_id == ticket_id, run.c.status == "QUEUED")
                            .values(status="SKIPPED", summary=f"{name} took the ticket over"))

    async def run_again(self, t: dict[str, Any]) -> UUID | None:
        """Queue a new run for a ticket (and hand it back to the AI if a person had taken it over)."""
        tid = UUID(str(t["id"]))
        async with self.sf() as s, s.begin():
            if (await s.execute(select(run.c.id).where(run.c.ticket_id == tid,
                                                       run.c.status.in_(("QUEUED", "RUNNING"))))).first():
                raise ChatError(409, "the agent is already working on this ticket")
            rid = await self.enqueue(s, ticket_id=tid, number=t["number"], plant_id=UUID(str(t["plant_id"])),
                                     trigger="MANUAL", trigger_key=None, tier=t["tier"])
            await s.execute(ticket_override.delete().where(ticket_override.c.ticket_id == tid))
        return rid

    # ------------------------------------------------------------------------------------------ engineer chat

    async def start_engineer_chat(self, sub: str, name: str, plant_id: UUID) -> dict[str, Any]:
        first = name.split(" ")[0]
        entry = {"role": "assistant", "at": datetime.now(UTC).isoformat(), "parts": [{"type": "text", "text": (
            f"Hi {first}. Ask me about support tickets, SLAs, what the AI agents did, machines or the SAP / "
            "ServiceNow / Jira interfaces. I answer with live cards, and I can prepare changes for you to confirm.")}],
            "suggestions": ENGINEER_SUGGESTIONS}
        cid = new_id()
        async with self.sf() as s, s.begin():
            await s.execute(engineer_chat.insert().values(id=cid, plant_id=plant_id, user_sub=sub, user_name=name,
                                                          messages=[], transcript=[entry]))
        return await self.get_engineer_chat(cid, sub)

    async def list_engineer_chats(self, sub: str, limit: int = 30) -> list[dict[str, Any]]:
        async with self.sf() as s:
            rows = await s.execute(select(engineer_chat.c.id, engineer_chat.c.title, engineer_chat.c.started_at,
                                          engineer_chat.c.last_at, engineer_chat.c.cost_usd)
                                   .where(engineer_chat.c.user_sub == sub, engineer_chat.c.title.is_not(None))
                                   .order_by(engineer_chat.c.last_at.desc()).limit(limit))
            return [dict(r._mapping) for r in rows]

    async def _own_engineer_chat(self, s: AsyncSession, chat_id: UUID, sub: str, lock: bool = False) -> Any:
        q = select(engineer_chat).where(engineer_chat.c.id == chat_id)
        row = (await s.execute(q.with_for_update() if lock else q)).first()
        if row is None:
            raise ChatError(404, "conversation not found")
        if row.user_sub != sub:
            raise ChatError(403, "this conversation belongs to someone else")
        return row

    async def get_engineer_chat(self, chat_id: UUID, sub: str) -> dict[str, Any]:
        async with self.sf() as s:
            d = dict((await self._own_engineer_chat(s, chat_id, sub))._mapping)
        d.pop("messages")
        return d

    async def delete_engineer_chat(self, chat_id: UUID, sub: str) -> None:
        async with self.sf() as s, s.begin():
            await self._own_engineer_chat(s, chat_id, sub, lock=True)
            await s.execute(engineer_chat.delete().where(engineer_chat.c.id == chat_id))

    async def engineer_turn(self, chat_id: UUID, sub: str, name: str, token: str, text: str, emit: Emit) -> None:
        text = text.strip()[:4000]
        if not text:
            raise ChatError(422, "empty message")
        async with self.sf() as s:
            row = await self._own_engineer_chat(s, chat_id, sub)
            policies, conf = await load_policies(s), await load_settings(s)
        if chat_id in self._busy_chats:
            raise ChatError(409, "the assistant is still answering")
        if self.model is None or conf["kill_switch"]:
            raise ChatError(503, "the AI assistant is switched off (kill switch or no API key)")
        budget = Decimal(str(conf["max_cost_per_engineer_chat_usd"])) - Decimal(row.cost_usd)
        if budget <= 0:
            raise ChatError(409, "this conversation reached its cost limit; start a new one")
        self._busy_chats.add(chat_id)
        token_rid = request_id_var.set(f"engineer-chat:{chat_id}")
        try:
            await self._engineer_turn(row, name, token, text, emit, policies, budget)
        finally:
            request_id_var.reset(token_rid)
            self._busy_chats.discard(chat_id)

    async def _engineer_turn(self, row: Any, name: str, token: str, text: str, emit: Emit, policies: dict[str, str],
                             budget: Decimal) -> None:
        rid = new_id()
        async with self.sf() as s, s.begin():
            await s.execute(run.insert().values(id=rid, kind="ENGINEER", trigger="CHAT", status="RUNNING",
                                                plant_id=row.plant_id, chat_session_id=row.id,
                                                started_at=datetime.now(UTC)))
        rec = Recorder(self.sf, rid, self.model.name)  # type: ignore[union-attr]
        parts: list[dict[str, Any]] = []

        async def collect(ev: dict[str, Any]) -> None:
            if ev["type"] == "card":
                parts.append({"type": "card", "card": ev["card"]})
            await emit(ev)

        async def on_text(delta: str) -> None:
            if parts and parts[-1]["type"] == "text":
                parts[-1]["text"] += delta
            else:
                parts.append({"type": "text", "text": delta})
            await emit({"type": "text", "delta": delta})

        ctx = Context(api=self.api.as_user(token), knowledge=self.knowledge, recorder=rec, plant_id=str(row.plant_id),
                      mode="engineer", policies=policies, emit=collect, sf=self.sf, hub=self.mcp, docs=self.docs,
                      mcp=await self.mcp.toolset("engineer"), engineer_name=name)
        decided = [p["card"] for e in row.transcript if e["role"] == "assistant" for p in e.get("parts", [])
                   if p["type"] == "card" and p["card"]["kind"] == "confirm" and p["card"]["status"] != "PENDING"
                   and not p["card"].get("reported")]
        notes = [f"[Engineer {'confirmed' if c['status'] in ('EXECUTED', 'FAILED') else 'cancelled'}] {c['label']} on "
                 f"{c['number']}: {c['status'].lower()}{' - ' + c['error'] if c.get('error') else ''}"
                 + "".join(f"; not done on {f['number']}: {f['error']}" for f in (c.get("result") or {}).get("failed", []))
                 for c in decided]
        history = list(row.messages)
        context = {"engineer": name, "plant_id": str(row.plant_id)} if not history else None
        messages = history + [{"role": "user", "content": prompts.engineer_message(text, context, notes)}]
        status, error = "DONE", None
        try:
            await run_loop(model=self.model, system=self._engineer_system, messages=messages, ctx=ctx,  # type: ignore[arg-type]
                           effort=self.s.effort_chat, max_steps=min(self.s.max_steps, 16), budget_usd=budget,
                           on_text=on_text)
        except (LimitReached, Exception) as e:
            if isinstance(e, CONFIG_ERRORS):
                await set_health(self.sf, False, f"config: {type(e).__name__}: {getattr(e, 'message', str(e))[:300]}")
            elif not isinstance(e, LimitReached):
                log.exception("engineer chat turn failed")
            status, error = "FAILED", str(e)[:1000]
            messages = history  # never keep a half-finished turn (tool call without result) in the history
            msg = ("I stopped here: this conversation reached its cost limit. Start a new conversation to continue."
                   if isinstance(e, LimitReached) else f"Sorry, I couldn't finish that ({type(e).__name__}). Try again.")
            parts.append({"type": "text", "text": msg})
            await emit({"type": "text", "delta": "\n\n" + msg})
        now = datetime.now(UTC)
        reported = {c["action_id"] for c in decided}
        async with self.sf() as s, s.begin():
            fresh = (await s.execute(select(engineer_chat.c.transcript).where(engineer_chat.c.id == row.id)
                                     .with_for_update())).scalar_one()
            for e in fresh:  # cards confirmed meanwhile keep their new status; mark the ones just reported
                for p in e.get("parts", []):
                    if p["type"] == "card" and p["card"].get("action_id") in reported:
                        p["card"]["reported"] = True
            fresh += [{"role": "engineer", "text": text, "at": now.isoformat()},
                      {"role": "assistant", "at": now.isoformat(), "parts": parts}]
            await s.execute(update(engineer_chat).where(engineer_chat.c.id == row.id).values(
                messages=messages, transcript=fresh, last_at=now, cost_usd=engineer_chat.c.cost_usd + rec.cost,
                title=row.title or text[:80]))
        summary = "".join(p["text"] for p in parts if p["type"] == "text").strip()
        await rec.finish(status, outcome="ANSWERED" if status == "DONE" else None, summary=summary[:500] or None,
                         error=error)
        await emit({"type": "done", "cost_usd": float(rec.cost)})

    async def confirm_engineer_action(self, chat_id: UUID, sub: str, name: str, token: str, action_id: str,
                                      confirm: bool) -> dict[str, Any]:
        """The engineer confirms (runs with their token) or cancels a change the assistant prepared."""
        async with self.sf() as s, s.begin():
            row = await self._own_engineer_chat(s, chat_id, sub, lock=True)
            card = next((p["card"] for e in row.transcript for p in e.get("parts", [])
                         if p["type"] == "card" and p["card"].get("action_id") == action_id), None)
            if card is None:
                raise ChatError(404, "no such change in this conversation")
            if card["status"] != "PENDING":
                raise ChatError(409, f"this change is already {card['status'].lower()}")
            card["status"] = "RUNNING" if confirm else "CANCELLED"
            await s.execute(update(engineer_chat).where(engineer_chat.c.id == chat_id).values(transcript=row.transcript))
        if confirm:
            try:
                card["result"] = await self._run_engineer_action(card, token, name)
                card["status"] = "EXECUTED"
            except (ApiError, ChatError) as e:
                card["status"], card["error"] = "FAILED", (e.message if isinstance(e, ChatError) else str(e))[:500]
        card["decided_by"], card["decided_at"] = name, datetime.now(UTC).isoformat()
        async with self.sf() as s, s.begin():
            transcript = (await s.execute(select(engineer_chat.c.transcript).where(engineer_chat.c.id == chat_id)
                                          .with_for_update())).scalar_one()
            for e in transcript:
                for p in e.get("parts", []):
                    if p["type"] == "card" and p["card"].get("action_id") == action_id:
                        p["card"] = card
            await s.execute(update(engineer_chat).where(engineer_chat.c.id == chat_id).values(transcript=transcript))
        return card

    async def _run_engineer_action(self, card: dict[str, Any], token: str, name: str) -> dict[str, Any]:
        uapi = self.api.as_user(token)
        action, args = eng.ENGINEER_ACTIONS[card["action"]], dict(card["args"])
        if not action.needs_ticket:  # a new ticket: the card then points at it
            result = await action.run(uapi, None, args, name)  # type: ignore[arg-type, misc]
            card["ticket_id"], card["number"] = result["ticket_id"], result["number"]
            return result
        if card.get("tickets"):  # the same change on several tickets, one by one
            if action.run is None:
                raise ChatError(422, f"{action.name} can't be done on several tickets at once")
            done, failed = [], []
            for ref in card["tickets"]:
                try:
                    t = await uapi.get(f"/tickets/{ref['ticket_id']}")
                    await action.run(uapi, t, args, name)
                    done.append(ref["number"])
                except ApiError as e:
                    failed.append({"number": ref["number"], "error": str(e)[:200]})
            if not done:
                raise ChatError(422, "; ".join(f"{f['number']}: {f['error']}" for f in failed)[:500])
            return {"done": done, "failed": failed}
        t = await uapi.get(f"/tickets/{card['ticket_id']}")  # as the engineer: they must be allowed to see it
        if action.run is not None:
            return await action.run(uapi, t, args, name)
        if action.name == "take_over":
            await self.take_over(UUID(t["id"]), t["number"], name, card.get("reason"))
            return {"taken_by": name}
        if action.name == "run_ai":
            return {"run_id": str(await self.run_again(t)), "status": "QUEUED"}
        p = await self.decide_proposal(UUID(args["proposal_id"]), action.name == "approve_proposal", token, name)
        if p["status"] == "FAILED":
            raise ChatError(422, f"approved, but MES refused it: {p['error']}")
        return {"proposal": p["status"]}
