from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import current_user
from ..models import Appointment, Doctor, ReferralCode, User
from ..schemas import AppointmentOut, BookIn, RescheduleIn
from ..services.slots import TZ, free_slots

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _to_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_past(dt: datetime | None) -> bool:
    if not dt:
        return False
    return _to_aware(dt) < datetime.now(timezone.utc)


def _own(db: Session, appt_id: int, user: User) -> Appointment:
    a = db.get(Appointment, appt_id)
    if not a or (a.patient_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Appointment not found")
    return a


def _require_free(db: Session, doctor: Doctor, start_at: datetime):
    if start_at.tzinfo is None:
        raise HTTPException(422, "start_at must include a timezone offset")
    if start_at not in free_slots(db, doctor, start_at.astimezone(TZ).date()):
        raise HTTPException(409, "That slot is not available")


@router.post("", response_model=AppointmentOut, status_code=201)
def book(body: BookIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    doctor = db.get(Doctor, body.doctor_id)
    if not doctor or not doctor.active:
        raise HTTPException(404, "Doctor not found")
    _require_free(db, doctor, body.start_at)

    # Validate referral code
    code_upper = body.referral_code.upper().strip()
    ref = db.query(ReferralCode).filter_by(code=code_upper).first()
    if not ref or ref.patient_id != user.id or ref.doctor_id != doctor.id:
        raise HTTPException(403, "A valid referral code is required to book this specialist.")
    if ref.used:
        raise HTTPException(409, "This referral code has already been used.")

    appt = Appointment(
        patient_id=user.id, doctor_id=doctor.id, start_at=body.start_at,
        end_at=body.start_at + timedelta(minutes=doctor.slot_minutes),
        status="pending_payment", fee=doctor.consultation_fee, amount_due=doctor.consultation_fee,
        hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.hold_minutes),
    )
    db.add(appt)
    try:
        db.commit()  # unique partial index rejects a concurrent double-booking
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That slot was just taken")

    # Mark referral code as consumed
    ref.used = True
    ref.used_at = datetime.now(timezone.utc)
    db.commit()
    return appt


@router.get("", response_model=list[AppointmentOut])
def my_appointments(db: Session = Depends(get_db), user: User = Depends(current_user)):
    now_utc = datetime.now(timezone.utc)
    now_naive = datetime.utcnow()
    # Auto-release expired holds on-the-fly
    db.query(Appointment).filter(
        Appointment.patient_id == user.id,
        Appointment.status == "pending_payment",
        (Appointment.hold_expires_at < now_utc) | (Appointment.hold_expires_at < now_naive),
    ).update({"status": "cancelled"}, synchronize_session=False)
    db.commit()
    return db.query(Appointment).filter_by(patient_id=user.id).order_by(Appointment.start_at.desc()).all()


@router.get("/{appt_id}", response_model=AppointmentOut)
def get_one(appt_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    a = _own(db, appt_id, user)
    if a.status == "pending_payment" and _is_past(a.hold_expires_at):
        a.status = "cancelled"
        db.commit()
    return a


@router.post("/{appt_id}/cancel", response_model=AppointmentOut)
def cancel(appt_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    a = _own(db, appt_id, user)
    if a.status == "cancelled":
        return a
    if a.status not in ("pending_payment", "confirmed"):
        raise HTTPException(409, f"Cannot cancel a {a.status} appointment")
    # Refund only when cancelled 24h+ ahead; late cancellations keep the fee (no-show protection).
    if a.status == "confirmed" and _to_aware(a.start_at) - datetime.now(timezone.utc) >= timedelta(hours=24):
        for p in a.payments:
            if p.status == "paid":
                p.status = "refund_due"
    a.status = "cancelled"
    db.commit()
    return a


@router.post("/{appt_id}/reschedule", response_model=AppointmentOut)
def reschedule(appt_id: int, body: RescheduleIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Confirmed booking (2h+ before start): move it. No-show: re-book without paying again."""
    a = _own(db, appt_id, user)
    _require_free(db, a.doctor, body.start_at)
    step = timedelta(minutes=a.doctor.slot_minutes)

    try:
        if a.status == "confirmed":
            if _to_aware(a.start_at) - datetime.now(timezone.utc) < timedelta(hours=2):
                raise HTTPException(409, "Too late to move this appointment; it is within 2 hours")
            a.start_at, a.end_at = body.start_at, body.start_at + step
            result = a
        elif a.status == "no_show":
            # Fee was already collected upfront, so the new appointment is confirmed straight away
            # at NORMAL priority - emergencies can bump it later (see services/priority.py).
            a.status = "rescheduled"
            result = Appointment(
                patient_id=a.patient_id, doctor_id=a.doctor_id, start_at=body.start_at,
                end_at=body.start_at + step, status="confirmed", priority=2,
                fee=a.fee, amount_due=Decimal("0"), rescheduled_from_id=a.id,
            )
            db.add(result)
        else:
            raise HTTPException(409, f"Cannot reschedule a {a.status} appointment")
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That slot was just taken")
    return result
