from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..config import settings
from ..models import ACTIVE_STATUSES, Appointment, Doctor, DoctorSchedule

TZ = ZoneInfo(settings.timezone)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(day, time.min, tzinfo=TZ)
    return lo, lo + timedelta(days=1)


def all_slots(db: Session, doctor: Doctor, day: date) -> list[datetime]:
    """Every fixed-length slot the doctor works on `day` (20-30 min each, never overlapping)."""
    step = timedelta(minutes=doctor.slot_minutes)
    windows = (
        db.query(DoctorSchedule)
        .filter_by(doctor_id=doctor.id, weekday=day.weekday())
        .order_by(DoctorSchedule.start_time)
        .all()
    )
    out: list[datetime] = []
    for w in windows:
        cur = datetime.combine(day, w.start_time, tzinfo=TZ)
        end = datetime.combine(day, w.end_time, tzinfo=TZ)
        while cur + step <= end:
            out.append(cur)
            cur += step
    return out


def free_slots(db: Session, doctor: Doctor, day: date) -> list[datetime]:
    lo, hi = day_bounds(day)
    now_utc = datetime.now(timezone.utc)
    now_naive = datetime.utcnow()
    # Release any holds that have passed the 2-minute mark
    db.query(Appointment).filter(
        Appointment.doctor_id == doctor.id,
        Appointment.status == "pending_payment",
        (Appointment.hold_expires_at < now_utc) | (Appointment.hold_expires_at < now_naive),
    ).update({"status": "cancelled"}, synchronize_session=False)
    db.commit()

    taken = {
        a.start_at
        for a in db.query(Appointment).filter(
            Appointment.doctor_id == doctor.id,
            Appointment.start_at >= lo,
            Appointment.start_at < hi,
            Appointment.status.in_(ACTIVE_STATUSES),
        )
    }
    if len(taken) >= doctor.max_daily_appointments:
        return []
    now = datetime.now(TZ)
    return [s for s in all_slots(db, doctor, day) if s not in taken and s > now]


def next_free_slot(db: Session, doctor: Doctor, after: datetime, horizon_days: int = 21) -> datetime | None:
    first = after.astimezone(TZ).date()
    for i in range(horizon_days):
        for s in free_slots(db, doctor, first + timedelta(days=i)):
            if s > after:
                return s
    return None
