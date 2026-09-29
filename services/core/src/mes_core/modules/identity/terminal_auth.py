"""Shop-floor terminal authentication.

Two credentials, deliberately separate:
  * device token: proves "this browser is terminal T-L1". Issued once at enrolment by a supervisor, stored
    in the kiosk's local storage; we keep only its SHA-256. Revoking the enrolment disables the device.
  * operator PIN: identifies the operator (stands in for a badge). A successful PIN login on an enrolled
    device creates a TerminalSession and an MES-signed JWT (HS256) valid for one shift at most.

The terminal JWT always carries operator permissions only, and is rejected once its session row is ended
(logout, replaced by the next PIN login on the same device, or revoked).
"""

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.identity.models import (
    AppUser,
    DeviceEnrollment,
    OperatorCredential,
    TerminalSession,
    UserScope,
)
from mes_core.modules.org.public import (
    TerminalRef,
    clear_pin_failures,
    get_terminal,
    register_pin_failure,
)
from mes_core.settings import Settings
from mes_core.shared.errors import DomainError, NotFound, Unauthorized, ValidationFailed

TERMINAL_ISSUER = "mes-core:terminal"
TERMINAL_AUDIENCE = "mes-api"
PIN_RE = re.compile(r"^\d{4,6}$")
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


# ------------------------------------------------------------------------------------------------ hashing


def pin_lookup(pepper: str, plant_id: UUID, pin: str) -> str:
    return hmac.new(pepper.encode(), f"{plant_id}:{pin}".encode(), hashlib.sha256).hexdigest()


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pin.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    b64 = base64.urlsafe_b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt).decode()}${b64(dk).decode()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        expected = base64.urlsafe_b64decode(dk)
        got = hashlib.scrypt(
            pin.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except ValueError:
        return False
    return hmac.compare_digest(got, expected)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ operators


def _validate_pin(pin: str) -> None:
    if not PIN_RE.match(pin or ""):
        raise ValidationFailed("PIN must be 4 to 6 digits", code="pin_format")


async def set_pin(
    s: AsyncSession, settings: Settings, *, user_id: UUID, plant_id: UUID, pin: str, technician: bool | None = None
) -> None:
    _validate_pin(pin)
    scoped = (
        await s.scalars(select(UserScope.id).where(UserScope.user_id == user_id, UserScope.plant_id == plant_id))
    ).first()
    if scoped is None:
        raise NotFound("user is not assigned to this plant")
    lookup = pin_lookup(settings.terminal_pin_pepper, plant_id, pin)
    clash = (
        await s.scalars(
            select(OperatorCredential).where(
                OperatorCredential.plant_id == plant_id,
                OperatorCredential.pin_lookup == lookup,
                OperatorCredential.user_id != user_id,
            )
        )
    ).first()
    if clash:
        raise DomainError("that PIN is already used by another operator in this plant", code="pin_in_use")
    cred = (
        await s.scalars(
            select(OperatorCredential).where(OperatorCredential.user_id == user_id, OperatorCredential.plant_id == plant_id)
        )
    ).first()
    now = datetime.now(UTC)
    roles = ["operator", "maintenance_tech"] if technician else ["operator"]
    if cred is None:
        s.add(OperatorCredential(user_id=user_id, plant_id=plant_id, pin_lookup=lookup, pin_hash=hash_pin(pin), pin_set_at=now,
                                 terminal_roles=roles))
    else:
        cred.pin_lookup, cred.pin_hash, cred.pin_set_at = lookup, hash_pin(pin), now
        if technician is not None:
            cred.terminal_roles = roles
    await s.flush()


async def create_operator(
    s: AsyncSession, settings: Settings, *, plant_id: UUID, username: str, display_name: str, pin: str
) -> AppUser:
    """A shop-floor-only person: exists in MES (no Keycloak account), logs in with a PIN on terminals."""
    if not re.match(r"^[a-z0-9._-]{3,40}$", username):
        raise ValidationFailed("username: 3-40 characters, lowercase letters, digits, . _ -")
    if (await s.scalars(select(AppUser).where(AppUser.username == username))).first():
        raise DomainError(f"username {username} already exists", code="duplicate")
    user = AppUser(username=username, display_name=display_name, active=True)
    s.add(user)
    await s.flush()
    s.add(UserScope(user_id=user.id, plant_id=plant_id, line_id=None))
    await set_pin(s, settings, user_id=user.id, plant_id=plant_id, pin=pin)
    return user


# ------------------------------------------------------------------------------------------------ enrolment


async def enroll_terminal(s: AsyncSession, *, terminal_id: UUID, enrolled_by: str) -> str:
    """(Re-)enrol a device for a terminal. Returns the device token; it is shown once and never stored."""
    terminal = await get_terminal(s, terminal_id)
    if not terminal.active:
        raise DomainError("terminal is deactivated", code="terminal_inactive")
    await revoke_enrollment(s, terminal_id=terminal_id, revoked_by=enrolled_by)
    token = secrets.token_urlsafe(32)
    s.add(
        DeviceEnrollment(
            terminal_id=terminal_id,
            plant_id=terminal.plant_id,
            token_hash=hash_device_token(token),
            enrolled_by=enrolled_by,
            enrolled_at=datetime.now(UTC),
        )
    )
    await s.flush()
    return token


async def revoke_enrollment(s: AsyncSession, *, terminal_id: UUID, revoked_by: str) -> bool:
    now = datetime.now(UTC)
    res = await s.execute(
        update(DeviceEnrollment)
        .where(DeviceEnrollment.terminal_id == terminal_id, DeviceEnrollment.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=revoked_by)
    )
    await s.execute(
        update(TerminalSession)
        .where(TerminalSession.terminal_id == terminal_id, TerminalSession.ended_at.is_(None))
        .values(ended_at=now, end_reason="REVOKED")
    )
    return res.rowcount > 0


async def terminal_for_device(s: AsyncSession, device_token: str | None) -> TerminalRef:
    if not device_token:
        raise Unauthorized("this device is not enrolled as a terminal", code="device_not_enrolled")
    enrollment = (
        await s.scalars(
            select(DeviceEnrollment).where(
                DeviceEnrollment.token_hash == hash_device_token(device_token), DeviceEnrollment.revoked_at.is_(None)
            )
        )
    ).first()
    if enrollment is None:
        raise Unauthorized("this device is not enrolled as a terminal (or its enrolment was revoked)", code="device_not_enrolled")
    terminal = await get_terminal(s, enrollment.terminal_id)
    if not terminal.active:
        raise Unauthorized("terminal is deactivated", code="terminal_inactive")
    return terminal


# ------------------------------------------------------------------------------------------------ PIN login


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    token: str | None = None
    session_id: UUID | None = None
    expires_at: datetime | None = None
    user: AppUser | None = None
    locked_until: datetime | None = None
    roles: list[str] | None = None


async def pin_login(s: AsyncSession, settings: Settings, *, terminal: TerminalRef, pin: str) -> LoginResult:
    """Returns a result instead of raising on a wrong PIN, so the failure counter commits with the transaction."""
    now = datetime.now(UTC)
    if terminal.pin_locked_until and terminal.pin_locked_until > now:
        return LoginResult(ok=False, locked_until=terminal.pin_locked_until)

    cred = None
    if PIN_RE.match(pin or ""):
        cred = (
            await s.scalars(
                select(OperatorCredential).where(
                    OperatorCredential.plant_id == terminal.plant_id,
                    OperatorCredential.pin_lookup == pin_lookup(settings.terminal_pin_pepper, terminal.plant_id, pin),
                )
            )
        ).first()
    user = await s.get(AppUser, cred.user_id) if cred else None
    if cred is None or user is None or not user.active or not verify_pin(pin, cred.pin_hash):
        locked = await register_pin_failure(
            s, terminal.id, max_failures=settings.terminal_max_pin_failures, lockout_s=settings.terminal_pin_lockout_s
        )
        return LoginResult(ok=False, locked_until=locked if locked and locked > now else None)

    await clear_pin_failures(s, terminal.id)
    # one operator per terminal: a new login ends the previous session on this device
    await s.execute(
        update(TerminalSession)
        .where(TerminalSession.terminal_id == terminal.id, TerminalSession.ended_at.is_(None))
        .values(ended_at=now, end_reason="REPLACED")
    )
    expires = now + timedelta(hours=settings.terminal_session_hours)
    session = TerminalSession(
        terminal_id=terminal.id, user_id=user.id, plant_id=terminal.plant_id, started_at=now, expires_at=expires,
        roles=list(cred.terminal_roles or ["operator"]),
    )
    s.add(session)
    await s.flush()
    token = jwt.encode(
        {
            "iss": TERMINAL_ISSUER,
            "aud": TERMINAL_AUDIENCE,
            "sub": str(user.id),
            "sid": str(session.id),
            "tid": str(terminal.id),
            "pid": str(terminal.plant_id),
            "preferred_username": user.username,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        },
        settings.terminal_jwt_secret,
        algorithm="HS256",
    )
    return LoginResult(ok=True, token=token, session_id=session.id, expires_at=expires, user=user,
                       roles=list(session.roles))


async def end_session(s: AsyncSession, session_id: UUID, reason: str = "LOGOUT") -> None:
    await s.execute(
        update(TerminalSession)
        .where(TerminalSession.id == session_id, TerminalSession.ended_at.is_(None))
        .values(ended_at=datetime.now(UTC), end_reason=reason)
    )


async def end_active_session(s: AsyncSession, terminal_id: UUID, reason: str) -> bool:
    """End whatever operator session is open on a terminal. False when nobody is logged in."""
    res = await s.execute(
        update(TerminalSession)
        .where(TerminalSession.terminal_id == terminal_id, TerminalSession.ended_at.is_(None))
        .values(ended_at=datetime.now(UTC), end_reason=reason)
    )
    return bool(res.rowcount)


# ------------------------------------------------------------------------------------------------ token verification


def is_terminal_token(token: str) -> bool:
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss") == TERMINAL_ISSUER
    except jwt.PyJWTError:
        return False


@dataclass(frozen=True)
class TerminalPrincipal:
    user_id: UUID
    username: str
    display_name: str | None
    plant_id: UUID
    terminal_id: UUID
    session_id: UUID
    roles: frozenset[str] = frozenset({"operator"})


async def verify_terminal_token(s: AsyncSession, settings: Settings, token: str) -> TerminalPrincipal:
    try:
        claims = jwt.decode(
            token, settings.terminal_jwt_secret, algorithms=["HS256"], audience=TERMINAL_AUDIENCE, issuer=TERMINAL_ISSUER
        )
    except jwt.PyJWTError as e:
        raise Unauthorized(f"invalid terminal session: {e}", code="session_invalid") from e
    session = await s.get(TerminalSession, UUID(claims["sid"]))
    if session is None or session.ended_at is not None or session.expires_at <= datetime.now(UTC):
        raise Unauthorized("terminal session has ended; log in again", code="session_ended")
    user = await s.get(AppUser, session.user_id)
    if user is None or not user.active:
        raise Unauthorized("operator is deactivated", code="session_invalid")
    return TerminalPrincipal(user.id, user.username, user.display_name, session.plant_id, session.terminal_id, session.id,
                             frozenset(session.roles or ["operator"]))


async def get_user(s: AsyncSession, user_id: UUID) -> AppUser:
    user = await s.get(AppUser, user_id)
    if user is None:
        raise NotFound("user not found")
    return user
