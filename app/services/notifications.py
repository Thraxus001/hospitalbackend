"""Email + SMS + dashboard notifications.

Design: business code calls `notify_*` which only INSERTS outbox rows (same DB transaction as the
booking/payment change). A background worker (`notify_loop`) creates due reminders and delivers
pending rows, retrying failures. `dedupe_key` is unique, so a reminder can never be sent twice, even
with several API workers running.
"""
import asyncio
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..database import SessionLocal
from ..models import Appointment, Notification, Payment, User, utcnow
from .slots import TZ

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


# ---------------------------------------------------------------- outbox
def enqueue(db: Session, user: User | None, kind: str, subject: str, body: str, dedupe: str,
            appointment_id: int | None = None, channels: tuple[str, ...] = ("email", "sms")) -> None:
    if user is None:
        return
    targets = {"email": user.email, "sms": user.phone, "in_app": user.full_name}
    for ch in channels:
        if not targets.get(ch):
            continue  # e.g. patient without an email address: SMS only
        n = Notification(
            user_id=user.id, appointment_id=appointment_id, kind=kind, channel=ch, recipient=targets[ch],
            subject=subject, body=body, dedupe_key=f"{dedupe}:{ch}",
            status="sent" if ch == "in_app" else "pending", sent_at=utcnow() if ch == "in_app" else None,
        )
        try:
            with db.begin_nested():
                db.add(n)
                db.flush()
        except IntegrityError:
            pass  # already queued


def _when(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b, %H:%M")


def _in(mins: float) -> str:
    if mins < 90:
        return f"in {max(1, round(mins))} minutes"
    if mins < 36 * 60:
        return f"in about {round(mins / 60)} hours"
    return f"in about {round(mins / 1440)} days"


def _place(a: Appointment) -> str:
    return f" at {a.doctor.hospital_location}" if a.doctor.hospital_location else ""


def _ts(a: Appointment) -> int:
    return int(a.start_at.timestamp())


# ---------------------------------------------------------------- event notifications
def notify_booking_confirmed(db: Session, a: Appointment, label: str = "Appointment confirmed") -> None:
    body = f"{label}: {a.doctor.full_name} ({a.doctor.specialty}), {_when(a.start_at)}{_place(a)}. Booking #{a.id}."
    enqueue(db, a.patient, "confirmed", label, f"Hello {a.patient.full_name.split()[0]}. {body}", f"confirmed:{a.id}:{_ts(a)}:p", a.id)
    enqueue(db, a.doctor.user, "new_booking", f"New booking: {a.patient.full_name}",
            f"{a.patient.full_name} booked {_when(a.start_at)}. {label}.", f"newbook:{a.id}:{_ts(a)}:d", a.id, ("in_app",))


def notify_moved(db: Session, a: Appointment, old_start: datetime, reason: str) -> None:
    body = (f"Your appointment with {a.doctor.full_name} was moved from {_when(old_start)} to {_when(a.start_at)}{_place(a)}. "
            f"{reason} We apologise for the change.")
    enqueue(db, a.patient, "moved", "Appointment time changed", body, f"moved:{a.id}:{_ts(a)}:p", a.id)
    enqueue(db, a.doctor.user, "moved", f"Rescheduled: {a.patient.full_name}",
            f"{a.patient.full_name} moved from {_when(old_start)} to {_when(a.start_at)}.", f"moved:{a.id}:{_ts(a)}:d", a.id, ("in_app",))


def notify_cancelled(db: Session, a: Appointment) -> None:
    enqueue(db, a.doctor.user, "cancelled", f"Cancelled: {a.patient.full_name}",
            f"{a.patient.full_name} cancelled the {_when(a.start_at)} appointment.", f"cancel:{a.id}:{_ts(a)}:d", a.id, ("in_app",))


def notify_refund_due(db: Session, a: Appointment) -> None:
    enqueue(db, a.patient, "refund_due", "Refund on its way",
            f"Your appointment #{a.id} was cancelled. Your refund is being processed and will reach you shortly.",
            f"refunddue:{a.id}:p", a.id)


def notify_refunded(db: Session, p: Payment) -> None:
    a = p.appointment
    enqueue(db, a.patient, "refunded", "Refund sent",
            f"We have refunded KES {p.amount:,.0f} for appointment #{a.id}. Thank you.", f"refunded:{p.id}:p", a.id)


# ---------------------------------------------------------------- reminders
def _offsets(csv: str) -> list[int]:
    return sorted({int(x) for x in csv.split(",") if x.strip()})


def _first_at_least(offsets: list[int], mins: float) -> int | None:
    return next((o for o in offsets if mins <= o), None)


def schedule_reminders(db: Session, now: datetime | None = None) -> int:
    """Create due reminders. For each appointment we pick the smallest configured offset that still
    covers the time left, so a booking made 1 hour ahead gets one sensible reminder, not three."""
    now = now or datetime.now(timezone.utc)
    p_off = _offsets(settings.reminder_offsets_minutes)
    d_off = _offsets(settings.doctor_reminder_offsets_minutes)
    horizon = max(p_off + d_off + [0])
    appts = (
        db.query(Appointment)
        .options(joinedload(Appointment.doctor), joinedload(Appointment.patient))
        .filter(
            Appointment.status == "confirmed",
            Appointment.start_at > now + timedelta(minutes=settings.min_reminder_lead_minutes),
            Appointment.start_at <= now + timedelta(minutes=horizon),
        )
        .all()
    )
    created = 0
    for a in appts:
        mins = (a.start_at - now).total_seconds() / 60

        off = _first_at_least(p_off, mins)
        if off is not None:
            body = (f"Reminder: your appointment with {a.doctor.full_name} ({a.doctor.specialty}) is {_in(mins)}, "
                    f"{_when(a.start_at)}{_place(a)}. Please arrive 10 minutes early.")
            enqueue(db, a.patient, f"reminder_{off}m", "Appointment reminder", body, f"rem:{a.id}:{_ts(a)}:{off}:p", a.id)
            created += 1

        off = _first_at_least(d_off, mins)
        if off is not None:
            final = off == d_off[0]  # the closest (e.g. 30-minute) reminder
            channels = ("in_app", "email", "sms") if final and settings.notify_doctors_external else ("in_app",)
            body = f"Next patient {_in(mins)}: {a.patient.full_name} at {_when(a.start_at)}."
            enqueue(db, a.doctor.user, f"doctor_reminder_{off}m", f"Upcoming: {a.patient.full_name}", body,
                    f"drem:{a.id}:{_ts(a)}:{off}:d", a.id, channels)
            created += 1
    return created


# ---------------------------------------------------------------- delivery
def send_email(to: str, subject: str, body: str) -> str | None:
    if not settings.smtp_host:
        log.info("[dry-run email] to=%s subject=%s\n%s", to, subject, body)
        return "dry-run"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    return None


def send_sms(to: str, body: str) -> str | None:
    if not settings.at_api_key:
        log.info("[dry-run sms] to=%s: %s", to, body)
        return "dry-run"
    host = "api.sandbox.africastalking.com" if settings.at_env == "sandbox" else "api.africastalking.com"
    data = {"username": settings.at_username, "to": "+" + to.lstrip("+"), "message": body}
    if settings.at_sender_id:
        data["from"] = settings.at_sender_id
    r = httpx.post(f"https://{host}/version1/messaging", data=data, timeout=20,
                   headers={"apiKey": settings.at_api_key, "Accept": "application/json"})
    r.raise_for_status()
    recipients = r.json().get("SMSMessageData", {}).get("Recipients", [])
    bad = [x.get("status") for x in recipients if x.get("status") != "Success"]
    if bad or not recipients:
        raise RuntimeError(f"SMS rejected: {bad or r.text[:120]}")
    return None


def deliver_pending(db: Session, batch: int = 50) -> int:
    rows = (
        db.query(Notification)
        .filter(Notification.status == "pending", Notification.channel.in_(("email", "sms")), Notification.attempts < MAX_ATTEMPTS)
        .order_by(Notification.id).limit(batch)
        .with_for_update(skip_locked=True).all()
    )
    for n in rows:
        try:
            note = send_email(n.recipient, n.subject, n.body) if n.channel == "email" else send_sms(n.recipient, n.body)
            n.status, n.sent_at, n.error = ("dry_run" if note else "sent"), utcnow(), note
        except Exception as e:  # network/provider problems: retry up to MAX_ATTEMPTS
            n.attempts += 1
            n.error = str(e)[:300]
            n.status = "failed" if n.attempts >= MAX_ATTEMPTS else "pending"
            log.warning("notification %s failed (%s): %s", n.id, n.channel, e)
    db.commit()
    return len(rows)


def run_cycle() -> None:
    with SessionLocal() as db:
        schedule_reminders(db)
        db.commit()
        deliver_pending(db)


async def notify_loop(interval: int = 30):
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, run_cycle)
        except Exception:
            log.exception("notification cycle failed")
        await asyncio.sleep(interval)
