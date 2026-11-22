from datetime import datetime, time, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Time, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# Statuses that occupy a slot. Only one such row may exist per (doctor, start time).
ACTIVE_STATUSES = ("pending_payment", "confirmed")

# Priority: lower number = served first
PRIORITY_EMERGENCY, PRIORITY_HIGH, PRIORITY_NORMAL = 0, 1, 2


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(120), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default="patient")  # patient | admin
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Doctor(Base):
    __tablename__ = "doctors"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    specialty: Mapped[str] = mapped_column(String(80), index=True)
    hospital_location: Mapped[str] = mapped_column(String(120), default="")
    rating: Mapped[float] = mapped_column(Float, default=0)
    slot_minutes: Mapped[int] = mapped_column(Integer, default=20)  # 20-30
    consultation_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    max_daily_appointments: Mapped[int] = mapped_column(Integer, default=20)
    active: Mapped[bool] = mapped_column(default=True)
    schedules: Mapped[list["DoctorSchedule"]] = relationship(back_populates="doctor", cascade="all, delete-orphan")


class DoctorSchedule(Base):
    """A visiting window, e.g. every Tuesday 09:00-13:00 (local time)."""
    __tablename__ = "doctor_schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Mon
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    doctor: Mapped[Doctor] = relationship(back_populates="schedules")


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # pending_payment | confirmed | completed | no_show | rescheduled | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending_payment", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=PRIORITY_NORMAL)
    fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    amount_due: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rescheduled_from_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    doctor: Mapped[Doctor] = relationship()
    patient: Mapped[User] = relationship()
    payments: Mapped[list["Payment"]] = relationship(back_populates="appointment")

    __table_args__ = (
        # The database itself guarantees no double-booking, even under concurrent requests.
        Index(
            "uq_active_slot", "doctor_id", "start_at", unique=True,
            postgresql_where=text("status IN ('pending_payment','confirmed')"),
        ),
    )


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"), index=True)
    method: Mapped[str] = mapped_column(String(20))  # mpesa | card | bank | insurance
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|paid|failed|refund_due|refunded
    checkout_request_id: Mapped[str | None] = mapped_column(String(80), index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    appointment: Mapped[Appointment] = relationship(back_populates="payments")


class Department(Base):
    """Curated department list managed by admin. Specialty names on Doctor rows should match these names."""
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    icon: Mapped[str | None] = mapped_column(String(50))   # department icon / identifier
    active: Mapped[bool] = mapped_column(default=True)


class HospitalSettings(Base):
    """Single-row config (always id=1). Stores comma-separated allowed payment methods and JSON-like insurance list."""
    __tablename__ = "hospital_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_methods: Mapped[str] = mapped_column(String(200), default="mpesa,card,insurance")
    insurance_providers: Mapped[str] = mapped_column(String(2000), default="")   # comma-separated names


class UserPaymentPreference(Base):
    """Patient's saved default payment preferences."""
    __tablename__ = "user_payment_preferences"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    preferred_method: Mapped[str | None] = mapped_column(String(20))   # mpesa | card | insurance
    insurance_provider: Mapped[str | None] = mapped_column(String(80))
    insurance_member_no: Mapped[str | None] = mapped_column(String(80))
    mpesa_phone: Mapped[str | None] = mapped_column(String(20))
    user: Mapped["User"] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[User] = relationship(foreign_keys=[admin_id])


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))          # confirmed | reminder | cancelled | new_booking
    channel: Mapped[str] = mapped_column(String(10))       # email | sms | in_app
    recipient: Mapped[str] = mapped_column(String(120))
    subject: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(String(2000), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|sent|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(300))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class ReferralCode(Base):
    """GP referral ticket assigned by admin, consumed when booking a specialist."""
    __tablename__ = "referral_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)   # uppercase alphanum, max 8
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)   # target specialist
    issued_by: Mapped[int] = mapped_column(ForeignKey("users.id"))                  # admin user
    used: Mapped[bool] = mapped_column(default=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    patient: Mapped[User] = relationship(foreign_keys=[patient_id])
    doctor: Mapped[Doctor] = relationship()
    issuer: Mapped[User] = relationship(foreign_keys=[issued_by])
