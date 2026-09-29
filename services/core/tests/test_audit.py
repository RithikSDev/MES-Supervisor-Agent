import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from mes_core.modules.compliance.audit_sql import BUSINESS_SCHEMAS
from mes_core.modules.compliance.public import seal_audit_log
from mes_core.modules.org.models import Equipment
from mes_core.shared.db.context import Actor
from mes_core.shared.db.session import Database

pytestmark = pytest.mark.db


async def test_write_without_actor_is_rejected_by_the_database(db: Database) -> None:
    async with db.session_factory() as s:
        with pytest.raises(DBAPIError, match="without actor context"):
            async with s.begin():
                await s.execute(text("UPDATE org.equipment SET name = name || ' x'"))


async def test_update_is_captured_with_before_after_and_reason(db: Database) -> None:
    async with db.unit_of_work(Actor("u-1", "tester"), request_id="req-42", reason="rename for test") as s:
        eq = (await s.scalars(select(Equipment).where(Equipment.code == "DEB-01"))).one()
        eq.name = "Deburr cell (renamed)"
    async with db.read() as s:
        row = (
            await s.execute(
                text(
                    "SELECT action, actor_id, actor_name, request_id, reason, changed_fields,"
                    " before->>'name' AS before_name, after->>'name' AS after_name, plant_id"
                    " FROM audit.audit_log WHERE table_name='equipment' AND record_id=:id ORDER BY id DESC LIMIT 1"
                ),
                {"id": str(eq.id)},
            )
        ).one()
    assert row.action == "U"
    assert (row.actor_id, row.actor_name, row.request_id, row.reason) == ("u-1", "tester", "req-42", "rename for test")
    assert row.changed_fields == ["name"]  # updated_at/row_version bookkeeping is not reported as a change
    assert (row.before_name, row.after_name) == ("Deburr/wash cell", "Deburr cell (renamed)")
    assert row.plant_id == eq.plant_id


async def test_noop_update_writes_no_audit_row(db: Database) -> None:
    count = "SELECT count(*) FROM audit.audit_log WHERE table_name='equipment'"
    async with db.read() as s:
        before = (await s.execute(text(count))).scalar_one()
    async with db.unit_of_work(Actor.service("test")) as s:
        await s.execute(update(Equipment).where(Equipment.code == "TURN-01").values(name=Equipment.name))
    async with db.read() as s:
        assert (await s.execute(text(count))).scalar_one() == before


async def test_app_role_cannot_modify_or_delete_audit_rows(db: Database) -> None:
    for stmt in ("UPDATE audit.audit_log SET reason = 'forged'", "DELETE FROM audit.audit_log"):
        async with db.session_factory() as s:
            with pytest.raises(DBAPIError, match="permission denied"):
                async with s.begin():
                    await s.execute(text(stmt))


async def test_every_business_table_has_the_audit_trigger(db: Database) -> None:
    async with db.read() as s:
        missing = (
            await s.execute(
                text(
                    "SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE c.relkind = 'r' AND n.nspname = ANY(:schemas)"
                    " AND NOT EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid = c.oid AND t.tgname = 'audit_row')"
                ),
                {"schemas": BUSINESS_SCHEMAS},
            )
        ).scalars().all()
    assert missing == []


async def test_hash_chain_seals_verifies_and_detects_tampering(db: Database, superdb: Database) -> None:
    async with db.session_factory() as s, s.begin():
        await seal_audit_log(s, 100_000)
    async with db.read() as s:
        ok = (await s.execute(text("SELECT * FROM audit.verify_chain()"))).one()
    assert ok.ok and ok.checked > 0

    # Tamper as superuser (bypassing the guard trigger), verify, then restore.
    async with superdb.session_factory() as s, s.begin():
        target = (await s.execute(text("SELECT id, reason FROM audit.audit_log WHERE seal_seq = 3"))).one()
        await s.execute(text("ALTER TABLE audit.audit_log DISABLE TRIGGER audit_log_guard"))
        await s.execute(text("UPDATE audit.audit_log SET reason = 'forged' WHERE id = :id"), {"id": target.id})
    try:
        async with db.read() as s:
            broken = (await s.execute(text("SELECT * FROM audit.verify_chain()"))).one()
        assert not broken.ok
        assert broken.first_broken_seq == 3
    finally:
        async with superdb.session_factory() as s, s.begin():
            await s.execute(text("UPDATE audit.audit_log SET reason = :r WHERE id = :id"), {"r": target.reason, "id": target.id})
            await s.execute(text("ALTER TABLE audit.audit_log ENABLE TRIGGER audit_log_guard"))
    async with db.read() as s:
        assert (await s.execute(text("SELECT ok FROM audit.verify_chain()"))).scalar_one()
