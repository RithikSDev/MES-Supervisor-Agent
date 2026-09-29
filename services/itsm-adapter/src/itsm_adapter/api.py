"""Admin API for the ITSM integration: ticket sync log (with the REST / MCP calls), retry, poll state, connection
health, and the ServiceNow / Jira connections themselves (admins set them up here; secrets are write-only)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from itsm_adapter.connections import (
    JIRA_OPTIONS,
    SN_OPTIONS,
    SYSTEMS,
    build_jira,
    build_servicenow,
    check_jira,
    check_servicenow,
    defaults,
    host_of,
    validate_url,
)
from itsm_adapter.container import Container, build_container
from itsm_adapter.db import (
    connection,
    connection_change,
    metadata,
    outbound_attempt,
    outbound_message,
    poll_state,
)
from itsm_adapter.errors import ItsmError
from itsm_adapter.settings import get_settings
from itsm_adapter.sync import retry_message
from mes_common.auth import AuthError, JwksTokenVerifier, Principal, TokenVerifier
from mes_common.logs import install_request_id, setup_logging
from mes_common.reset import own_tables, truncate

# the AI support agent reads the queues to diagnose tickets and retries messages when its policy allows it
READ_ROLES = {"admin", "supervisor", "ai_support_agent"}
WRITE_ROLES = {"admin", "supervisor", "ai_support_agent"}
CONFIG_ROLES = {"admin"}  # only admins set up the ServiceNow / Jira connections
bearer = HTTPBearer(auto_error=False)


class MessageOut(BaseModel):
    id: UUID
    ticket_id: UUID
    ticket_number: str
    event_type: str
    changes: list[str]
    origin: str | None
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    completed_at: datetime | None
    next_attempt_at: datetime


class AttemptOut(BaseModel):
    attempt_no: int
    started_at: datetime
    finished_at: datetime
    outcome: str
    error: str | None
    calls: list[dict[str, Any]]


class MessageDetail(MessageOut):
    payload: dict[str, Any]
    attempts_log: list[AttemptOut]


class PollOut(BaseModel):
    name: str
    watermark: datetime
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    last_published: int


class ConnectionOut(BaseModel):
    system: str
    mode: str
    url: str
    configured: bool
    reachable: bool
    detail: list[str]
    error: str | None


class StatusOut(BaseModel):
    mode: str
    counts: dict[str, int]
    polls: list[PollOut]
    connections: list[ConnectionOut]


class ConnectionIn(BaseModel):
    url: str = Field(min_length=1, max_length=300)
    username: str = Field(min_length=1, max_length=200)  # ServiceNow user / Jira account e-mail
    secret: str | None = Field(None, max_length=500)  # new password / API token; omitted = keep the stored one
    enabled: bool = True
    options: dict[str, str] = {}


class TestIn(ConnectionIn):
    create_groups: bool = False


class TeamUser(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(None, max_length=200)
    tier: str | None = None


class TeamIn(BaseModel):
    users: list[TeamUser] = Field(max_length=100)
    create: bool = False


def _connection_view(system: str, row: Any, c: Container) -> dict[str, Any]:
    keys = SN_OPTIONS if system == "servicenow" else JIRA_OPTIONS
    opts = {**defaults(system, c.settings), **(row.options if row else {})}
    if row is None:
        return {"system": system, "configured": False, "url": "", "username": "", "secret_set": False, "enabled": False,
                "options": {k: opts.get(k, "") for k in keys}}
    return {"system": system, "configured": True, "url": row.url, "username": row.username,
            "secret_set": bool(row.secret), "secret_set_at": row.secret_set_at, "secret_set_by": row.secret_set_by,
            "enabled": row.enabled, "version": row.version, "updated_at": row.updated_at, "updated_by": row.updated_by,
            "options": {k: opts.get(k, "") for k in keys}, "error": c.registry.error.get(system)}


def _row(r: Any) -> dict[str, Any]:
    d = dict(r._mapping)
    d["changes"] = d["payload"].get("changes") or []
    d["origin"] = d["payload"].get("origin")
    return d


def create_app(container: Container | None = None, verifier: TokenVerifier | None = None) -> FastAPI:
    setup_logging("itsm-adapter-api")
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.c = container or build_container(settings)
        await app.state.c.registry.import_env_once()
        await app.state.c.registry.refresh(force=True)
        app.state.verifier = verifier or JwksTokenVerifier(
            issuer=settings.oidc_issuer, jwks_url=settings.oidc_jwks_url, audience=settings.oidc_audience
        )
        yield
        if container is None:
            await app.state.c.aclose()

    app = FastAPI(title="MES ITSM Adapter", version="0.1.0", lifespan=lifespan, root_path_in_servers=False)
    install_request_id(app)

    async def principal(request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Principal:
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

    Reader = Annotated[Principal, Depends(require(READ_ROLES))]
    Writer = Annotated[Principal, Depends(require(WRITE_ROLES))]

    def c_(request: Request) -> Container:
        return request.app.state.c

    C = Annotated[Container, Depends(c_)]

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "UP"}

    @app.get("/health/ready")
    async def ready(c: C) -> dict:
        async with c.session_factory() as s:
            await s.execute(text("SELECT 1"))
        return {"status": "UP"}

    @app.get("/api/v1/itsm/messages", response_model=list[MessageOut])
    async def messages(  # type: ignore[no-untyped-def]
        c: C, _: Reader, status: Annotated[list[str] | None, Query()] = None, ticket_id: UUID | None = None,
        limit: int = Query(100, le=500),
    ) -> list:
        q = select(outbound_message).order_by(outbound_message.c.seq.desc()).limit(limit)
        if status:
            q = q.where(outbound_message.c.status.in_(status))
        if ticket_id:
            q = q.where(outbound_message.c.ticket_id == ticket_id)
        async with c.session_factory() as s:
            return [_row(r) for r in (await s.execute(q)).all()]

    @app.get("/api/v1/itsm/messages/{msg_id}", response_model=MessageDetail)
    async def message(msg_id: UUID, c: C, _: Reader) -> dict:
        async with c.session_factory() as s:
            row = (await s.execute(select(outbound_message).where(outbound_message.c.id == msg_id))).first()
            if row is None:
                raise HTTPException(404, "message not found")
            attempts = (
                await s.execute(select(outbound_attempt).where(outbound_attempt.c.message_id == msg_id)
                                .order_by(outbound_attempt.c.attempt_no))
            ).all()
        return {**_row(row), "attempts_log": [{**dict(a._mapping), "calls": a.calls or []} for a in attempts]}

    @app.post("/api/v1/itsm/messages/{msg_id}/retry", status_code=202)
    async def retry(msg_id: UUID, c: C, _: Writer) -> dict:
        async with c.session_factory() as s, s.begin():
            ok = await retry_message(s, msg_id)
        if not ok:
            raise HTTPException(409, "only FAILED, DEAD or waiting messages can be retried")
        return {"status": "PENDING"}

    @app.post("/api/v1/itsm/poll", status_code=202)
    async def poll_now(c: C, _: Writer) -> dict:
        return {"published": await c.poller.poll_all()}

    Admin = Annotated[Principal, Depends(require(CONFIG_ROLES))]

    def _clean(system: str, body: ConnectionIn) -> tuple[str, dict[str, str]]:
        if system not in SYSTEMS:
            raise HTTPException(404, "unknown system (servicenow | jira)")
        url = body.url.strip().rstrip("/")
        if problem := validate_url(system, url, c_settings.itsm_allow_any_host):
            raise HTTPException(422, problem)
        keys = SN_OPTIONS if system == "servicenow" else JIRA_OPTIONS
        opts = {k: (body.options.get(k) or "").strip()[:200] for k in keys}
        required = ("group_l1", "group_l2", "group_l3") if system == "servicenow" else ("project", "issue_type_bug", "issue_type_task")
        if missing := [k for k in required if not opts[k]]:
            raise HTTPException(422, f"missing {', '.join(missing)}")
        return url, opts

    c_settings = settings

    @app.get("/api/v1/itsm/connections")
    async def connections(c: C, _: Reader) -> dict:
        rows = await c.registry.load()
        async with c.session_factory() as s:
            history = [dict(r._mapping) for r in (await s.execute(
                select(connection_change).order_by(connection_change.c.at.desc()).limit(30))).all()]
        return {"mode": c.settings.itsm_mode, "managed_here": c.registry.dynamic, "vault_ready": c.registry.vault.ready,
                "vault_error": c.registry.vault.error, "allow_any_host": c.settings.itsm_allow_any_host,
                "servicenow": _connection_view("servicenow", rows.get("servicenow"), c),
                "jira": _connection_view("jira", rows.get("jira"), c), "history": history}

    async def _secret(c: Container, system: str, body: ConnectionIn) -> str:
        if body.secret:
            return body.secret
        row = (await c.registry.load()).get(system)
        if row is None or not row.secret:
            raise HTTPException(422, "enter the password" if system == "servicenow" else "enter the API token")
        try:
            return c.registry.vault.open(row.secret)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/v1/itsm/connections/{system}/test")
    async def test_connection(system: str, body: TestIn, c: C, _: Admin) -> dict:
        """Try the given settings (the stored secret unless a new one is given) without saving them."""
        url, opts = _clean(system, body)
        secret = await _secret(c, system, body)
        if system == "servicenow":
            port = build_servicenow(url, body.username.strip(), secret, opts, c.settings, c.registry._http("servicenow"))
            groups = {"L1": opts["group_l1"], "L2": opts["group_l2"], "L3": opts["group_l3"]}
            checks = await check_servicenow(port, groups, create_groups=body.create_groups)
        else:
            port = build_jira(url, body.username.strip(), secret, opts, c.settings, c.registry._http("jira"))  # type: ignore[assignment]
            checks = await check_jira(port, [opts["issue_type_bug"], opts["issue_type_task"]])  # type: ignore[arg-type]
        if c.registry._own_http:
            await port.aclose()
        return {"ok": all(x["ok"] is not False for x in checks), "checks": checks}

    @app.put("/api/v1/itsm/connections/{system}")
    async def save_connection(system: str, body: ConnectionIn, c: C, who: Admin) -> dict:
        url, opts = _clean(system, body)
        if body.secret and not c.registry.vault.ready:
            raise HTTPException(409, c.registry.vault.error or "no encryption key")
        name = who.display_name or who.username
        rows = await c.registry.load()
        old = rows.get(system)
        if old is None and not body.secret:
            raise HTTPException(422, "enter the password" if system == "servicenow" else "enter the API token")
        values: dict[str, Any] = {"url": url, "username": body.username.strip(), "options": opts, "enabled": body.enabled,
                                  "updated_by": name, "updated_at": func.now()}
        if body.secret:
            values |= {"secret": c.registry.vault.seal(body.secret), "secret_set_at": func.now(), "secret_set_by": name}
        changed = ["url", "username", "secret", "enabled", *[f"options.{k}" for k in opts]] if old is None else [
            *[k for k in ("url", "username", "enabled") if getattr(old, k) != values[k]],
            *(["secret"] if body.secret else []),
            *[f"options.{k}" for k, v in opts.items() if (old.options or {}).get(k) != v]]
        moved = old is not None and host_of(old.url) != host_of(url)
        async with c.session_factory() as s, s.begin():
            if old is None:
                await s.execute(insert(connection).values(system=system, **values))
            elif changed:
                await s.execute(update(connection).where(connection.c.system == system)
                                .values(**values, version=connection.c.version + 1))
            if changed:
                await s.execute(connection_change.insert().values(
                    system=system, by=name, changes=changed,
                    note=f"instance changed from {host_of(old.url)} to {host_of(url)}: tickets created on the old one "
                         "stay there" if moved and old is not None else None))
            if moved or old is None:  # changes on the new instance are read from now on
                await s.execute(insert(poll_state).values(name=system, watermark=func.now())
                                .on_conflict_do_update(index_elements=["name"], set_={"watermark": func.now(),
                                                                                       "last_error": None}))
        await c.registry.refresh(force=True)
        return _connection_view(system, (await c.registry.load()).get(system), c)

    @app.post("/api/v1/itsm/servicenow/users")
    async def servicenow_users(body: TeamIn, c: C, _: Admin) -> dict:
        """Which MES support engineers exist as ServiceNow users (matched by e-mail, then user name) and can be picked
        as "Assigned to" (role itil, member of their tier's assignment group). create=true creates missing users and
        adds a missing itil role / group membership."""
        await c.registry.refresh()
        sn = c.registry.sn
        if not sn.configured or not hasattr(sn, "ensure_user"):
            raise HTTPException(409, "ServiceNow is not connected over REST")
        groups = c.registry.cfg.groups
        out = []
        for u in body.users:
            try:
                group = groups.get(u.tier) if u.tier else None
                state = await sn.ensure_user(u.username, u.name, u.email, group, create=body.create)
                out.append({"username": u.username, "name": u.name, "group": group, **state})
            except ItsmError as e:
                out.append({"username": u.username, "name": u.name, "status": "error", "error": str(e)[:300]})
        return {"users": out}

    @app.post("/api/v1/itsm/admin/reset")
    async def reset_data(c: C, _: Admin) -> dict:
        """Demo reset: forget every ticket sync; the connections stay and only new ServiceNow / Jira changes are read."""
        if not c.settings.allow_data_reset:
            raise HTTPException(403, "data reset is switched off on this installation (ALLOW_DATA_RESET)")
        tables = own_tables(metadata, keep=("itsm.connection", "itsm.connection_change"))
        async with c.session_factory() as s, s.begin():
            await truncate(s, tables)
            await s.execute(insert(poll_state).values([{"name": n, "watermark": func.now()} for n in ("servicenow", "jira")]))
        return {"tables": len(tables), "kept": ["itsm.connection", "itsm.connection_change"]}

    @app.get("/api/v1/itsm/status", response_model=StatusOut)
    async def status(c: C, _: Reader) -> dict:
        await c.registry.refresh()
        async with c.session_factory() as s:
            counts = dict((await s.execute(select(outbound_message.c.status, func.count())
                                           .group_by(outbound_message.c.status))).all())
            polls = [dict(r._mapping) for r in (await s.execute(select(poll_state).order_by(poll_state.c.name))).all()]
        conns = []
        for port in (c.servicenow, c.jira):
            base = {"system": port.system, "mode": port.mode, "url": port.endpoint, "configured": port.configured}
            if not port.configured:
                why = c.registry.error.get(port.system) or "not configured (Integrations -> ServiceNow / Jira)"
                conns.append({**base, "reachable": False, "detail": [], "error": why})
                continue
            try:
                conns.append({**base, "reachable": True, "detail": (await port.health())["detail"], "error": None})
            except Exception as e:
                conns.append({**base, "reachable": False, "detail": [], "error": str(e)[:300]})
        return {"mode": c.settings.itsm_mode, "counts": counts, "polls": polls, "connections": conns}

    return app
