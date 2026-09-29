"""Terminal device + PIN login endpoints, and the admin side (terminals, enrolment, operators, PINs)."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from mes_core.modules.identity import terminal_auth as ta
from mes_core.modules.identity.models import (
    AppUser,
    DeviceEnrollment,
    OperatorCredential,
    TerminalSession,
    UserScope,
)
from mes_core.modules.org.public import create_terminal, get_plant, list_terminals
from mes_core.shared.db.context import Actor
from mes_core.shared.errors import TooManyAttempts, Unauthorized
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, SettingsDep, UserDep, request_id, require

router = APIRouter(tags=["terminals"])

DeviceHeader = Annotated[str | None, Header(alias="X-Terminal-Device")]
_DEVICE_ACTOR = Actor.service("terminal-auth")


class TerminalInfo(BaseModel):
    terminal_id: UUID
    code: str
    name: str
    line_id: UUID
    line_code: str
    plant_id: UUID
    plant_code: str
    plant_timezone: str
    pin_locked_until: datetime | None


class OperatorBrief(BaseModel):
    id: UUID
    username: str
    display_name: str | None


class LoginIn(BaseModel):
    pin: str


class LoginOut(BaseModel):
    access_token: str
    expires_at: datetime
    roles: list[str] = ["operator"]
    operator: OperatorBrief
    terminal: TerminalInfo


async def _terminal_info(s, terminal) -> dict:  # type: ignore[no-untyped-def]
    plant = await get_plant(s, terminal.plant_id)
    return {
        "terminal_id": terminal.id,
        "code": terminal.code,
        "name": terminal.name,
        "line_id": terminal.line_id,
        "line_code": terminal.line_code,
        "plant_id": plant.id,
        "plant_code": plant.code,
        "plant_timezone": plant.timezone,
        "pin_locked_until": terminal.pin_locked_until,
    }


# ------------------------------------------------------------------------------------------------ device side


@router.get("/terminal/device", response_model=TerminalInfo)
async def device(db: DbDep, device: DeviceHeader = None) -> dict:
    async with db.read() as s:
        terminal = await ta.terminal_for_device(s, device)
        return await _terminal_info(s, terminal)


@router.post("/terminal/login", response_model=LoginOut)
async def login(body: LoginIn, request: Request, db: DbDep, settings: SettingsDep, device: DeviceHeader = None) -> dict:
    async with db.unit_of_work(_DEVICE_ACTOR, request_id=request_id(request), reason="terminal PIN login") as s:
        terminal = await ta.terminal_for_device(s, device)
        result = await ta.pin_login(s, settings, terminal=terminal, pin=body.pin)
        info = await _terminal_info(s, terminal) if result.ok else None
    # outside the transaction: a failed attempt's counter is committed before we answer
    if not result.ok:
        if result.locked_until:
            retry = max(1, int((result.locked_until - datetime.now(UTC)).total_seconds()))
            raise TooManyAttempts(
                f"Too many wrong PINs. Try again in {retry} s.", code="pin_locked", details={"retry_after": retry}
            )
        raise Unauthorized("PIN not recognised", code="wrong_pin")
    user = result.user
    assert user is not None and result.token and result.expires_at
    return {
        "access_token": result.token,
        "expires_at": result.expires_at,
        "operator": {"id": user.id, "username": user.username, "display_name": user.display_name},
        "terminal": info,
        "roles": result.roles,
    }


@router.post("/terminal/logout", status_code=204)
async def logout(user: UserDep, request: Request, db: DbDep) -> None:
    if user.terminal_session_id is None:
        raise HTTPException(400, "not a terminal session")
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await ta.end_session(s, user.terminal_session_id)


# ------------------------------------------------------------------------------------------------ admin side


class TerminalAdminOut(BaseModel):
    id: UUID
    plant_id: UUID
    line_id: UUID
    line_code: str
    code: str
    name: str
    active: bool
    pin_locked_until: datetime | None
    enrolled: bool
    enrolled_at: datetime | None
    enrolled_by: str | None
    session_operator: str | None
    session_started_at: datetime | None


class TerminalCreate(BaseModel):
    plant_id: UUID
    line_id: UUID
    code: str = Field(min_length=2, max_length=20)
    name: str = Field(min_length=2, max_length=80)


class EnrollOut(BaseModel):
    terminal_id: UUID
    device_token: str


@router.get("/terminals", response_model=list[TerminalAdminOut])
async def terminals(plant_id: UUID, db: DbDep, user=require(Perm.TERMINAL_ADMIN)) -> list:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        refs = await list_terminals(s, plant_id)
        enrollments = {
            e.terminal_id: e
            for e in (
                await s.scalars(
                    select(DeviceEnrollment).where(DeviceEnrollment.plant_id == plant_id, DeviceEnrollment.revoked_at.is_(None))
                )
            ).all()
        }
        sessions = {
            row[0].terminal_id: row
            for row in (
                await s.execute(
                    select(TerminalSession, AppUser.display_name, AppUser.username)
                    .join(AppUser, AppUser.id == TerminalSession.user_id)
                    .where(TerminalSession.plant_id == plant_id, TerminalSession.ended_at.is_(None))
                )
            ).all()
        }
    out = []
    for t in refs:
        e = enrollments.get(t.id)
        sess = sessions.get(t.id)
        live = sess is not None and sess[0].expires_at > datetime.now(UTC)
        out.append(
            {
                **t.__dict__,
                "enrolled": e is not None,
                "enrolled_at": e.enrolled_at if e else None,
                "enrolled_by": e.enrolled_by if e else None,
                "session_operator": (sess[1] or sess[2]) if live else None,
                "session_started_at": sess[0].started_at if live else None,
            }
        )
    return out


@router.post("/terminals", response_model=TerminalAdminOut, status_code=201)
async def add_terminal(body: TerminalCreate, request: Request, db: DbDep, user=require(Perm.TERMINAL_ADMIN)) -> dict:  # type: ignore[no-untyped-def]
    user.ensure_plant(body.plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        t = await create_terminal(s, plant_id=body.plant_id, line_id=body.line_id, code=body.code, name=body.name)
    return {**t.__dict__, "enrolled": False, "enrolled_at": None, "enrolled_by": None, "session_operator": None, "session_started_at": None}


async def _plant_of_terminal(db, terminal_id: UUID) -> UUID:  # type: ignore[no-untyped-def]
    from mes_core.modules.org.public import get_terminal

    async with db.read() as s:
        return (await get_terminal(s, terminal_id)).plant_id


@router.post("/terminals/{terminal_id}/enroll", response_model=EnrollOut)
async def enroll(terminal_id: UUID, request: Request, db: DbDep, user=require(Perm.TERMINAL_ADMIN)) -> dict:  # type: ignore[no-untyped-def]
    user.ensure_plant(await _plant_of_terminal(db, terminal_id))
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason="terminal enrolment") as s:
        token = await ta.enroll_terminal(s, terminal_id=terminal_id, enrolled_by=user.username)
    return {"terminal_id": terminal_id, "device_token": token}


@router.post("/terminals/{terminal_id}/revoke", status_code=204)
async def revoke(terminal_id: UUID, request: Request, db: DbDep, user=require(Perm.TERMINAL_ADMIN)) -> None:  # type: ignore[no-untyped-def]
    user.ensure_plant(await _plant_of_terminal(db, terminal_id))
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason="terminal enrolment revoked") as s:
        await ta.revoke_enrollment(s, terminal_id=terminal_id, revoked_by=user.username)


class EndSessionIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/terminals/{terminal_id}/end-session", status_code=204)
async def end_terminal_session(  # type: ignore[no-untyped-def]
    terminal_id: UUID, body: EndSessionIn, request: Request, db: DbDep, user=require(Perm.TERMINAL_ADMIN),
) -> None:
    """End the operator session on a terminal (e.g. someone left it logged in); the device stays enrolled."""
    user.ensure_plant(await _plant_of_terminal(db, terminal_id))
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        if not await ta.end_active_session(s, terminal_id, reason="ENDED_BY_SUPPORT"):
            raise HTTPException(409, "no operator is logged in on this terminal")


class OperatorOut(BaseModel):
    id: UUID
    username: str
    display_name: str | None
    active: bool
    has_pin: bool
    pin_set_at: datetime | None
    technician: bool = False  # PIN sessions get maintenance technician rights
    sso_linked: bool  # has a Keycloak account (vs. shop-floor-only)


class OperatorCreate(BaseModel):
    plant_id: UUID
    username: str
    display_name: str = Field(min_length=2, max_length=80)
    pin: str


class PinSet(BaseModel):
    plant_id: UUID
    pin: str
    technician: bool | None = None


@router.get("/operators", response_model=list[OperatorOut])
async def operators(plant_id: UUID, db: DbDep, user=require(Perm.OPERATOR_ADMIN)) -> list:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        rows = (
            await s.execute(
                select(AppUser, OperatorCredential.pin_set_at, OperatorCredential.terminal_roles)
                .join(UserScope, UserScope.user_id == AppUser.id)
                .outerjoin(
                    OperatorCredential,
                    (OperatorCredential.user_id == AppUser.id) & (OperatorCredential.plant_id == plant_id),
                )
                .where(UserScope.plant_id == plant_id)
                .order_by(AppUser.username)
            )
        ).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "active": u.active,
            "has_pin": pin_set_at is not None,
            "pin_set_at": pin_set_at,
            "sso_linked": u.idp_subject is not None,
            "technician": "maintenance_tech" in (roles or []),
        }
        for u, pin_set_at, roles in rows
    ]


@router.post("/operators", response_model=OperatorOut, status_code=201)
async def add_operator(body: OperatorCreate, request: Request, db: DbDep, settings: SettingsDep, user=require(Perm.OPERATOR_ADMIN)) -> dict:  # type: ignore[no-untyped-def]
    user.ensure_plant(body.plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason="operator created") as s:
        u = await ta.create_operator(
            s, settings, plant_id=body.plant_id, username=body.username, display_name=body.display_name, pin=body.pin
        )
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "active": True, "has_pin": True,
            "pin_set_at": datetime.now(UTC), "sso_linked": False}


@router.put("/operators/{user_id}/pin", status_code=204)
async def set_operator_pin(user_id: UUID, body: PinSet, request: Request, db: DbDep, settings: SettingsDep, user=require(Perm.OPERATOR_ADMIN)) -> None:  # type: ignore[no-untyped-def]
    user.ensure_plant(body.plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason="operator PIN set") as s:
        await ta.get_user(s, user_id)
        await ta.set_pin(s, settings, user_id=user_id, plant_id=body.plant_id, pin=body.pin, technician=body.technician)
