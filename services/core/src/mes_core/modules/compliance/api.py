from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text

from mes_core.modules.compliance.models import ESignature, audit_log
from mes_core.shared.messaging import messaging
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, require

router = APIRouter(tags=["compliance"])


class AuditEntryOut(BaseModel):
    id: int
    occurred_at: datetime
    tx_id: int
    plant_id: UUID | None
    actor_id: str
    actor_name: str | None
    actor_type: str
    request_id: str | None
    schema_name: str
    table_name: str
    record_id: str | None
    action: str
    changed_fields: list[str] | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    reason: str | None
    signature_id: UUID | None
    seal_seq: int | None
    chain_hash: str | None


class AuditPage(BaseModel):
    items: list[AuditEntryOut]
    total: int


class VerifyOut(BaseModel):
    ok: bool
    checked: int
    expected_last_seq: int
    first_broken_seq: int | None
    unsealed: int


class SignatureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plant_id: UUID | None
    signer_user_id: UUID
    signer_username: str
    signer_display_name: str | None
    meaning: str
    subject_type: str
    subject_id: UUID
    reason: str
    signed_at: datetime


@router.get("/audit", response_model=AuditPage)
async def audit_entries(
    db: DbDep,
    user=require(Perm.AUDIT_READ),  # type: ignore[no-untyped-def]
    table_name: str | None = None,
    record_id: Annotated[list[str] | None, Query()] = None,
    actor_id: str | None = None,
    plant_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    limit: int = Query(100, le=500),
) -> dict:
    a = audit_log.c
    conds = []
    if table_name:
        schema, _, table = table_name.rpartition(".")
        conds.append(a.table_name == table)
        if schema:
            conds.append(a.schema_name == schema)
    if record_id:
        conds.append(a.record_id.in_(record_id))
    if actor_id:
        conds.append(a.actor_id == actor_id)
    if plant_id:
        user.ensure_plant(plant_id)
        conds.append(a.plant_id == plant_id)
    elif user.plant_ids is not None:
        conds.append(a.plant_id.in_(user.plant_ids) | a.plant_id.is_(None))
    if since:
        conds.append(a.occurred_at >= since)
    if until:
        conds.append(a.occurred_at < until)
    async with db.read() as s:
        total = (await s.execute(select(func.count()).select_from(audit_log).where(*conds))).scalar_one()
        rows = (
            await s.execute(
                select(audit_log).where(*conds).order_by(a.occurred_at.desc(), a.id.desc()).offset(offset).limit(limit)
            )
        ).all()
    return {"items": [dict(r._mapping) for r in rows], "total": total}


@router.get("/audit/verify", response_model=VerifyOut)
async def verify(db: DbDep, _=require(Perm.AUDIT_VERIFY)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        r = (await s.execute(text("SELECT * FROM audit.verify_chain()"))).one()
        unsealed = (
            await s.execute(select(func.count()).select_from(audit_log).where(audit_log.c.seal_seq.is_(None)))
        ).scalar_one()
    return {
        "ok": r.ok,
        "checked": r.checked,
        "expected_last_seq": r.expected_last_seq,
        "first_broken_seq": r.first_broken_seq,
        "unsealed": unsealed,
    }


@router.get("/signatures", response_model=list[SignatureOut])
async def signatures(
    db: DbDep,
    user=require(Perm.AUDIT_READ),  # type: ignore[no-untyped-def]
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    limit: int = Query(100, le=500),
) -> list:
    q = select(ESignature).order_by(ESignature.signed_at.desc()).limit(limit)
    if subject_type:
        q = q.where(ESignature.subject_type == subject_type)
    if subject_id:
        q = q.where(ESignature.subject_id == subject_id)
    if user.plant_ids is not None:
        q = q.where(ESignature.plant_id.in_(user.plant_ids))
    async with db.read() as s:
        return list((await s.scalars(q)).all())


class EventFailureOut(BaseModel):
    id: int
    topic: str
    key: str | None
    event_type: str | None
    error: str
    created_at: datetime
    raw: str


@router.get("/ops/event-failures", response_model=list[EventFailureOut])
async def event_failures(  # type: ignore[no-untyped-def]
    db: DbDep, user=require(Perm.AUDIT_READ), since: datetime | None = None, limit: int = Query(50, le=200),
) -> list:
    """Events MES could not process (dead letters): what failed, when, and why. For support diagnosis."""
    dl = messaging.dead_letter
    q = select(dl).order_by(dl.c.created_at.desc()).limit(limit)
    if since:
        q = q.where(dl.c.created_at >= since)
    async with db.read() as s:
        rows = (await s.execute(q)).all()
    return [{**r._mapping, "raw": r.raw[:2000]} for r in rows]
