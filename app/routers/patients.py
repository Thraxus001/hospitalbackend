"""Patient-facing endpoints: payment preferences + public hospital settings."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user
from ..models import Department, Doctor, HospitalSettings, UserPaymentPreference, User
from ..schemas import DepartmentOut, HospitalSettingsOut, UserPaymentPrefIn, UserPaymentPrefOut

router = APIRouter(prefix="/patient", tags=["patient"])


def _get_hospital_settings(db: Session) -> HospitalSettings:
    s = db.get(HospitalSettings, 1)
    if not s:
        s = HospitalSettings(id=1, payment_methods="mpesa,card,insurance", insurance_providers="")
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


@router.get("/settings", response_model=HospitalSettingsOut)
def hospital_settings(db: Session = Depends(get_db)):
    """Public endpoint – no auth required. Returns the payment methods and insurance providers
    the hospital accepts (configured by admin)."""
    s = _get_hospital_settings(db)
    return HospitalSettingsOut(
        payment_methods=[m.strip() for m in s.payment_methods.split(",") if m.strip()],
        insurance_providers=[p.strip() for p in s.insurance_providers.split(",") if p.strip()],
    )


@router.get("/departments", response_model=list[DepartmentOut])
def departments(db: Session = Depends(get_db)):
    """Public endpoint – returns active departments with doctor counts."""
    depts = db.query(Department).filter(Department.active.is_(True)).order_by(Department.name).all()
    out = []
    for dept in depts:
        d = DepartmentOut.model_validate(dept)
        d.doctor_count = db.query(Doctor).filter(
            Doctor.specialty.ilike(dept.name), Doctor.active.is_(True)
        ).count()
        out.append(d)
    return out


@router.get("/preferences", response_model=UserPaymentPrefOut)
def get_prefs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    pref = db.query(UserPaymentPreference).filter_by(user_id=user.id).first()
    if not pref:
        return UserPaymentPrefOut(preferred_method=None, insurance_provider=None,
                                   insurance_member_no=None, mpesa_phone=None)
    return pref


@router.put("/preferences", response_model=UserPaymentPrefOut)
def set_prefs(body: UserPaymentPrefIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    pref = db.query(UserPaymentPreference).filter_by(user_id=user.id).first()
    if pref:
        for k, v in body.model_dump().items():
            setattr(pref, k, v)
    else:
        pref = UserPaymentPreference(user_id=user.id, **body.model_dump())
        db.add(pref)
    db.commit()
    db.refresh(pref)
    return pref
