"""Who is writing. Set once per unit of work; read by column defaults and pushed into Postgres for the audit trigger."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

ActorType = Literal["USER", "SERVICE"]


@dataclass(frozen=True)
class Actor:
    id: str  # app_user.id for people, "svc:<name>" for services
    name: str
    type: ActorType = "USER"

    @staticmethod
    def service(name: str) -> Actor:
        return Actor(id=f"svc:{name}", name=name, type="SERVICE")


_actor: ContextVar[Actor | None] = ContextVar("mes_actor", default=None)


def current_actor() -> Actor | None:
    return _actor.get()


def current_actor_id() -> str:
    a = _actor.get()
    if a is None:
        raise RuntimeError("write attempted outside a unit of work (no actor set)")
    return a.id


def set_actor(actor: Actor):  # type: ignore[no-untyped-def]
    return _actor.set(actor)


def reset_actor(token) -> None:  # type: ignore[no-untyped-def]
    _actor.reset(token)
