from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user
from ..models import Appointment, Payment, User
from ..schemas import CardPayIn, InsurancePayIn, MpesaPayIn, PaymentOut
from ..services.mpesa import normalize_phone, stk_push

router = APIRouter(prefix="/payments", tags=["payments"])


def _pending_appt(db: Session, appt_id: int, user: User) -> Appointment:
    a = db.get(Appointment, appt_id)
    if not a or a.patient_id != user.id:
        raise HTTPException(404, "Appointment not found")
    if a.status != "pending_payment":
        raise HTTPException(409, f"Appointment is {a.status}; nothing to pay")
    return a


def _settle(appt: Appointment, pay: Payment):
    """Apply a successful payment; confirm the appointment once nothing is owed."""
    appt.amount_due = max(Decimal("0"), appt.amount_due - pay.amount)
    if appt.amount_due == 0:
        appt.status, appt.hold_expires_at = "confirmed", None


@router.post("/mpesa", response_model=PaymentOut, status_code=202)
def pay_mpesa(body: MpesaPayIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    a = _pending_appt(db, body.appointment_id, user)
    phone = normalize_phone(body.phone)
    checkout_id = stk_push(phone, int(a.amount_due), f"APPT{a.id}", "Consultation fee")
    pay = Payment(appointment_id=a.id, method="mpesa", amount=a.amount_due, checkout_request_id=checkout_id)
    db.add(pay)
    db.commit()
    return pay


@router.post("/card", response_model=PaymentOut)
def pay_card(body: CardPayIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    a = _pending_appt(db, body.appointment_id, user)
    clean_num = "".join(filter(str.isdigit, body.card_number))
    last4 = clean_num[-4:] if len(clean_num) >= 4 else "CARD"
    brand = body.bank_name or ("Visa" if clean_num.startswith("4") else "Mastercard" if clean_num.startswith("5") else "Card")
    pay = Payment(
        appointment_id=a.id,
        method="card",
        amount=a.amount_due,
        status="paid",
        provider_ref=f"{brand}:{last4}",
    )
    db.add(pay)
    _settle(a, pay)
    db.commit()
    return pay


@router.post("/mpesa/callback", include_in_schema=False)
def mpesa_callback(payload: dict, db: Session = Depends(get_db)):
    """Called by Safaricom. In production restrict to Safaricom IPs at your proxy/firewall."""
    cb = payload.get("Body", {}).get("stkCallback", {})
    pay = db.query(Payment).filter_by(checkout_request_id=cb.get("CheckoutRequestID")).first()
    ack = {"ResultCode": 0, "ResultDesc": "Accepted"}
    if not pay or pay.status != "pending":
        return ack
    if cb.get("ResultCode") == 0:
        items = {i["Name"]: i.get("Value") for i in cb.get("CallbackMetadata", {}).get("Item", [])}
        pay.provider_ref = str(items.get("MpesaReceiptNumber", ""))
        if pay.appointment.status == "pending_payment":
            pay.status = "paid"
            _settle(pay.appointment, pay)
        else:
            pay.status = "refund_due"  # paid after the hold expired and the slot was released
    else:
        pay.status = "failed"  # slot stays held until the hold timer runs out
    db.commit()
    return ack


@router.post("/insurance", response_model=PaymentOut)
def pay_insurance(body: InsurancePayIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Validate cover and record the insurer's share; the patient pays the co-pay (e.g. via M-Pesa).

    `verify_member` is a stub - replace it with your insurer/clearing-house API call.
    """
    a = _pending_appt(db, body.appointment_id, user)
    coverage = verify_member(body.provider, body.member_no)
    if coverage is None:
        raise HTTPException(422, "Insurance membership could not be verified")
    insurer_share = (a.fee * coverage).quantize(Decimal("0.01"))
    pay = Payment(appointment_id=a.id, method="insurance", amount=insurer_share, status="paid",
                  provider_ref=f"{body.provider}:{body.member_no}")
    db.add(pay)
    _settle(a, pay)
    db.commit()
    return pay


def verify_member(provider: str, member_no: str) -> Decimal | None:
    """Return the covered fraction (0-1) or None if invalid. STUB: 80% cover for any non-empty member no."""
    return Decimal("0.8") if provider and member_no else None
