from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select

from mes_core.modules.masterdata.models import Material
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, require

router = APIRouter(prefix="/materials", tags=["masterdata"])


class MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    material_number: str
    description: str
    base_uom: str
    material_type: str
    serial_profile: str | None
    is_serialized: bool


@router.get("", response_model=list[MaterialOut])
async def materials(
    db: DbDep,
    search: str | None = None,
    material_type: str | None = None,
    limit: int = Query(200, le=1000),
    _=require(Perm.MASTERDATA_READ),
) -> list:
    q = select(Material).order_by(Material.material_number).limit(limit)
    if search:
        like = f"%{search}%"
        q = q.where(or_(Material.material_number.ilike(like), Material.description.ilike(like)))
    if material_type:
        q = q.where(Material.material_type == material_type)
    async with db.read() as s:
        return list((await s.scalars(q)).all())
