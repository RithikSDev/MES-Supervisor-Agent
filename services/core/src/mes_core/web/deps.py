import time
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mes_common.auth import AuthError, TokenVerifier
from mes_core.modules.identity.public import (
    UserRecord,
    is_terminal_token,
    provision_user,
    verify_terminal_token,
)
from mes_core.settings import Settings
from mes_core.shared.db.context import Actor
from mes_core.shared.db.session import Database
from mes_core.shared.errors import Unauthorized
from mes_core.shared.permissions import (
    ALL_PLANTS_ROLES,
    TERMINAL_EXTRA_ROLES,
    TERMINAL_ROLES,
    Perm,
    permissions_for,
)
from mes_core.shared.users import CurrentUser

_bearer = HTTPBearer(auto_error=False)
_USER_CACHE_TTL_S = 60


def get_db(request: Request) -> Database:
    return request.app.state.db


DbDep = Annotated[Database, Depends(get_db)]


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


async def get_current_user(
    request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> CurrentUser:
    if creds is None:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    if is_terminal_token(creds.credentials):
        return await _terminal_user(request, creds.credentials)
    verifier: TokenVerifier = request.app.state.verifier
    try:
        principal = await verifier.verify(creds.credentials)
    except AuthError as e:
        raise HTTPException(401, f"invalid token: {e}", headers={"WWW-Authenticate": "Bearer"}) from e

    cache: dict[str, tuple[float, UserRecord]] = request.app.state.user_cache
    hit = cache.get(principal.subject)
    if hit and hit[0] > time.monotonic():
        record = hit[1]
    else:
        db: Database = request.app.state.db
        async with db.unit_of_work(Actor.service("identity-sync"), request_id=request_id(request)) as s:
            record = await provision_user(s, principal)
        cache[principal.subject] = (time.monotonic() + _USER_CACHE_TTL_S, record)
    if not record.active:
        raise HTTPException(403, "user is deactivated in MES")

    return CurrentUser(
        id=record.id,
        username=record.username,
        display_name=record.display_name,
        roles=principal.roles,
        permissions=permissions_for(principal.roles),
        plant_ids=None if principal.roles & ALL_PLANTS_ROLES else record.plant_ids,
    )


async def _terminal_user(request: Request, token: str) -> CurrentUser:
    """Operator PIN session on a shop-floor terminal: operator rights, scoped to that terminal's plant."""
    db: Database = request.app.state.db
    async with db.read() as s:
        try:
            p = await verify_terminal_token(s, request.app.state.settings, token)
        except Unauthorized as e:
            raise HTTPException(401, e.message, headers={"WWW-Authenticate": "Bearer"}) from e
    roles = TERMINAL_ROLES | (p.roles & TERMINAL_EXTRA_ROLES)
    return CurrentUser(
        id=p.user_id,
        username=p.username,
        display_name=p.display_name,
        roles=roles,
        permissions=permissions_for(roles),
        plant_ids=frozenset({p.plant_id}),
        terminal_id=p.terminal_id,
        terminal_session_id=p.session_id,
    )


UserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require(*perms: Perm) -> Any:
    async def check(user: UserDep) -> CurrentUser:
        missing = [p.value for p in perms if not user.has(p)]
        if missing:
            raise HTTPException(403, f"missing permission(s): {', '.join(missing)}")
        return user

    return Depends(check)
