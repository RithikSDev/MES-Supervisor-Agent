from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from mes_core.modules.org.public import get_terminal
from mes_core.web.deps import DbDep, UserDep

router = APIRouter(tags=["identity"])


class MeOut(BaseModel):
    id: UUID
    username: str
    display_name: str | None
    roles: list[str]
    permissions: list[str]
    plant_ids: list[UUID] | None  # null = all plants
    # set for a shop-floor terminal session
    terminal_id: UUID | None = None
    terminal_code: str | None = None
    line_id: UUID | None = None
    line_code: str | None = None


@router.get("/me", response_model=MeOut)
async def me(user: UserDep, db: DbDep) -> dict:
    terminal: dict = {}
    if user.terminal_id:
        async with db.read() as s:
            t = await get_terminal(s, user.terminal_id)
        terminal = {"terminal_id": t.id, "terminal_code": t.code, "line_id": t.line_id, "line_code": t.line_code}
    return {
        **terminal,
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "roles": sorted(user.roles),
        "permissions": sorted(p.value for p in user.permissions),
        "plant_ids": sorted(user.plant_ids) if user.plant_ids is not None else None,
    }
