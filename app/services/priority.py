from datetime import timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import ACTIVE_STATUSES, Appointment, Doctor
from .slots import TZ, all_slots, next_free_slot


def create_priority_appointment(
    db: Session, doctor: Doctor, patient_id: int, start_at, priority: int
) -> tuple[Appointment, Appointment | None]:
    """Book an emergency/high-priority patient into `start_at`.

    If that slot is held by a lower-priority *rescheduled* (ex no-show) appointment,
    that appointment is pushed to the doctor's next free slot. A regular paid booking
    is never bumped - the caller gets a 409 and should choose another slot.
    """
    step = timedelta(minutes=doctor.slot_minutes)
    if start_at not in all_slots(db, doctor, start_at.astimezone(TZ).date()):
        raise HTTPException(422, "Not a valid slot for this doctor")

    occupant = (
        db.query(Appointment)
        .filter(
            Appointment.doctor_id == doctor.id,
            Appointment.start_at == start_at,
            Appointment.status.in_(ACTIVE_STATUSES),
        )
        .with_for_update()
        .first()
    )
    bumped = None
    if occupant:
        if occupant.rescheduled_from_id is None or occupant.priority <= priority:
            raise HTTPException(409, "Slot is held by a booking that cannot be displaced")
        new_start = next_free_slot(db, doctor, start_at)
        if not new_start:
            raise HTTPException(409, "No free slot to move the displaced appointment to")
        occupant.start_at, occupant.end_at = new_start, new_start + step
        db.flush()  # free the old slot before inserting the new row
        bumped = occupant

    appt = Appointment(
        patient_id=patient_id, doctor_id=doctor.id, start_at=start_at, end_at=start_at + step,
        status="confirmed", priority=priority,
        fee=doctor.consultation_fee, amount_due=Decimal("0"),  # settled at billing desk
    )
    db.add(appt)
    db.flush()
    return appt, bumped
