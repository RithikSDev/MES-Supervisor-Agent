"""Notifier API: the alert list and acknowledgement (admin UI bell and page), the on-call rota and the alert rules,
channel status and a test alert."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mes_common.auth import AuthError, JwksTokenVerifier, TokenVerifier
from mes_common.db import make_engine, make_session_factory
from mes_common.ids import new_id
from mes_common.logs import install_request_id, setup_logging
from mes_common.reset import own_tables, truncate
from notifier.alerts import OPEN, ROLES, RULES, acknowledge, ensure_defaults, open_alert
from notifier.channels import Email, Teams
from notifier.db import delivery, metadata, notification, rota, rule
from notifier.settings import Settings, get_settings

STAFF = {"admin", "supervisor", "quality_engineer", "maintenance_tech"}
ADMIN = {"admin"}
bearer = HTTPBearer(auto_error=False)


@dataclass
class Who:
    name: str
    roles: frozenset[str]


class Ack(BaseModel):
    note: str | None = None


class RotaEntry(BaseModel):
    role: str
    position: int = Field(ge=1, le=9)
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    teams: bool = True
    active: bool = True


class RuleIn(BaseModel):
    enabled: bool
    channels: list[str]
    ack_minutes: int = Field(ge=1, le=1440)


def _rows(result: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in result]


def create_app(sf: async_sessionmaker[AsyncSession] | None = None, verifier: TokenVerifier | None = None,
               settings: Settings | None = None) -> FastAPI:
    setup_logging("notifier-api")
    s_ = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        if sf is None:
            engine = make_engine(s_.database_url, pool_size=5)
        app.state.sf = sf or make_session_factory(engine)
        app.state.verifier = verifier or JwksTokenVerifier(issuer=s_.oidc_issuer, jwks_url=s_.oidc_jwks_url,
                                                           audience=s_.oidc_audience)
        await ensure_defaults(app.state.sf)
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="MES Notifier", version="0.1.0", lifespan=lifespan)
    install_request_id(app)

    async def who(request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Who:
        if creds is None:
            raise HTTPException(401, "missing bearer token")
        try:
            p = await request.app.state.verifier.verify(creds.credentials)
        except AuthError as e:
            raise HTTPException(401, f"invalid token: {e}") from e
        return Who(p.display_name or p.username, p.roles)

    def require(roles: set[str]):  # type: ignore[no-untyped-def]
        async def dep(w: Annotated[Who, Depends(who)]) -> Who:
            if not roles & w.roles:
                raise HTTPException(403, f"requires one of roles {sorted(roles)}")
            return w
        return dep

    Staff = Annotated[Who, Depends(require(STAFF))]
    Admin = Annotated[Who, Depends(require(ADMIN))]

    def sf_(request: Request) -> async_sessionmaker[AsyncSession]:
        return request.app.state.sf

    SF = Annotated[async_sessionmaker[AsyncSession], Depends(sf_)]

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "UP"}

    @app.get("/health/ready")
    async def ready(sf: SF) -> dict:
        async with sf() as s:
            await s.execute(text("SELECT 1"))
        return {"status": "UP"}

    @app.get("/api/v1/notify/notifications")
    async def notifications(sf: SF, _: Staff, open_only: bool = False, ticket_id: UUID | None = None,
                            limit: int = Query(100, le=500)) -> list:
        q = select(notification).order_by(notification.c.seq.desc()).limit(limit)
        if open_only:
            q = q.where(notification.c.status.in_(OPEN))
        if ticket_id:
            q = q.where(notification.c.ticket_id == ticket_id)
        async with sf() as s:
            return _rows(await s.execute(q))

    @app.get("/api/v1/notify/notifications/{nid}")
    async def one(nid: UUID, sf: SF, _: Staff) -> dict:
        async with sf() as s:
            n = (await s.execute(select(notification).where(notification.c.id == nid))).first()
            if n is None:
                raise HTTPException(404, "alert not found")
            ds = _rows(await s.execute(select(delivery).where(delivery.c.notification_id == nid).order_by(delivery.c.id)))
        return {**n._mapping, "deliveries": ds}

    @app.post("/api/v1/notify/notifications/{nid}/ack")
    async def ack(nid: UUID, body: Ack, sf: SF, w: Staff) -> dict:
        async with sf() as s, s.begin():
            if not await acknowledge(s, nid, w.name):
                raise HTTPException(409, "already acknowledged or closed")
        return {"status": "ACKED", "acked_by": w.name}

    @app.get("/api/v1/notify/summary")
    async def summary(sf: SF, _: Staff) -> dict:
        async with sf() as s:
            open_ = (await s.execute(select(func.count()).select_from(notification)
                                     .where(notification.c.status.in_(OPEN)))).scalar()
            escalated = (await s.execute(select(func.count()).select_from(notification)
                                         .where(notification.c.status.in_(OPEN), notification.c.level > 0))).scalar()
        return {"open": open_, "escalated": escalated,
                "channels": {"email": Email(s_).configured, "teams": Teams(s_).configured, "in_app": True}}

    # ---- rota
    @app.get("/api/v1/notify/rota")
    async def get_rota(sf: SF, _: Staff) -> list:
        async with sf() as s:
            return _rows(await s.execute(select(rota).order_by(rota.c.role, rota.c.position)))

    @app.put("/api/v1/notify/rota")
    async def put_rota(entries: list[RotaEntry], sf: SF, _: Admin) -> list:
        for e in entries:
            if e.role not in ROLES:
                raise HTTPException(422, f"role must be one of {ROLES}")
        async with sf() as s, s.begin():
            await s.execute(rota.delete())
            for e in entries:
                await s.execute(rota.insert().values(id=new_id(), **e.model_dump()))
            return _rows(await s.execute(select(rota).order_by(rota.c.role, rota.c.position)))

    # ---- rules
    @app.get("/api/v1/notify/rules")
    async def get_rules(sf: SF, _: Staff) -> list:
        async with sf() as s:
            rows = _rows(await s.execute(select(rule).order_by(rule.c.key)))
        return [{**r, "label": RULES[r["key"]].label if r["key"] in RULES else r["key"]} for r in rows]

    @app.put("/api/v1/notify/rules/{key}")
    async def put_rule(key: str, body: RuleIn, sf: SF, w: Admin) -> dict:
        if key not in RULES or not set(body.channels) <= {"email", "teams"}:
            raise HTTPException(422, "unknown rule or channel")
        async with sf() as s, s.begin():
            await s.execute(update(rule).where(rule.c.key == key).values(
                enabled=body.enabled, channels=body.channels, ack_minutes=body.ack_minutes, updated_by=w.name,
                updated_at=func.now()))
        return {"key": key, **body.model_dump()}

    @app.post("/api/v1/notify/admin/reset")
    async def reset_data(request: Request, sf: SF, _: Admin) -> dict:
        """Demo reset: all alerts and deliveries go; on-call rota and alert rules are back to the defaults."""
        settings = s_
        if not settings.allow_data_reset:
            raise HTTPException(403, "data reset is switched off on this installation (ALLOW_DATA_RESET)")
        tables = own_tables(metadata)
        async with sf() as s, s.begin():
            await truncate(s, tables)
        await ensure_defaults(sf)
        mailbox = "not configured"
        if settings.mailpit_api_url:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=5) as http:
                    r = await http.delete(f"{settings.mailpit_api_url.rstrip('/')}/api/v1/messages")
                mailbox = "emptied" if r.status_code < 300 else f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                mailbox = type(e).__name__
        return {"tables": len(tables), "demo_mailbox": mailbox}

    @app.post("/api/v1/notify/test", status_code=202)
    async def test_alert(sf: SF, w: Admin) -> dict:
        """A test alert to the L1 on call (and on up the chain if nobody acknowledges it)."""
        async with sf() as s, s.begin():
            await open_alert(s, "P1_RAISED", f"TEST:{new_id()}", {"severity": "P4"}, ["L1"],
                             "Test alert from MES", f"Sent by {w.name} to check the alert channels.", force=True)
        return {"queued": True}

    return app
