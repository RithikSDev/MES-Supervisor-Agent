from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.compliance.models import SIGNATURE_MEANINGS, ESignature
from mes_core.shared.db.session import set_change_reason
from mes_core.shared.errors import ValidationFailed
from mes_core.shared.users import CurrentUser


@dataclass(frozen=True)
class SignatureRef:
    id: UUID
    signer_username: str
    meaning: str
    reason: str
    signed_at: datetime


async def sign(
    s: AsyncSession,
    user: CurrentUser,
    *,
    meaning: str,
    subject_type: str,
    subject_id: UUID,
    reason: str,
    plant_id: UUID | None,
) -> SignatureRef:
    """Record an e-signature and link every audit row written later in this transaction to it."""
    if meaning not in SIGNATURE_MEANINGS:
        raise ValidationFailed(f"unknown signature meaning {meaning!r}")
    if not reason or not reason.strip():
        raise ValidationFailed("a reason is required to sign", code="signature_reason_required")
    sig = ESignature(
        plant_id=plant_id,
        signer_user_id=user.id,
        signer_username=user.username,
        signer_display_name=user.display_name,
        meaning=meaning,
        subject_type=subject_type,
        subject_id=subject_id,
        reason=reason.strip(),
        signed_at=datetime.now(UTC),
    )
    s.add(sig)
    await s.flush()
    await set_change_reason(s, sig.reason, sig.id)
    return SignatureRef(sig.id, sig.signer_username, sig.meaning, sig.reason, sig.signed_at)


async def seal_audit_log(s: AsyncSession, batch: int = 1000) -> int:
    return int((await s.execute(text("SELECT audit.seal(:b)"), {"b": batch})).scalar_one())


async def ensure_audit_partitions(s: AsyncSession) -> None:
    await s.execute(text("SELECT audit.ensure_partitions(2)"))
