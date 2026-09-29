"""Admin API for the integration monitor: message log, retry, resync, poll state."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select, text

from erp_adapter.container import Container, build_container
from erp_adapter.db import inbound_message, metadata, outbound_attempt, outbound_message, poll_state
from erp_adapter.outbound import retry_message
from erp_adapter.ports import ErpError
from erp_adapter.settings import get_settings
from mes_common.auth import AuthError, JwksTokenVerifier, Principal, TokenVerifier
from mes_common.logs import install_request_id, setup_logging
from mes_common.reset import own_tables, truncate

# the AI support agent reads the queues to diagnose tickets and retries messages when its policy allows it
READ_ROLES = {"admin", "supervisor", "ai_support_agent"}
WRITE_ROLES = {"admin", "supervisor", "ai_support_agent"}

bearer = HTTPBearer(auto_error=False)


def create_app(container: Container | None = None, verifier: TokenVerifier | None = None) -> FastAPI:
    setup_logging("erp-adapter-api")
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.c = container or build_container(settings)
        app.state.verifier = verifier or JwksTokenVerifier(
            issuer=settings.oidc_issuer, jwks_url=settings.oidc_jwks_url, audience=settings.oidc_audience
        )
        yield
        if container is None:
            await app.state.c.aclose()

    app = FastAPI(title="MES ERP Adapter", version="0.1.0", lifespan=lifespan, root_path_in_servers=False)
    install_request_id(app)

    def c_(request: Request) -> Container:
        return request.app.state.c

    async def principal(
        request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> Principal:
        if creds is None:
            raise HTTPException(401, "missing bearer token")
        try:
            return await request.app.state.verifier.verify(creds.credentials)
        except AuthError as e:
            raise HTTPException(401, f"invalid token: {e}") from e

    def require(roles: set[str]):  # type: ignore[no-untyped-def]
        async def dep(p: Annotated[Principal, Depends(principal)]) -> Principal:
            if not roles & p.roles:
                raise HTTPException(403, f"requires one of roles {sorted(roles)}")
            return p

        return dep

    Admin = Annotated[Principal, Depends(require({"admin"}))]
    Reader = Annotated[Principal, Depends(require(READ_ROLES))]
    Writer = Annotated[Principal, Depends(require(WRITE_ROLES))]
    C = Annotated[Container, Depends(c_)]

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "UP"}

    @app.get("/health/ready")
    async def ready(c: C) -> dict:
        async with c.session_factory() as s:
            await s.execute(text("SELECT 1"))
        return {"status": "UP"}

    class OutboundOut(BaseModel):
        id: UUID
        message_type: str
        erp_key: str
        status: str
        attempts: int
        max_attempts: int
        next_attempt_at: datetime
        last_error: str | None
        erp_result: dict | None
        reconciled: bool
        payload: dict
        created_at: datetime
        updated_at: datetime
        completed_at: datetime | None

    class AttemptOut(BaseModel):
        attempt_no: int
        started_at: datetime
        finished_at: datetime
        outcome: str
        error: str | None

    class OutboundDetail(OutboundOut):
        attempts_log: list[AttemptOut]

    class InboundOut(BaseModel):
        id: UUID
        erp_object: str
        erp_key: str
        erp_changed_at: datetime | None
        event_id: UUID
        received_at: datetime
        payload: dict

    class PollStateOut(BaseModel):
        name: str
        watermark: datetime
        last_run_at: datetime | None
        last_success_at: datetime | None
        last_error: str | None
        last_published: int

    @app.get("/api/v1/outbound", response_model=list[OutboundOut])
    async def list_outbound(
        c: C,
        _: Reader,
        status: Annotated[list[str] | None, Query()] = None,
        erp_key: str | None = None,
        limit: int = Query(100, le=500),
    ) -> Any:
        q = select(outbound_message).order_by(outbound_message.c.seq.desc()).limit(limit)
        if status:
            q = q.where(outbound_message.c.status.in_(status))
        if erp_key:
            q = q.where(outbound_message.c.erp_key == erp_key)
        async with c.session_factory() as s:
            return [dict(r._mapping) for r in (await s.execute(q)).all()]

    @app.get("/api/v1/outbound/{msg_id}", response_model=OutboundDetail)
    async def get_outbound(msg_id: UUID, c: C, _: Reader) -> Any:
        async with c.session_factory() as s:
            row = (await s.execute(select(outbound_message).where(outbound_message.c.id == msg_id))).first()
            if row is None:
                raise HTTPException(404, "message not found")
            attempts = (
                await s.execute(
                    select(outbound_attempt)
                    .where(outbound_attempt.c.message_id == msg_id)
                    .order_by(outbound_attempt.c.attempt_no)
                )
            ).all()
        return {**row._mapping, "attempts_log": [dict(a._mapping) for a in attempts]}

    class RetryRequest(BaseModel):
        extra_attempts: int = 3

    @app.post("/api/v1/outbound/{msg_id}/retry")
    async def retry(msg_id: UUID, c: C, _: Writer, body: RetryRequest | None = None) -> dict:
        async with c.session_factory() as s, s.begin():
            ok = await retry_message(s, msg_id, (body or RetryRequest()).extra_attempts)
        if not ok:
            raise HTTPException(409, "message is not in a retryable state")
        return {"status": "PENDING"}

    @app.get("/api/v1/outbound-stats")
    async def outbound_stats(c: C, _: Reader) -> dict[str, int]:
        async with c.session_factory() as s:
            rows = (
                await s.execute(
                    select(outbound_message.c.status, func.count()).group_by(outbound_message.c.status)
                )
            ).all()
        return {r[0]: r[1] for r in rows}

    @app.get("/api/v1/inbound", response_model=list[InboundOut])
    async def list_inbound(c: C, _: Reader, erp_key: str | None = None, limit: int = Query(100, le=500)) -> Any:
        q = select(inbound_message).order_by(inbound_message.c.received_at.desc()).limit(limit)
        if erp_key:
            q = q.where(inbound_message.c.erp_key == erp_key)
        async with c.session_factory() as s:
            return [dict(r._mapping) for r in (await s.execute(q)).all()]

    @app.get("/api/v1/poll-state", response_model=list[PollStateOut])
    async def get_poll_state(c: C, _: Reader) -> Any:
        async with c.session_factory() as s:
            return [dict(r._mapping) for r in (await s.execute(select(poll_state))).all()]

    @app.post("/api/v1/admin/reset")
    async def reset_data(c: C, _: Admin) -> dict:
        """Demo reset: forget all postings, received orders and poll positions (orders are read again from SAP)."""
        if not c.settings.allow_data_reset:
            raise HTTPException(403, "data reset is switched off on this installation (ALLOW_DATA_RESET)")
        tables = own_tables(metadata)
        async with c.session_factory() as s, s.begin():
            await truncate(s, tables)
        return {"tables": len(tables)}

    @app.post("/api/v1/poll")
    async def poll_now(c: C, _: Writer) -> dict:
        return {"published": await c.poller.poll_all()}

    @app.post("/api/v1/orders/{order_number}/resync")
    async def resync(order_number: str, c: C, _: Writer) -> dict:
        try:
            wo = await c.erp.fetch_order(order_number)
        except ErpError as e:
            raise HTTPException(502, str(e)) from e
        async with c.session_factory() as s, s.begin():
            await c.poller.publish_if_new(s, wo, force=True)
        return {"published": True, "erp_last_changed_at": wo.erp_last_changed_at}

    return app
