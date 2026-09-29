from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.auth import Principal
from mes_core.modules.identity.models import AppUser, UserScope


@dataclass(frozen=True)
class UserRecord:
    id: UUID
    username: str
    display_name: str | None
    active: bool
    plant_ids: frozenset[UUID]


async def provision_user(s: AsyncSession, p: Principal) -> UserRecord:
    """Find or create the local user for a token. Links a pre-seeded user by username on first login.

    Must run inside a unit of work (it may write).
    """
    user = (await s.scalars(select(AppUser).where(AppUser.idp_subject == p.subject))).first()
    if user is None:
        user = (
            await s.scalars(select(AppUser).where(AppUser.username == p.username, AppUser.idp_subject.is_(None)))
        ).first()
        if user is None:
            user = AppUser(username=p.username)
            s.add(user)
        user.idp_subject = p.subject
    if user.display_name != p.display_name:
        user.display_name = p.display_name
    if user.email != p.email:
        user.email = p.email
    if user.last_login_at is None or (datetime.now(UTC) - user.last_login_at).total_seconds() > 3600:
        user.last_login_at = datetime.now(UTC)
    await s.flush()
    scopes = (await s.scalars(select(UserScope.plant_id).where(UserScope.user_id == user.id))).all()
    return UserRecord(user.id, user.username, user.display_name, user.active, frozenset(scopes))


# Terminal sessions (verification is used by the web layer for every terminal request)
from mes_core.modules.identity.terminal_auth import (  # noqa: E402
    TerminalPrincipal,
    is_terminal_token,
    verify_terminal_token,
)

__all__ = ["TerminalPrincipal", "UserRecord", "is_terminal_token", "provision_user", "verify_terminal_token"]
