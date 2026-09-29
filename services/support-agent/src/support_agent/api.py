"""HTTP API of the support agent.

Terminal side (operator's terminal session token): start a chat, send a message (answer streamed as server-sent
events), close it. Admin side (Keycloak users): the support engineer chat (answers with cards, changes confirmed by the
engineer and run with their token), runs with every step, proposals to approve or reject, the action policy and kill
switch, counters.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

import jwt
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from mes_common.auth import AuthError, JwksTokenVerifier, TokenVerifier
from mes_common.ids import new_id
from mes_common.logs import install_request_id, setup_logging
from mes_common.reset import own_tables, truncate
from support_agent.actions import CHAT_ACTIONS, allowed_modes, label
from support_agent.agent import ChatError
from support_agent.clients import ApiError
from support_agent.container import Container, build_container
from support_agent.db import (
    chat_session,
    eval_run,
    mcp_server,
    metadata,
    policy,
    proposal,
    rating,
    run,
    setting,
    step,
    ticket_override,
)
from support_agent.settings import get_settings
from support_agent.store import ensure_defaults, load_policies, load_settings

TERMINAL_ISSUER = "mes-core:terminal"
READ_ROLES = {"admin", "supervisor"}
APPROVE_ROLES = {"admin", "supervisor"}
POLICY_ROLES = {"admin"}
bearer = HTTPBearer(auto_error=False)


@dataclass
class Caller:
    token: str
    kind: str  # user | terminal
    name: str
    roles: frozenset[str]
    user_id: str | None = None  # MES user id (terminal sessions)
    sub: str | None = None  # Keycloak subject (admin users)


class ChatStart(BaseModel):
    equipment_code: str | None = None
    erp_order_number: str | None = None
    op_no: str | None = None
    screen: str | None = None


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class EngineerChatStart(BaseModel):
    plant_id: UUID


class EngineerMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Confirm(BaseModel):
    confirm: bool


class McpServerIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,19}$")
    label: str = Field(min_length=1, max_length=60)
    description: str | None = Field(None, max_length=500)
    url: str = Field(pattern=r"^https?://\S+$", max_length=300)
    token: str | None = Field(None, max_length=1000)  # new bearer token; omitted = keep the stored one
    clear_token: bool = False
    enabled: bool = True
    modes: list[Literal["engineer", "l2", "ticket", "chat"]] = ["engineer", "l2"]


class McpTestIn(BaseModel):
    url: str = Field(pattern=r"^https?://\S+$", max_length=300)
    token: str | None = Field(None, max_length=1000)
    server_id: UUID | None = None  # test with this server's stored token


class Decision(BaseModel):
    note: str | None = Field(None, max_length=1000)


class PolicyUpdate(BaseModel):
    actions: dict[str, Literal["off", "suggest", "auto"]] = {}
    kill_switch: bool | None = None
    max_cost_per_ticket_usd: float | None = Field(None, gt=0, le=50)
    max_cost_per_chat_usd: float | None = Field(None, gt=0, le=20)
    max_cost_per_l2_usd: float | None = Field(None, gt=0, le=50)
    max_cost_per_engineer_chat_usd: float | None = Field(None, gt=0, le=20)


def _is_terminal(token: str) -> bool:
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss") == TERMINAL_ISSUER
    except jwt.PyJWTError:
        return False


def _rows(result: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in result]


def create_app(container: Container | None = None, verifier: TokenVerifier | None = None) -> FastAPI:
    setup_logging("support-agent-api")
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.c = container or build_container(settings)
        app.state.verifier = verifier or JwksTokenVerifier(
            issuer=settings.oidc_issuer, jwks_url=settings.oidc_jwks_url, audience=settings.oidc_audience)
        await ensure_defaults(app.state.c.session_factory)
        await app.state.c.agent.mcp.ensure_defaults()
        await app.state.c.agent.docs.sync_builtin()
        yield
        if container is None:
            await app.state.c.aclose()

    app = FastAPI(title="MES Support Agent", version="0.1.0", lifespan=lifespan)
    install_request_id(app)

    def c_(request: Request) -> Container:
        return request.app.state.c

    C = Annotated[Container, Depends(c_)]

    async def caller(request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Caller:
        if creds is None:
            raise HTTPException(401, "missing bearer token")
        token = creds.credentials
        if _is_terminal(token):  # MES-issued terminal session: MES itself says whether it is still valid
            try:
                me = await request.app.state.c.api.whoami(token)
            except ApiError as e:
                raise HTTPException(401, "terminal session not valid") from e
            return Caller(token, "terminal", me.get("display_name") or me["username"], frozenset(me["roles"]), me["id"])
        try:
            p = await request.app.state.verifier.verify(token)
        except AuthError as e:
            raise HTTPException(401, f"invalid token: {e}") from e
        return Caller(token, "user", p.display_name or p.username, p.roles, sub=p.subject)

    def require(roles: set[str]):  # type: ignore[no-untyped-def]
        async def dep(who: Annotated[Caller, Depends(caller)]) -> Caller:
            if who.kind != "user" or not roles & who.roles:
                raise HTTPException(403, f"requires one of roles {sorted(roles)}")
            return who
        return dep

    Who = Annotated[Caller, Depends(caller)]
    Reader = Annotated[Caller, Depends(require(READ_ROLES))]
    Approver = Annotated[Caller, Depends(require(APPROVE_ROLES))]
    PolicyAdmin = Annotated[Caller, Depends(require(POLICY_ROLES))]

    def chat_error(e: ChatError) -> HTTPException:
        return HTTPException(e.status, e.message)

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "UP"}

    @app.get("/health/ready")
    async def ready(c: C) -> dict:
        async with c.session_factory() as s:
            await s.execute(text("SELECT 1"))
        return {"status": "UP"}

    def sse(turn: Any) -> StreamingResponse:
        """Stream a chat turn as server-sent events; the turn finishes even if the client disconnects."""
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def emit(ev: dict[str, Any]) -> None:
            await queue.put(ev)

        async def work() -> None:
            try:
                await turn(emit)
            except ChatError as e:
                await queue.put({"type": "error", "status": e.status, "message": e.message})
            except Exception as e:  # the turn itself reports model/tool failures; this is a last resort
                await queue.put({"type": "error", "status": 500, "message": f"assistant failed: {type(e).__name__}"})
            finally:
                await queue.put(None)

        task = asyncio.create_task(work())

        async def events() -> AsyncIterator[str]:
            while (ev := await queue.get()) is not None:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
            await task

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------------------------------ terminal chat

    @app.post("/api/v1/agent/chat/sessions", status_code=201)
    async def start_chat(body: ChatStart, c: C, who: Who) -> dict:
        try:
            return await c.agent.start_chat(who.token, body.model_dump())
        except ChatError as e:
            raise chat_error(e) from e

    @app.get("/api/v1/agent/chat/sessions/{session_id}")
    async def get_chat(session_id: UUID, c: C, who: Who) -> dict:
        try:
            d = await c.agent.get_chat(session_id)
        except ChatError as e:
            raise chat_error(e) from e
        mine = who.kind == "terminal" and str(d["user_id"]) == who.user_id
        if not mine and not (who.kind == "user" and READ_ROLES & who.roles):
            raise HTTPException(403, "not your conversation")
        return d

    @app.post("/api/v1/agent/chat/sessions/{session_id}/messages")
    async def send(session_id: UUID, body: ChatMessage, c: C, who: Who) -> StreamingResponse:
        return sse(lambda emit: c.agent.chat_turn(session_id, who.token, body.text, emit))

    @app.post("/api/v1/agent/chat/sessions/{session_id}/close", status_code=204)
    async def close_chat(session_id: UUID, c: C, who: Who) -> None:
        try:
            await c.agent.close_chat(session_id, who.token)
        except ChatError as e:
            raise chat_error(e) from e

    @app.get("/api/v1/agent/chat/sessions")
    async def chats(c: C, _: Reader, limit: int = Query(50, le=200)) -> list:
        async with c.session_factory() as s:
            rows = _rows(await s.execute(select(chat_session).order_by(chat_session.c.started_at.desc()).limit(limit)))
        for r in rows:
            r.pop("messages")
        return rows

    # ------------------------------------------------------------------------------------------ engineer chat

    @app.post("/api/v1/agent/engineer/chats", status_code=201)
    async def start_engineer_chat(body: EngineerChatStart, c: C, who: Reader) -> dict:
        return await c.agent.start_engineer_chat(who.sub or who.name, who.name, body.plant_id)

    @app.get("/api/v1/agent/engineer/chats")
    async def engineer_chats(c: C, who: Reader, limit: int = Query(30, le=100)) -> list:
        return await c.agent.list_engineer_chats(who.sub or who.name, limit)

    @app.get("/api/v1/agent/engineer/chats/{chat_id}")
    async def engineer_chat_detail(chat_id: UUID, c: C, who: Reader) -> dict:
        try:
            return await c.agent.get_engineer_chat(chat_id, who.sub or who.name)
        except ChatError as e:
            raise chat_error(e) from e

    @app.delete("/api/v1/agent/engineer/chats/{chat_id}", status_code=204)
    async def delete_engineer_chat(chat_id: UUID, c: C, who: Reader) -> None:
        try:
            await c.agent.delete_engineer_chat(chat_id, who.sub or who.name)
        except ChatError as e:
            raise chat_error(e) from e

    @app.post("/api/v1/agent/engineer/chats/{chat_id}/messages")
    async def engineer_send(chat_id: UUID, body: EngineerMessage, c: C, who: Reader) -> StreamingResponse:
        return sse(lambda emit: c.agent.engineer_turn(chat_id, who.sub or who.name, who.name, who.token, body.text, emit))

    @app.post("/api/v1/agent/engineer/chats/{chat_id}/actions/{action_id}")
    async def engineer_confirm(chat_id: UUID, action_id: str, body: Confirm, c: C, who: Reader) -> dict:
        """Confirm (runs now, with the engineer's own rights) or cancel a change the assistant prepared."""
        try:
            return await c.agent.confirm_engineer_action(chat_id, who.sub or who.name, who.name, who.token, action_id,
                                                         body.confirm)
        except ChatError as e:
            raise chat_error(e) from e

    # ------------------------------------------------------------------------------------------ runs

    @app.get("/api/v1/agent/runs")
    async def runs(c: C, _: Reader, kind: str | None = None, ticket_id: UUID | None = None,
                   limit: int = Query(100, le=500)) -> list:
        q = select(run).order_by(run.c.created_at.desc()).limit(limit)
        if kind:
            q = q.where(run.c.kind == kind)
        if ticket_id:
            q = q.where(run.c.ticket_id == ticket_id)
        async with c.session_factory() as s:
            return _rows(await s.execute(q))

    @app.get("/api/v1/agent/runs/{run_id}")
    async def run_detail(run_id: UUID, c: C, _: Reader) -> dict:
        async with c.session_factory() as s:
            r = (await s.execute(select(run).where(run.c.id == run_id))).first()
            if r is None:
                raise HTTPException(404, "run not found")
            steps = _rows(await s.execute(select(step).where(step.c.run_id == run_id).order_by(step.c.seq)))
            props = _rows(await s.execute(select(proposal).where(proposal.c.run_id == run_id)))
            rated = (await s.execute(select(rating).where(rating.c.run_id == run_id))).first()
        return {**r._mapping, "trail": steps, "proposals": props, "rating": dict(rated._mapping) if rated else None}

    @app.post("/api/v1/agent/tickets/{ticket_id}/run", status_code=202)
    async def rerun(ticket_id: UUID, c: C, who: Approver) -> dict:
        try:
            t = await c.api.get(f"/tickets/{ticket_id}")
        except ApiError as e:
            raise HTTPException(e.status, e.message) from e
        try:
            rid = await c.agent.run_again(t)  # also hands a taken-over ticket back
        except ChatError as e:
            raise chat_error(e) from e
        return {"run_id": str(rid), "status": "QUEUED"}

    @app.get("/api/v1/agent/tickets/{ticket_id}")
    async def for_ticket(ticket_id: UUID, c: C, _: Reader) -> dict:
        async with c.session_factory() as s:
            runs_ = _rows(await s.execute(select(run).where(run.c.ticket_id == ticket_id).order_by(run.c.created_at.desc())))
            props = _rows(await s.execute(select(proposal).where(proposal.c.ticket_id == ticket_id)
                                          .order_by(proposal.c.created_at.desc())))
            chat = (await s.execute(select(chat_session.c.id, chat_session.c.transcript)
                                    .where(chat_session.c.ticket_id == ticket_id))).first()
            taken = (await s.execute(select(ticket_override).where(ticket_override.c.ticket_id == ticket_id))).first()
        return {"runs": runs_, "proposals": props, "taken_over": dict(taken._mapping) if taken else None,
                "chat": {"id": chat.id, "transcript": chat.transcript} if chat else None}

    class TakeOver(BaseModel):
        note: str | None = Field(None, max_length=500)

    @app.post("/api/v1/agent/tickets/{ticket_id}/takeover", status_code=201)
    async def take_over(ticket_id: UUID, body: TakeOver, c: C, who: Approver) -> dict:
        """A person works this ticket: the agent stops acting on it (queued runs are skipped) until handed back."""
        t = await c.api.get(f"/tickets/{ticket_id}")
        await c.agent.take_over(ticket_id, t["number"], who.name, body.note)
        return {"taken_by": who.name}

    @app.delete("/api/v1/agent/tickets/{ticket_id}/takeover", status_code=204)
    async def hand_back(ticket_id: UUID, c: C, who: Approver) -> None:
        async with c.session_factory() as s, s.begin():
            await s.execute(ticket_override.delete().where(ticket_override.c.ticket_id == ticket_id))

    # ------------------------------------------------------------------------------------------ proposals

    @app.get("/api/v1/agent/proposals")
    async def proposals(c: C, _: Reader, status: Annotated[list[str] | None, Query()] = None,
                        limit: int = Query(100, le=500)) -> list:
        q = select(proposal).order_by(proposal.c.created_at.desc()).limit(limit)
        if status:
            q = q.where(proposal.c.status.in_(status))
        async with c.session_factory() as s:
            return _rows(await s.execute(q))

    @app.post("/api/v1/agent/proposals/{proposal_id}/approve")
    async def approve(proposal_id: UUID, body: Decision, c: C, who: Approver) -> dict:
        # runs with the approver's own token: MES checks this person may do it and records them in the audit trail
        try:
            return await c.agent.decide_proposal(proposal_id, True, who.token, who.name, body.note)
        except ChatError as e:
            raise chat_error(e) from e

    @app.post("/api/v1/agent/proposals/{proposal_id}/reject")
    async def reject(proposal_id: UUID, body: Decision, c: C, who: Approver) -> dict:
        try:
            return await c.agent.decide_proposal(proposal_id, False, who.token, who.name, body.note)
        except ChatError as e:
            raise chat_error(e) from e

    class RatingIn(BaseModel):
        score: Literal[1, -1]
        comment: str | None = Field(None, max_length=1000)

    @app.put("/api/v1/agent/runs/{run_id}/rating")
    async def rate(run_id: UUID, body: RatingIn, c: C, who: Approver) -> dict:
        """A supervisor's verdict on a run: helpful (+1) or wrong (-1), with an optional comment."""
        async with c.session_factory() as s, s.begin():
            await s.execute(insert(rating).values(run_id=run_id, score=body.score, comment=body.comment, rated_by=who.name)
                            .on_conflict_do_update(index_elements=["run_id"], set_={
                                "score": body.score, "comment": body.comment, "rated_by": who.name, "rated_at": func.now()}))
        return {"run_id": str(run_id), "score": body.score}

    @app.get("/api/v1/agent/analytics")
    async def agent_analytics(c: C, _: Reader, days: int = Query(30, ge=1, le=365)) -> dict:
        """How the AI support engineers performed: runs and outcomes per tier, proposals and what people decided,
        chats, cost, speed, supervisors' ratings."""
        since = datetime.now(UTC) - timedelta(days=days)
        async with c.session_factory() as s:
            runs_ = (await s.execute(select(run.c.kind, run.c.tier, run.c.outcome, run.c.status, func.count(),
                                            func.coalesce(func.sum(run.c.cost_usd), 0),
                                            func.avg(func.extract("epoch", run.c.finished_at - run.c.created_at)))
                                     .where(run.c.created_at >= since).group_by(run.c.kind, run.c.tier, run.c.outcome,
                                                                               run.c.status))).all()
            props = (await s.execute(select(proposal.c.action, proposal.c.status, func.count())
                                     .where(proposal.c.created_at >= since)
                                     .group_by(proposal.c.action, proposal.c.status))).all()
            wait_min = (await s.execute(select(func.avg(func.extract("epoch", proposal.c.decided_at - proposal.c.created_at)))
                                        .where(proposal.c.created_at >= since, proposal.c.decided_at.is_not(None)))).scalar()
            chats = dict((await s.execute(select(chat_session.c.status, func.count())
                                          .where(chat_session.c.started_at >= since).group_by(chat_session.c.status))).all())
            ratings = dict((await s.execute(select(rating.c.score, func.count()).where(rating.c.rated_at >= since)
                                            .group_by(rating.c.score))).all())
            per_day = (await s.execute(select(func.date_trunc("day", run.c.created_at).label("d"), run.c.kind,
                                              func.coalesce(func.sum(run.c.cost_usd), 0), func.count())
                                       .where(run.c.created_at >= since).group_by("d", run.c.kind).order_by("d"))).all()
            tickets = (await s.execute(select(func.count(func.distinct(run.c.ticket_id)))
                                       .where(run.c.kind == "TICKET", run.c.created_at >= since))).scalar()
        ticket_cost = sum(float(r[5]) for r in runs_ if r[0] == "TICKET")
        return {
            "days": days,
            "runs": [{"kind": r[0], "tier": r[1], "outcome": r[2], "status": r[3], "count": r[4], "cost_usd": float(r[5]),
                      "avg_seconds": round(float(r[6]), 1) if r[6] is not None else None} for r in runs_],
            "proposals": [{"action": p[0], "status": p[1], "count": p[2]} for p in props],
            "proposal_wait_min": round(float(wait_min) / 60, 1) if wait_min else None,
            "chats": chats,
            "ratings": {"helpful": ratings.get(1, 0), "wrong": ratings.get(-1, 0)},
            "cost_per_day": [{"day": d[0].date().isoformat(), "kind": d[1], "cost_usd": float(d[2]), "runs": d[3]}
                             for d in per_day],
            "tickets_worked": tickets,
            "cost_per_ticket_usd": round(ticket_cost / tickets, 3) if tickets else None,
        }

    @app.get("/api/v1/agent/evals")
    async def evals(c: C, _: Reader, limit: int = Query(20, le=100)) -> list:
        """Evaluation runs (python -m support_agent.evals), newest first, with every scenario's checks."""
        async with c.session_factory() as s:
            return _rows(await s.execute(select(eval_run).order_by(eval_run.c.started_at.desc()).limit(limit)))

    @app.post("/api/v1/agent/admin/reset")
    async def reset_data(c: C, _: PolicyAdmin) -> dict:
        """Demo reset: all AI runs, chats and proposals go; policy, cost limits, MCP servers and knowledge are back to
        the defaults (built-in documents only)."""
        if not c.settings.allow_data_reset:
            raise HTTPException(403, "data reset is switched off on this installation (ALLOW_DATA_RESET)")
        tables = own_tables(metadata)
        async with c.session_factory() as s, s.begin():
            await truncate(s, tables)
        c.agent._busy_chats.clear()
        await ensure_defaults(c.session_factory)
        await c.agent.mcp.ensure_defaults()
        await c.agent.docs.sync_builtin()
        return {"tables": len(tables)}

    # ------------------------------------------------------------------------------------------ knowledge

    @app.get("/api/v1/agent/knowledge")
    async def knowledge_docs(c: C, _: Reader) -> list:
        return await c.agent.docs.documents()

    @app.post("/api/v1/agent/knowledge", status_code=201)
    async def upload_knowledge(c: C, who: PolicyAdmin, file: Annotated[UploadFile, File()],
                               title: Annotated[str, Form(min_length=1, max_length=200)],
                               kind: Annotated[str, Form()] = "MANUAL",
                               equipment_type: Annotated[str | None, Form(max_length=40)] = None) -> dict:
        """Add an OEM manual (PDF), a fault-code list (CSV: code, text, cause, remedy, ...) or a note (text/markdown)."""
        data = await file.read()
        try:
            return await c.agent.docs.upload(filename=file.filename or "upload", data=data, title=title, kind=kind,
                                             equipment_type=equipment_type, by=who.name)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.delete("/api/v1/agent/knowledge/{doc_id}", status_code=204)
    async def delete_knowledge(doc_id: UUID, c: C, _: PolicyAdmin) -> None:
        if not await c.agent.docs.remove(doc_id):
            raise HTTPException(409, "built-in documents can't be removed (edit the file under knowledge/)")

    @app.get("/api/v1/agent/knowledge/search")
    async def search_knowledge(c: C, _: Reader, q: str = Query(min_length=2, max_length=300)) -> list:
        return await c.agent.docs.search(q, limit=10)

    # ------------------------------------------------------------------------------------------ MCP servers

    def _mcp_view(r: Any) -> dict[str, Any]:
        d = dict(r._mapping)
        d["token_set"] = bool(d.pop("token"))
        return d

    @app.get("/api/v1/agent/mcp-servers")
    async def mcp_servers(c: C, _: Reader) -> dict:
        hub = c.agent.mcp
        return {"vault_ready": hub.vault.ready, "vault_error": hub.vault.error,
                "servers": [_mcp_view(r) for r in await hub.rows()]}

    @app.post("/api/v1/agent/mcp-servers/test")
    async def test_mcp(body: McpTestIn, c: C, _: PolicyAdmin) -> dict:
        """List the tools of an MCP server (nothing is saved): shows the address and token work."""
        hub, token = c.agent.mcp, body.token or ""
        if not token and body.server_id:
            row = next((r for r in await hub.rows() if r.id == body.server_id), None)
            try:
                token = hub.vault.open(row.token) if row else ""
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        try:
            tools = await hub.discover(body.url, token)
        except Exception as e:  # noqa: BLE001 - reported to the admin
            from support_agent.mcp_client import _error_text
            return {"ok": False, "error": _error_text(e), "tools": []}
        return {"ok": True, "error": None, "tools": [{"name": t["name"], "description": t["description"]} for t in tools]}

    async def _save_mcp(c: Container, body: McpServerIn, who: Caller, server_id: UUID | None) -> dict:
        hub = c.agent.mcp
        if body.token and not hub.vault.ready:
            raise HTTPException(409, hub.vault.error or "no encryption key")
        values: dict[str, Any] = {"name": body.name, "label": body.label.strip(), "description": body.description,
                                  "url": body.url.strip(), "enabled": body.enabled, "modes": sorted(set(body.modes)),
                                  "updated_by": who.name, "updated_at": func.now()}
        if body.token:
            values["token"] = hub.vault.seal(body.token)
        elif body.clear_token:
            values["token"] = None
        try:
            async with c.session_factory() as s, s.begin():
                if server_id is None:
                    row = (await s.execute(insert(mcp_server).values(id=new_id(), **values).returning(mcp_server))).one()
                else:
                    row = (await s.execute(update(mcp_server).where(mcp_server.c.id == server_id)
                                           .values(**values, version=mcp_server.c.version + 1)
                                           .returning(mcp_server))).first()
                    if row is None:
                        raise HTTPException(404, "MCP server not found")
        except IntegrityError as e:
            raise HTTPException(409, f"an MCP server named '{body.name}' exists already") from e
        return _mcp_view(row)

    @app.post("/api/v1/agent/mcp-servers", status_code=201)
    async def add_mcp(body: McpServerIn, c: C, who: PolicyAdmin) -> dict:
        return await _save_mcp(c, body, who, None)

    @app.put("/api/v1/agent/mcp-servers/{server_id}")
    async def edit_mcp(server_id: UUID, body: McpServerIn, c: C, who: PolicyAdmin) -> dict:
        return await _save_mcp(c, body, who, server_id)

    @app.delete("/api/v1/agent/mcp-servers/{server_id}", status_code=204)
    async def delete_mcp(server_id: UUID, c: C, _: PolicyAdmin) -> None:
        async with c.session_factory() as s, s.begin():
            await s.execute(mcp_server.delete().where(mcp_server.c.id == server_id))

    # ------------------------------------------------------------------------------------------ policy, status

    async def _policy_view(c: Container) -> dict[str, Any]:
        async with c.session_factory() as s:
            modes = await load_policies(s)
            conf = await load_settings(s)
            meta = {r.action: (r.updated_by, r.updated_at) for r in await s.execute(select(policy))}
        return {
            "actions": [{"action": a, "label": label(a), "mode": m, "modes": list(allowed_modes(a)),
                         "scope": "chat" if a in CHAT_ACTIONS else "ticket",
                         "updated_by": meta.get(a, (None, None))[0], "updated_at": meta.get(a, (None, None))[1]}
                        for a, m in modes.items()],
            **{k: conf[k] for k in ("kill_switch", "max_cost_per_ticket_usd", "max_cost_per_chat_usd", "max_cost_per_l2_usd",
                  "max_cost_per_engineer_chat_usd")},
        }

    @app.get("/api/v1/agent/policies")
    async def get_policies(c: C, _: Reader) -> dict:
        return await _policy_view(c)

    @app.put("/api/v1/agent/policies")
    async def put_policies(body: PolicyUpdate, c: C, who: PolicyAdmin) -> dict:
        for action, mode in body.actions.items():
            if mode not in allowed_modes(action):
                raise HTTPException(422, f"'{mode}' is not a mode of {action}")
        async with c.session_factory() as s, s.begin():
            for action, mode in body.actions.items():
                await s.execute(update(policy).where(policy.c.action == action)
                                .values(mode=mode, updated_by=who.name, updated_at=func.now()))
            for key in ("kill_switch", "max_cost_per_ticket_usd", "max_cost_per_chat_usd", "max_cost_per_l2_usd",
                  "max_cost_per_engineer_chat_usd"):
                value = getattr(body, key)
                if value is not None:
                    await s.execute(update(setting).where(setting.c.key == key)
                                    .values(value=value, updated_by=who.name, updated_at=func.now()))
        return await _policy_view(c)

    @app.get("/api/v1/agent/status")
    async def status(c: C, _: Reader) -> dict:
        since = datetime.now(UTC) - timedelta(days=1)
        async with c.session_factory() as s:
            conf = await load_settings(s)
            ticket_runs = dict((await s.execute(select(run.c.outcome, func.count()).where(
                run.c.kind == "TICKET", run.c.created_at >= since, run.c.status.in_(("DONE", "STOPPED", "FAILED")))
                .group_by(run.c.outcome))).all())
            by_status = dict((await s.execute(select(run.c.status, func.count()).where(
                run.c.kind == "TICKET", run.c.created_at >= since).group_by(run.c.status))).all())
            chats = dict((await s.execute(select(chat_session.c.status, func.count()).where(
                chat_session.c.started_at >= since).group_by(chat_session.c.status))).all())
            cost = (await s.execute(select(func.coalesce(func.sum(run.c.cost_usd), 0)).where(run.c.created_at >= since))).scalar()
            pending = (await s.execute(select(func.count()).select_from(proposal).where(proposal.c.status == "PENDING"))).scalar()
            avg_s = (await s.execute(select(func.avg(func.extract("epoch", run.c.finished_at - run.c.created_at))).where(
                run.c.kind == "TICKET", run.c.status == "DONE", run.c.created_at >= since))).scalar()
        return {
            "health": conf["health"],
            "model": c.settings.agent_model, "key_configured": c.agent.model is not None, "kill_switch": conf["kill_switch"],
            "active": c.agent.model is not None and not conf["kill_switch"],
            "last_24h": {"ticket_outcomes": ticket_runs, "ticket_runs": by_status, "chats": chats,
                         "cost_usd": float(cost or 0), "avg_ticket_minutes": round(float(avg_s) / 60, 1) if avg_s else None},
            "pending_proposals": pending,
        }

    return app
