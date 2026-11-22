from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import timedelta

from ..database import get_db
from ..models import Doctor
from ..schemas import DoctorOut, SlotOut
from ..services.slots import free_slots, TZ

router = APIRouter(prefix="/doctors", tags=["doctors"])


def to_out(d: Doctor, db: Session | None = None) -> DoctorOut:
    out = DoctorOut.model_validate(d)
    out.working_days = sorted({s.weekday for s in d.schedules})
    if db is not None:
        today = datetime.now(TZ).date()
        out.has_slots_today = len(free_slots(db, d, today)) > 0
    return out


@router.get("", response_model=list[DoctorOut])
def list_doctors(specialty: str | None = None, q: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Doctor).filter(Doctor.active.is_(True))
    if specialty:
        query = query.filter(Doctor.specialty.ilike(specialty))
    if q:
        query = query.filter(Doctor.full_name.ilike(f"%{q}%") | Doctor.specialty.ilike(f"%{q}%"))
    return [to_out(d, db) for d in query.order_by(Doctor.rating.desc(), Doctor.full_name)]


@router.get("/{doctor_id}", response_model=DoctorOut)
def get_doctor(doctor_id: int, db: Session = Depends(get_db)):
    d = db.get(Doctor, doctor_id)
    if not d or not d.active:
        raise HTTPException(404, "Doctor not found")
    return to_out(d, db)


@router.get("/{doctor_id}/slots", response_model=list[SlotOut])
def doctor_slots(doctor_id: int, day: date, db: Session = Depends(get_db)):
    d = db.get(Doctor, doctor_id)
    if not d or not d.active:
        raise HTTPException(404, "Doctor not found")
    step = timedelta(minutes=d.slot_minutes)
    return [SlotOut(start_at=s, end_at=s + step) for s in free_slots(db, d, day)]
