"""OIDC bearer-token verification (Keycloak).

Services validate tokens locally against the realm's JWKS. The issuer is the *public* Keycloak URL
(what the browser used); the JWKS may be fetched over the internal Docker network.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

import jwt
from jwt import PyJWKClient


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    username: str
    roles: frozenset[str]
    display_name: str | None = None
    email: str | None = None
    claims: dict = field(default_factory=dict, compare=False, repr=False)


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Principal: ...


class JwksTokenVerifier:
    def __init__(self, *, issuer: str, jwks_url: str, audience: str, leeway: int = 30) -> None:
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway
        self._jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    async def verify(self, token: str) -> Principal:
        try:
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
            )
        except jwt.PyJWTError as e:
            raise AuthError(str(e)) from e
        except Exception as e:  # JWKS fetch failures etc.
            raise AuthError(f"token verification failed: {e}") from e
        return principal_from_claims(claims)


def principal_from_claims(claims: dict) -> Principal:
    roles = frozenset(claims.get("realm_access", {}).get("roles", []))
    return Principal(
        subject=claims["sub"],
        username=claims.get("preferred_username") or claims["sub"],
        roles=roles,
        display_name=claims.get("name"),
        email=claims.get("email"),
        claims=claims,
    )


class StaticTokenVerifier:
    """Test-only verifier: maps opaque tokens to principals. Never wired up outside tests."""

    def __init__(self, principals: dict[str, Principal]) -> None:
        self._p = principals

    async def verify(self, token: str) -> Principal:
        try:
            return self._p[token]
        except KeyError:
            raise AuthError("unknown test token") from None
