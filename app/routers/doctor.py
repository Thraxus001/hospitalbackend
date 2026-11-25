from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_doctor, current_user
from ..models import Appointment, Doctor, Notification, User, utcnow
from ..schemas import AppointmentOut, DoctorOut, NotificationOut
from ..services.audit import audit
from ..services.slots import TZ, day_bounds
from .doctors import to_out

router = APIRouter(prefix="/doctor", tags=["doctor"])


@router.get("/me", response_model=DoctorOut)
def me(doc: Doctor = Depends(current_doctor)):
    return to_out(doc)


@router.get("/appointments", response_model=list[AppointmentOut])
def day_list(
    day: str | None = None,
    all_upcoming: bool = False,
    status: str | None = None,
    db: Session = Depends(get_db),
    doc: Doctor = Depends(current_doctor),
):
    q = db.query(Appointment).filter(Appointment.doctor_id == doc.id)
    if not all_upcoming and day:
        d = datetime.strptime(day, "%Y-%m-%d").date()
        lo, hi = day_bounds(d)
        q = q.filter(Appointment.start_at >= lo, Appointment.start_at < hi)
    elif not all_upcoming and not day:
        today = datetime.now(TZ).date()
        lo, hi = day_bounds(today)
        q = q.filter(Appointment.start_at >= lo, Appointment.start_at < hi)

    if status:
        q = q.filter(Appointment.status == status)
    else:
        q = q.filter(Appointment.status.in_(("confirmed", "completed", "no_show", "pending_payment")))

    return q.order_by(Appointment.start_at).all()


@router.get("/upcoming", response_model=list[AppointmentOut])
def upcoming(hours: int = 24, db: Session = Depends(get_db), doc: Doctor = Depends(current_doctor)):
    now = datetime.now(timezone.utc)
    return (
        db.query(Appointment)
        .filter(Appointment.doctor_id == doc.id, Appointment.status == "confirmed",
                Appointment.end_at > now, Appointment.start_at <= now + timedelta(hours=min(hours, 72)))
        .order_by(Appointment.start_at).all()
    )


def _mark(db: Session, doc: Doctor, user: User, appt_id: int, new: str) -> Appointment:
    a = db.get(Appointment, appt_id)
    if not a or a.doctor_id != doc.id:
        raise HTTPException(404, "Appointment not found")
    if a.status not in ("confirmed", "completed", "no_show"):
        raise HTTPException(409, f"Cannot mark a {a.status} appointment as {new}")
    if new == "no_show" and a.start_at > datetime.now(timezone.utc):
        raise HTTPException(409, "Cannot mark as no-show before the appointment start time")
    a.status = new
    audit(db, user, f"appointment.{new}", f"#{a.id} by {doc.full_name}")
    db.commit()
    return a


@router.post("/appointments/{appt_id}/complete", response_model=AppointmentOut)
def complete(appt_id: int, db: Session = Depends(get_db), doc: Doctor = Depends(current_doctor), user: User = Depends(current_user)):
    return _mark(db, doc, user, appt_id, "completed")


@router.post("/appointments/{appt_id}/no-show", response_model=AppointmentOut)
def no_show(appt_id: int, db: Session = Depends(get_db), doc: Doctor = Depends(current_doctor), user: User = Depends(current_user)):
    return _mark(db, doc, user, appt_id, "no_show")


# ---- dashboard notification feed ----
def _feed(db: Session, user: User):
    return db.query(Notification).filter(Notification.user_id == user.id, Notification.channel == "in_app")


@router.get("/notifications", response_model=list[NotificationOut])
def feed(unread_only: bool = False, limit: int = 50, db: Session = Depends(get_db),
         user: User = Depends(current_user), _: Doctor = Depends(current_doctor)):
    q = _feed(db, user)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    return q.order_by(Notification.created_at.desc()).limit(min(limit, 200)).all()


@router.post("/notifications/read-all")
def read_all(db: Session = Depends(get_db), user: User = Depends(current_user), _: Doctor = Depends(current_doctor)):
    _feed(db, user).filter(Notification.read_at.is_(None)).update({"read_at": utcnow()}, synchronize_session=False)
    db.commit()
    return {"ok": True}


@router.post("/notifications/{nid}/read")
def read_one(nid: int, db: Session = Depends(get_db), user: User = Depends(current_user), _: Doctor = Depends(current_doctor)):
    n = _feed(db, user).filter(Notification.id == nid).first()
    if not n:
        raise HTTPException(404, "Notification not found")
    n.read_at = n.read_at or utcnow()
    db.commit()
    return {"ok": True}
