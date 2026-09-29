from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.masterdata.models import Material


@dataclass(frozen=True)
class MaterialRef:
    id: UUID
    material_number: str
    description: str
    base_uom: str
    material_type: str
    is_serialized: bool


def _ref(m: Material) -> MaterialRef:
    return MaterialRef(m.id, m.material_number, m.description, m.base_uom, m.material_type, m.is_serialized)


async def upsert_material(
    s: AsyncSession,
    *,
    material_number: str,
    description: str,
    base_uom: str,
    material_type: str,
    serial_profile: str | None,
) -> MaterialRef:
    """Create or update from ERP data. Only touches the row when something actually changed (no audit noise)."""
    m = (await s.scalars(select(Material).where(Material.material_number == material_number))).first()
    if m is None:
        m = Material(material_number=material_number)
        s.add(m)
    for field, value in (
        ("description", description),
        ("base_uom", base_uom),
        ("material_type", material_type),
        ("serial_profile", serial_profile),
        ("is_serialized", bool(serial_profile)),
    ):
        if getattr(m, field, None) != value:
            setattr(m, field, value)
    await s.flush()
    return _ref(m)


async def search_material_ids(s: AsyncSession, text: str, limit: int = 200) -> list[UUID]:
    like = f"%{text}%"
    q = (
        select(Material.id)
        .where((Material.material_number.ilike(like)) | (Material.description.ilike(like)))
        .limit(limit)
    )
    return list((await s.scalars(q)).all())


async def get_materials(s: AsyncSession, ids: set[UUID]) -> dict[UUID, MaterialRef]:
    if not ids:
        return {}
    rows = (await s.scalars(select(Material).where(Material.id.in_(ids)))).all()
    return {m.id: _ref(m) for m in rows}
