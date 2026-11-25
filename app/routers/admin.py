from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..models import Appointment, AuditLog, Department, Doctor, DoctorSchedule, HospitalSettings, Notification, Payment, User
from ..schemas import (
    AdminNotificationOut,
    AdminPatientIn,
    AdminPaymentOut,
    AdminDoctorOut,
    AppointmentOut,
    AuditRowOut,
    DepartmentIn,
    DepartmentOut,
    DoctorIn,
    DoctorOut,
    DoctorAccountIn,
    HospitalSettingsIn,
    HospitalSettingsOut,
    PriorityBookIn,
    ScheduleIn,
    ScheduleOut,
    UserOut,
)
from ..services.audit import audit
from ..services.notifications import notify_refunded
from ..services.priority import create_priority_appointment
from ..services.slots import TZ, day_bounds
from .doctors import to_out

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

@router.get("/doctors", response_model=list[AdminDoctorOut])
def list_doctors(db: Session = Depends(get_db), current_user=Depends(require_admin)):
    doctors = db.query(Doctor).order_by(Doctor.id).all()
    doctor_logins = {
        name for (name,) in db.query(User.full_name).filter(User.role == "doctor").all()
    }
    return [
        AdminDoctorOut(
            **to_out(doctor).model_dump(),
            max_daily_appointments=doctor.max_daily_appointments,
            active=doctor.active,
            has_login=doctor.full_name in doctor_logins,
        )
        for doctor in doctors
    ]

@router.post("/doctors", response_model=DoctorOut, status_code=201)
def add_doctor(body: DoctorIn, db: Session = Depends(get_db)):
    doc_data = body.model_dump()
    login_phone = doc_data.pop("login_phone", None)
    login_email = doc_data.pop("login_email", None)
    login_password = doc_data.pop("login_password", None)

    d = Doctor(**doc_data)
    db.add(d)
    db.commit()
    db.refresh(d)

    if login_phone and login_password:
        from ..security import hash_password
        clean_phone = login_phone.strip()
        user = db.query(User).filter((User.phone == clean_phone) | (User.full_name == d.full_name)).first()
        if user:
            user.role = "doctor"
            user.phone = clean_phone
            user.email = login_email.strip() if login_email else None
            user.hashed_password = hash_password(login_password)
        else:
            user = User(
                full_name=d.full_name,
                phone=clean_phone,
                email=login_email.strip() if login_email else None,
                hashed_password=hash_password(login_password),
                role="doctor",
            )
            db.add(user)
        db.commit()

    return to_out(d)


@router.post("/doctors/{doctor_id}/account", status_code=201)
def create_doctor_account(
    doctor_id: int,
    body: DoctorAccountIn,
    db: Session = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    d = db.get(Doctor, doctor_id)
    if not d:
        raise HTTPException(404, "Doctor not found")

    from ..security import hash_password
    clean_phone = body.phone.strip()
    clean_email = body.email.strip() if body.email and body.email.strip() else None

    user = db.query(User).filter((User.phone == clean_phone) | (User.full_name == d.full_name)).first()
    if user:
        user.full_name = d.full_name
        user.phone = clean_phone
        user.email = clean_email
        user.hashed_password = hash_password(body.password)
        user.role = "doctor"
    else:
        user = User(
            full_name=d.full_name,
            phone=clean_phone,
            email=clean_email,
            hashed_password=hash_password(body.password),
            role="doctor",
        )
        db.add(user)

    audit(db, current_admin, "doctor.account_created", f"Login created for {d.full_name} ({clean_phone})")
    db.commit()
    return {"ok": True, "user_id": user.id}


@router.post("/doctors/{doctor_id}/schedule", status_code=201)
def add_schedule(doctor_id: int, body: ScheduleIn, db: Session = Depends(get_db)):
    if not db.get(Doctor, doctor_id):
        raise HTTPException(404, "Doctor not found")
    if body.end_time <= body.start_time:
        raise HTTPException(422, "end_time must be after start_time")
    db.add(DoctorSchedule(doctor_id=doctor_id, **body.model_dump()))
    db.commit()
    return {"ok": True}


@router.get("/doctors/{doctor_id}/schedule", response_model=list[ScheduleOut])
def list_schedule(doctor_id: int, db: Session = Depends(get_db)):
    if not db.get(Doctor, doctor_id):
        raise HTTPException(404, "Doctor not found")
    return (
        db.query(DoctorSchedule)
        .filter(DoctorSchedule.doctor_id == doctor_id)
        .order_by(DoctorSchedule.weekday, DoctorSchedule.start_time)
        .all()
    )


@router.delete("/schedule/{schedule_id}")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)):
    schedule = db.get(DoctorSchedule, schedule_id)
    if not schedule:
        raise HTTPException(404, "Schedule window not found")
    db.delete(schedule)
    db.commit()
    return {"ok": True}


@router.get("/appointments", response_model=list[AppointmentOut])
def list_appointments(day: date | None = None, doctor_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(Appointment)
    if day:
        lo, hi = day_bounds(day)
        q = q.filter(Appointment.start_at >= lo, Appointment.start_at < hi)
    if doctor_id:
        q = q.filter(Appointment.doctor_id == doctor_id)
    return q.order_by(Appointment.start_at, Appointment.priority).all()


@router.get("/payments", response_model=list[AdminPaymentOut])
def list_payments(status: str | None = None, db: Session = Depends(get_db)):
    query = (
        db.query(Payment, Appointment, User, Doctor)
        .join(Appointment, Payment.appointment_id == Appointment.id)
        .join(User, Appointment.patient_id == User.id)
        .join(Doctor, Appointment.doctor_id == Doctor.id)
    )
    if status:
        query = query.filter(Payment.status == status)
    rows = query.order_by(Payment.created_at.desc(), Payment.id.desc()).all()
    return [
        AdminPaymentOut(
            id=payment.id,
            appointment_id=appointment.id,
            patient=patient.full_name,
            doctor=doctor.full_name,
            method=payment.method,
            amount=payment.amount,
            status=payment.status,
            provider_ref=payment.provider_ref,
            created_at=payment.created_at,
            appointment_at=appointment.start_at,
        )
        for payment, appointment, patient, doctor in rows
    ]


@router.post("/payments/{payment_id}/refunded")
def mark_payment_refunded(
    payment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status != "refund_due":
        raise HTTPException(409, f"Cannot mark a {payment.status} payment as refunded")
    payment.status = "refunded"
    notify_refunded(db, payment)
    audit(db, current_user, "payment.refunded", f"Payment #{payment.id}")
    db.commit()
    return {"ok": True}


@router.get("/notifications", response_model=list[AdminNotificationOut])
def list_notifications(
    status: str | None = None,
    channel: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(Notification, User).join(User, Notification.user_id == User.id)
    if status:
        query = query.filter(Notification.status == status)
    if channel:
        query = query.filter(Notification.channel == channel)
    rows = query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(500).all()
    return [
        AdminNotificationOut(
            id=notification.id,
            kind=notification.kind,
            channel=notification.channel,
            subject=notification.subject,
            body=notification.body,
            status=notification.status,
            appointment_id=notification.appointment_id,
            created_at=notification.created_at,
            read_at=notification.read_at,
            recipient=notification.recipient,
            user_name=user.full_name,
            attempts=notification.attempts,
            error=notification.error,
        )
        for notification, user in rows
    ]


@router.post("/notifications/{notification_id}/retry")
def retry_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(404, "Notification not found")
    if notification.status not in ("failed", "dry_run") or notification.channel not in ("email", "sms"):
        raise HTTPException(409, "Only failed or dry-run email or SMS notifications can be retried")
    notification.status = "pending"
    notification.attempts = 0
    notification.error = None
    notification.sent_at = None
    audit(db, current_user, "notification.retry", f"Notification #{notification.id}")
    db.commit()
    return {"ok": True}


def _set_status(db: Session, appt_id: int, allowed_from: tuple[str, ...], new: str) -> Appointment:
    a = db.get(Appointment, appt_id)
    if not a:
        raise HTTPException(404, "Appointment not found")
    if a.status not in allowed_from:
        raise HTTPException(409, f"Cannot mark a {a.status} appointment as {new}")
    a.status = new
    db.commit()
    return a


@router.post("/appointments/{appt_id}/no-show", response_model=AppointmentOut)
def mark_no_show(appt_id: int, db: Session = Depends(get_db)):
    """Fee is retained; the patient may now reschedule at normal priority."""
    return _set_status(db, appt_id, ("confirmed",), "no_show")


@router.post("/appointments/{appt_id}/complete", response_model=AppointmentOut)
def mark_complete(appt_id: int, db: Session = Depends(get_db)):
    return _set_status(db, appt_id, ("confirmed",), "completed")


@router.post("/appointments/priority", status_code=201)
def book_priority(body: PriorityBookIn, db: Session = Depends(get_db)):
    """Emergency / high-priority walk-in. May displace a rescheduled normal-priority appointment."""
    doctor = db.get(Doctor, body.doctor_id)
    if not doctor or not db.get(User, body.patient_id):
        raise HTTPException(404, "Doctor or patient not found")
    appt, bumped = create_priority_appointment(db, doctor, body.patient_id, body.start_at, body.priority)
    db.commit()
    return {
        "appointment": AppointmentOut.model_validate(appt),
        "displaced": AppointmentOut.model_validate(bumped) if bumped else None,
    }


@router.get("/reports/summary")
def summary(days: int = 30, db: Session = Depends(get_db)):
    today = datetime.now(TZ).date()
    since = today - timedelta(days=days)

    def local_time(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(TZ)

    def paid(method=None):
        q = db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(Payment.status == "paid", Payment.created_at >= since)
        return float(q.filter(Payment.method == method).scalar() if method else q.scalar())

    daily_totals = {
        (since + timedelta(days=offset)).isoformat(): 0.0
        for offset in range(days + 1)
    }
    paid_rows = (
        db.query(Payment.amount, Payment.created_at)
        .filter(Payment.status == "paid", Payment.created_at >= since)
        .all()
    )
    for amount, created_at in paid_rows:
        day = local_time(created_at).date().isoformat()
        if day in daily_totals:
            daily_totals[day] += float(amount)

    hourly_bookings: dict[str, int] = {}
    booking_times = (
        db.query(Appointment.created_at)
        .filter(Appointment.created_at >= since)
        .all()
    )
    for (created_at,) in booking_times:
        hour = str(local_time(created_at).hour)
        hourly_bookings[hour] = hourly_bookings.get(hour, 0) + 1

    counts = dict(
        db.query(Appointment.status, func.count()).filter(Appointment.created_at >= since).group_by(Appointment.status).all()
    )
    attended, missed = counts.get("completed", 0), counts.get("no_show", 0)

    # Waiting = pending_payment (held, not yet paid) + confirmed (paid, awaiting visit)
    waiting = counts.get("pending_payment", 0) + counts.get("confirmed", 0)
    total_appointments = sum(counts.values())

    per_doctor = (
        db.query(Doctor.full_name, func.count(Appointment.id))
        .join(Appointment, Appointment.doctor_id == Doctor.id)
        .filter(Appointment.status.in_(("confirmed", "completed")), Appointment.created_at >= since)
        .group_by(Doctor.full_name).all()
    )
    return {
        "period_days": days,
        "collected": paid(),
        "collected_by_method": {m: paid(m) for m in ("mpesa", "card", "bank", "insurance")},
        "pending_payments": float(
            db.query(func.coalesce(func.sum(Appointment.amount_due), 0)).filter(Appointment.status == "pending_payment").scalar()
        ),
        "refunds_due": float(
            db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(Payment.status == "refund_due").scalar()
        ),
        "appointments_by_status": counts,
        "total_appointments": total_appointments,
        "waiting": waiting,
        "no_shows": missed,
        "counselled": attended,
        "turnout_rate": round(attended / (attended + missed), 3) if attended + missed else None,
        "bookings_per_doctor": {n: c for n, c in per_doctor},
        "daily_collected": [
            {"date": day, "amount": amount}
            for day, amount in daily_totals.items()
        ],
        "bookings_by_hour": hourly_bookings,
    }


# ---- patients ----

@router.get("/patients", response_model=list[UserOut])
def list_patients(q: str | None = None, db: Session = Depends(get_db)):
    query = db.query(User).filter(User.role == "patient")
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            (User.full_name.ilike(term)) | (User.phone.ilike(term)) | (User.email.ilike(term))
        )
    return query.order_by(User.full_name).limit(100).all()


@router.post("/patients", response_model=UserOut, status_code=201)
def create_patient(body: AdminPatientIn, db: Session = Depends(get_db)):
    from ..security import hash_password
    clean_phone = body.phone.strip()
    clean_email = body.email.strip() if body.email and body.email.strip() else None

    existing = db.query(User).filter(User.phone == clean_phone).first()
    if existing:
        raise HTTPException(409, f"A patient with phone number {clean_phone} already exists ({existing.full_name})")
    if clean_email:
        existing_email = db.query(User).filter(User.email == clean_email).first()
        if existing_email:
            raise HTTPException(409, f"A patient with email {clean_email} already exists")

    user = User(
        full_name=body.full_name.strip(),
        phone=clean_phone,
        email=clean_email,
        hashed_password=hash_password("tempPass1234"),
        role="patient",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---- departments ----

@router.get("/departments", response_model=list[DepartmentOut])
def list_departments(db: Session = Depends(get_db)):
    depts = db.query(Department).order_by(Department.name).all()
    out = []
    for dept in depts:
        d = DepartmentOut.model_validate(dept)
        d.doctor_count = db.query(Doctor).filter(
            Doctor.specialty.ilike(dept.name), Doctor.active.is_(True)
        ).count()
        out.append(d)
    return out


@router.post("/departments", response_model=DepartmentOut, status_code=201)
def add_department(body: DepartmentIn, db: Session = Depends(get_db)):
    if db.query(Department).filter(Department.name.ilike(body.name)).first():
        from fastapi import HTTPException
        raise HTTPException(409, "Department already exists")
    dept = Department(**body.model_dump())
    db.add(dept)
    db.commit()
    d = DepartmentOut.model_validate(dept)
    d.doctor_count = 0
    return d


@router.delete("/departments/{dept_id}", status_code=204)
def delete_department(dept_id: int, db: Session = Depends(get_db)):
    dept = db.get(Department, dept_id)
    if not dept:
        from fastapi import HTTPException
        raise HTTPException(404, "Department not found")
    db.delete(dept)
    db.commit()


# ---- hospital settings (payment methods + insurance providers) ----

def _get_settings(db: Session) -> HospitalSettings:
    s = db.get(HospitalSettings, 1)
    if not s:
        s = HospitalSettings(id=1, payment_methods="mpesa,card,insurance", insurance_providers="")
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


@router.get("/settings", response_model=HospitalSettingsOut)
def get_settings(db: Session = Depends(get_db)):
    s = _get_settings(db)
    return HospitalSettingsOut(
        payment_methods=[m.strip() for m in s.payment_methods.split(",") if m.strip()],
        insurance_providers=[p.strip() for p in s.insurance_providers.split(",") if p.strip()],
    )


@router.put("/settings", response_model=HospitalSettingsOut)
def update_settings(body: HospitalSettingsIn, db: Session = Depends(get_db)):
    s = _get_settings(db)
    s.payment_methods = ",".join(body.payment_methods)
    s.insurance_providers = ",".join(body.insurance_providers)
    db.commit()
    return HospitalSettingsOut(
        payment_methods=body.payment_methods,
        insurance_providers=body.insurance_providers,
    )


# ---- audit log ----

@router.get("/audit", response_model=list[AuditRowOut])
def audit_log(limit: int = 200, db: Session = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 500)).all()
    out = []
    for r in rows:
        a = AuditRowOut.model_validate(r)
        a.admin = r.actor.full_name if r.actor else str(r.admin_id)
        out.append(a)
    return out
