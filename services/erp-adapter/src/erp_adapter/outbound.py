"""Outbound: deliver MES postings (production confirmations, goods movements) to SAP exactly once, from SAP's
point of view.

SAP postings are not idempotent. Guarantees come from:
  1. `outbound_message.id` = the MES record id: a duplicate event from Kafka is a no-op (ON CONFLICT).
  2. Every retry first *reconciles*: it looks the posting up in SAP by the marker written into it
     (ConfirmationText / MaterialDocumentHeaderText) and only re-posts if SAP has no record of it. That covers
     timeouts after SAP committed.
  3. Messages for the same order go out in creation order across kinds (confirmation before the goods receipt
     it caused; SAP locks the order during posting anyway).

Status flow: PENDING -> SENDING -> POSTED | RETRY_WAIT -> SENDING ... | FAILED (business error) | DEAD (gave up)
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from erp_adapter.db import (
    MESSAGE_CONFIRMATION,
    MESSAGE_GOODS_MOVEMENT,
    MESSAGE_MAINT_NOTIFICATION,
    MESSAGE_MAINT_ORDER,
    MESSAGE_MAINT_ORDER_STATUS,
    messaging,
    outbound_attempt,
    outbound_message,
)
from erp_adapter.ports import (
    ConfirmationCommand,
    ErpError,
    ErpErrorKind,
    ErpPort,
    GoodsMovementCommand,
    MaintNotificationCommand,
    MaintOrderCommand,
    MaintOrderStatusCommand,
)
from erp_adapter.settings import Settings
from mes_common.events import EventEnvelope, PermanentError, Topics

log = logging.getLogger(__name__)

EVENT_CONFIRMATION_CREATED = "mes.confirmation.created"
EVENT_CONFIRMATION_RESULT = "erp.confirmation.result"
EVENT_GOODS_MOVEMENT_CREATED = "mes.goods_movement.created"
EVENT_GOODS_MOVEMENT_RESULT = "erp.goods_movement.result"
EVENT_MAINT_NOTIFICATION_CREATED = "mes.maintenance.notification_created"
EVENT_MAINT_ORDER_CREATED = "mes.maintenance.order_created"
EVENT_MAINT_ORDER_STATUS = "mes.maintenance.order_status"
EVENT_MAINTENANCE_RESULT = "erp.maintenance.result"


def backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(5 * 2 ** (attempt - 1), 300))


@dataclass(frozen=True)
class Kind:
    """Everything that differs between outbound message types."""

    message_type: str
    command: type[BaseModel]
    id_field: str
    post: Callable[[ErpPort, Any], Awaitable[BaseModel]]
    find: Callable[[ErpPort, Any], Awaitable[BaseModel | None]]
    erp_key: Callable[[Any], str]
    result_topic: str
    result_event: str
    # extra fields for the result event (e.g. which MES object a status change belongs to)
    extra: Callable[[Any], dict[str, Any]] | None = None


def _movement_key(cmd: GoodsMovementCommand) -> str:
    return f"{cmd.erp_plant}:{cmd.erp_order_number}" if cmd.erp_order_number else f"{cmd.erp_plant}:MAT:{cmd.material}"


KINDS: dict[str, Kind] = {
    MESSAGE_CONFIRMATION: Kind(
        MESSAGE_CONFIRMATION, ConfirmationCommand, "confirmation_id",
        lambda erp, c: erp.post_confirmation(c), lambda erp, c: erp.find_confirmation(c),
        lambda c: f"{c.erp_plant}:{c.erp_order_number}",
        Topics.ERP_CONFIRMATION_RESULTS, EVENT_CONFIRMATION_RESULT,
    ),
    MESSAGE_GOODS_MOVEMENT: Kind(
        MESSAGE_GOODS_MOVEMENT, GoodsMovementCommand, "movement_id",
        lambda erp, c: erp.post_goods_movement(c), lambda erp, c: erp.find_goods_movement(c),
        _movement_key, Topics.ERP_GOODS_MOVEMENT_RESULTS, EVENT_GOODS_MOVEMENT_RESULT,
    ),
    # SAP PM mirror. Order and its spare-part goods movements share the key plant:order, so SAP has the order
    # before its first 261.
    MESSAGE_MAINT_NOTIFICATION: Kind(
        MESSAGE_MAINT_NOTIFICATION, MaintNotificationCommand, "notification_id",
        lambda erp, c: erp.post_maint_notification(c), lambda erp, c: erp.find_maint_notification(c),
        lambda c: f"{c.erp_plant}:PMN:{c.number}", Topics.ERP_MAINTENANCE_RESULTS, EVENT_MAINTENANCE_RESULT,
        lambda c: {"message_type": MESSAGE_MAINT_NOTIFICATION, "number": c.number},
    ),
    MESSAGE_MAINT_ORDER: Kind(
        MESSAGE_MAINT_ORDER, MaintOrderCommand, "order_id",
        lambda erp, c: erp.post_maint_order(c), lambda erp, c: erp.find_maint_order(c),
        lambda c: f"{c.erp_plant}:{c.number}", Topics.ERP_MAINTENANCE_RESULTS, EVENT_MAINTENANCE_RESULT,
        lambda c: {"message_type": MESSAGE_MAINT_ORDER, "number": c.number},
    ),
    MESSAGE_MAINT_ORDER_STATUS: Kind(
        MESSAGE_MAINT_ORDER_STATUS, MaintOrderStatusCommand, "message_id",
        lambda erp, c: erp.set_maint_order_status(c), lambda erp, c: erp.find_maint_order_status(c),
        lambda c: f"{c.erp_plant}:{c.number}", Topics.ERP_MAINTENANCE_RESULTS, EVENT_MAINTENANCE_RESULT,
        lambda c: {"message_type": MESSAGE_MAINT_ORDER_STATUS, "number": c.number, "order_id": str(c.order_id),
                   "requested_status": c.status},
    ),
}

INTAKE_EVENTS = {
    EVENT_MAINT_NOTIFICATION_CREATED: MESSAGE_MAINT_NOTIFICATION,
    EVENT_MAINT_ORDER_CREATED: MESSAGE_MAINT_ORDER,
    EVENT_MAINT_ORDER_STATUS: MESSAGE_MAINT_ORDER_STATUS,
}


class Intake:
    """Kafka handler: MES event -> durable outbound message."""

    def __init__(self, settings: Settings, kind: Kind) -> None:
        self._s = settings
        self._k = kind

    async def __call__(self, s: AsyncSession, env: EventEnvelope) -> None:
        try:
            cmd = self._k.command.model_validate(env.payload)
        except Exception as e:
            raise PermanentError(f"invalid {self._k.message_type} payload: {e}") from e
        await s.execute(
            insert(outbound_message)
            .values(
                id=getattr(cmd, self._k.id_field),
                message_type=self._k.message_type,
                erp_key=self._k.erp_key(cmd),
                payload=cmd.model_dump(mode="json"),
                status="PENDING",
                max_attempts=self._s.max_attempts,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )


def ConfirmationIntake(settings: Settings) -> Intake:  # noqa: N802 (kept for callers/tests)
    return Intake(settings, KINDS[MESSAGE_CONFIRMATION])


def GoodsMovementIntake(settings: Settings) -> Intake:  # noqa: N802
    return Intake(settings, KINDS[MESSAGE_GOODS_MOVEMENT])


class OutboundDispatcher:
    def __init__(self, erp: ErpPort, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._erp = erp
        self._sf = session_factory
        self._s = settings

    async def run_once(self, limit: int = 20) -> int:
        claimed = await self._claim(limit)
        for msg_id in claimed:
            await self.deliver(msg_id)
        return len(claimed)

    async def _claim(self, limit: int) -> list[UUID]:
        m = outbound_message
        earlier = aliased(outbound_message)
        now = func.now()
        due = or_(
            and_(m.c.status.in_(("PENDING", "RETRY_WAIT")), m.c.next_attempt_at <= now),
            and_(m.c.status == "SENDING", m.c.lease_until < now),  # worker died mid-send
        )
        blocked_by_earlier = (
            select(earlier.c.id)
            .where(
                earlier.c.erp_key == m.c.erp_key,
                earlier.c.seq < m.c.seq,
                earlier.c.status.in_(("PENDING", "SENDING", "RETRY_WAIT")),
            )
            .exists()
        )
        async with self._sf() as s, s.begin():
            ids = (
                await s.execute(
                    select(m.c.id)
                    .where(due, ~blocked_by_earlier)
                    .order_by(m.c.seq)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            if ids:
                await s.execute(
                    update(m)
                    .where(m.c.id.in_(ids))
                    .values(
                        status="SENDING",
                        attempts=m.c.attempts + 1,
                        lease_until=now + timedelta(seconds=self._s.lease_s),
                        updated_at=now,
                    )
                )
        return list(ids)

    async def deliver(self, msg_id: UUID) -> None:
        async with self._sf() as s:
            row = (await s.execute(select(outbound_message).where(outbound_message.c.id == msg_id))).one()
        kind = KINDS[row.message_type]
        cmd = kind.command.model_validate(row.payload)
        started = datetime.now(UTC)
        reconciled = False
        try:
            posted: BaseModel | None = None
            if row.attempts > 1:
                # We tried before. SAP may have committed that attempt even though we never got an answer.
                posted = await kind.find(self._erp, cmd)
                reconciled = posted is not None
            if posted is None:
                posted = await kind.post(self._erp, cmd)
        except ErpError as e:
            await self._record_failure(kind, row, started, e)
            return
        except Exception as e:  # programming/transport errors we did not classify
            await self._record_failure(kind, row, started, ErpError(ErpErrorKind.AMBIGUOUS, repr(e)))
            return
        await self._record_success(kind, row, started, posted.model_dump(), reconciled)

    async def _record_success(self, kind: Kind, row: Any, started: datetime, result: dict, reconciled: bool) -> None:
        async with self._sf() as s, s.begin():
            await s.execute(
                update(outbound_message)
                .where(outbound_message.c.id == row.id)
                .values(
                    status="POSTED", erp_result=result, reconciled=reconciled, last_error=None, lease_until=None,
                    completed_at=func.now(), updated_at=func.now(),
                )
            )
            await self._log_attempt(s, row, started, "RECONCILED" if reconciled else "POSTED", None)
            await self._publish_result(s, kind, row, "POSTED", result, None, reconciled)
        log.info("%s %s %s: %s", kind.message_type, row.id, "reconciled" if reconciled else "posted", result)

    async def _record_failure(self, kind: Kind, row: Any, started: datetime, e: ErpError) -> None:
        if e.kind == ErpErrorKind.PERMANENT:
            status = "FAILED"
        elif row.attempts >= row.max_attempts:
            status = "DEAD"
        else:
            status = "RETRY_WAIT"
        async with self._sf() as s, s.begin():
            await s.execute(
                update(outbound_message)
                .where(outbound_message.c.id == row.id)
                .values(
                    status=status,
                    last_error=str(e)[:2000],
                    lease_until=None,
                    next_attempt_at=func.now() + backoff(row.attempts),
                    completed_at=func.now() if status in ("FAILED", "DEAD") else None,
                    updated_at=func.now(),
                )
            )
            await self._log_attempt(s, row, started, e.kind.value, str(e))
            if status in ("FAILED", "DEAD"):
                await self._publish_result(s, kind, row, "FAILED", {}, str(e), False)
        log.warning("%s %s attempt %d -> %s: %s", kind.message_type, row.id, row.attempts, status, e)

    async def _log_attempt(self, s: AsyncSession, row: Any, started: datetime, outcome: str, error: str | None) -> None:
        await s.execute(
            outbound_attempt.insert().values(
                message_id=row.id,
                attempt_no=row.attempts,
                started_at=started,
                finished_at=datetime.now(UTC),
                outcome=outcome,
                error=error[:2000] if error else None,
            )
        )

    async def _publish_result(
        self, s: AsyncSession, kind: Kind, row: Any, status: str, result: dict, error: str | None, reconciled: bool
    ) -> None:
        extra = kind.extra(kind.command.model_validate(row.payload)) if kind.extra else {}
        await messaging.publish(
            s,
            topic=kind.result_topic,
            key=row.erp_key,
            event_type=kind.result_event,
            payload={
                **extra,
                kind.id_field: str(row.id),
                "status": status,
                **result,
                "error": error,
                "attempts": row.attempts,
                "reconciled": reconciled,
            },
        )


ConfirmationDispatcher = OutboundDispatcher  # Phase 1 name


async def retry_message(s: AsyncSession, msg_id: UUID, extra_attempts: int) -> bool:
    """Manual retry from the integration monitor. Always reconciles first because attempts > 0."""
    res = await s.execute(
        update(outbound_message)
        .where(outbound_message.c.id == msg_id, outbound_message.c.status.in_(("FAILED", "DEAD", "RETRY_WAIT")))
        .values(
            status="PENDING",
            next_attempt_at=func.now(),
            max_attempts=outbound_message.c.attempts + extra_attempts,
            completed_at=None,
            updated_at=func.now(),
        )
    )
    return res.rowcount > 0
