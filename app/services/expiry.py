import asyncio
import logging
from datetime import datetime, timezone

from ..database import SessionLocal
from ..models import Appointment

log = logging.getLogger(__name__)


def release_expired_holds() -> int:
    with SessionLocal() as db:
        now_utc = datetime.now(timezone.utc)
        now_naive = datetime.utcnow()
        n = (
            db.query(Appointment)
            .filter(
                Appointment.status == "pending_payment",
                (Appointment.hold_expires_at < now_utc) | (Appointment.hold_expires_at < now_naive),
            )
            .update({"status": "cancelled"}, synchronize_session=False)
        )
        db.commit()
        return n


async def expiry_loop(interval: int = 5):
    """Unpaid bookings free their slot automatically once the 2-minute hold times out."""
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, release_expired_holds)
        except Exception:
            log.exception("hold expiry failed")
        await asyncio.sleep(interval)
