import re
import secrets
import string
from datetime import datetime, timezone
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user, require_admin
from ..models import ReferralCode, User
from ..schemas import ReferralCodeIn, ReferralCodeOut, ReferralVerifyIn
from ..services.notifications import notify_referral_issued

router = APIRouter(tags=["referrals"])

_ALPHANUM = string.ascii_uppercase + string.digits
_CODE_RE = re.compile(r"^[A-Z0-9]{1,8}$")

# Rate limiter: ip -> list of monotonic timestamps of failed attempts
_attempt_log: dict[str, list[float]] = {}
_WINDOW = 900  # 15 minutes in seconds
_MAX_ATTEMPTS = 5


def _generate_code() -> str:
    return "".join(secrets.choice(_ALPHANUM) for _ in range(8))


def _check_rate_limit(ip: str) -> None:
    now = monotonic()
    attempts = [t for t in _attempt_log.get(ip, []) if now - t < _WINDOW]
    _attempt_log[ip] = attempts
    if len(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again later.",
        )


def _record_failure(ip: str) -> None:
    now = monotonic()
    attempts = [t for t in _attempt_log.get(ip, []) if now - t < _WINDOW]
    attempts.append(now)
    _attempt_log[ip] = attempts


def _clear_attempts(ip: str) -> None:
    _attempt_log.pop(ip, None)


# ---- Admin endpoints ----

admin_router = APIRouter(prefix="/admin/referral-codes", tags=["admin", "referrals"], dependencies=[Depends(require_admin)])


@admin_router.post("", response_model=ReferralCodeOut, status_code=201)
def issue_referral_code(
    body: ReferralCodeIn,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    from ..models import Doctor
    patient = db.get(User, body.patient_id)
    if not patient or patient.role != "patient":
        raise HTTPException(404, "Patient not found")
    doctor = db.get(Doctor, body.doctor_id)
    if not doctor or not doctor.active:
        raise HTTPException(404, "Doctor not found")

    # Generate a unique code
    for _ in range(10):
        code = _generate_code()
        if not db.query(ReferralCode).filter_by(code=code).first():
            break
    else:
        raise HTTPException(500, "Could not generate a unique code")

    ref = ReferralCode(
        code=code,
        patient_id=body.patient_id,
        doctor_id=body.doctor_id,
        issued_by=current.id,
    )
    db.add(ref)
    db.commit()
    db.refresh(ref)
    out = ReferralCodeOut.model_validate(ref)
    out.patient_name = ref.patient.full_name
    out.doctor_name = ref.doctor.full_name

    # Notify patient via email and SMS
    notify_referral_issued(db, ref.patient, ref.doctor.full_name, ref.code)
    db.commit()

    return out


@admin_router.get("", response_model=list[ReferralCodeOut])
def list_referral_codes(
    patient_id: int | None = None,
    used: bool | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(ReferralCode)
    if patient_id is not None:
        q = q.filter(ReferralCode.patient_id == patient_id)
    if used is not None:
        q = q.filter(ReferralCode.used == used)
    rows = q.order_by(ReferralCode.created_at.desc()).all()
    out = []
    for r in rows:
        o = ReferralCodeOut.model_validate(r)
        o.patient_name = r.patient.full_name
        o.doctor_name = r.doctor.full_name
        out.append(o)
    return out


@admin_router.delete("/{code_id}", status_code=204)
def revoke_referral_code(code_id: int, db: Session = Depends(get_db)):
    ref = db.get(ReferralCode, code_id)
    if not ref:
        raise HTTPException(404, "Referral code not found")
    if ref.used:
        raise HTTPException(409, "Cannot revoke an already-used code")
    db.delete(ref)
    db.commit()


# ---- Patient endpoint ----

patient_router = APIRouter(prefix="/referrals", tags=["referrals"])


@patient_router.post("/verify", response_model=ReferralCodeOut)
def verify_referral_code(
    body: ReferralVerifyIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)

    code_upper = body.code.upper().strip()
    if not _CODE_RE.match(code_upper):
        _record_failure(ip)
        raise HTTPException(422, "Invalid code format. Code must be up to 8 alphanumeric characters.")

    ref = db.query(ReferralCode).filter_by(code=code_upper).first()

    if not ref or ref.patient_id != user.id or ref.doctor_id != body.doctor_id:
        _record_failure(ip)
        raise HTTPException(404, "Referral code not found or does not match your details.")

    if ref.used:
        _record_failure(ip)
        raise HTTPException(409, "This referral code has already been used.")

    _clear_attempts(ip)
    out = ReferralCodeOut.model_validate(ref)
    out.patient_name = ref.patient.full_name
    out.doctor_name = ref.doctor.full_name
    return out
