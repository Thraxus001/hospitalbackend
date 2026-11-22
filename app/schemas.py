from datetime import datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=2)
    phone: str = Field(min_length=9)
    email: str | None = None
    password: str = Field(min_length=8)


class LoginIn(BaseModel):
    phone: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(ORM):
    id: int
    full_name: str
    phone: str
    email: str | None
    role: str


class AdminPatientIn(BaseModel):
    full_name: str = Field(min_length=2)
    phone: str = Field(min_length=9)
    email: str | None = None


class DoctorIn(BaseModel):
    full_name: str
    specialty: str
    hospital_location: str = ""
    slot_minutes: int = Field(20, ge=20, le=30)
    consultation_fee: Decimal = Field(gt=0)
    max_daily_appointments: int = Field(20, ge=1)


class DoctorOut(ORM):
    id: int
    full_name: str
    specialty: str
    hospital_location: str
    rating: float
    slot_minutes: int
    consultation_fee: float
    working_days: list[int] = []
    has_slots_today: bool = False


class AdminDoctorOut(DoctorOut):
    max_daily_appointments: int
    active: bool
    has_login: bool


class ScheduleIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time


class ScheduleOut(ORM):
    id: int
    weekday: int
    start_time: time
    end_time: time


class SlotOut(BaseModel):
    start_at: datetime
    end_at: datetime


class BookIn(BaseModel):
    doctor_id: int
    start_at: datetime
    referral_code: str = Field(min_length=1, max_length=8)


class RescheduleIn(BaseModel):
    start_at: datetime


class PriorityBookIn(BaseModel):
    patient_id: int
    doctor_id: int
    start_at: datetime
    priority: int = Field(0, ge=0, le=1)  # 0 emergency, 1 high


class AppointmentOut(ORM):
    id: int
    doctor_id: int
    doctor: DoctorOut
    patient: UserOut | None = None
    start_at: datetime
    end_at: datetime
    status: str
    priority: int
    fee: float
    amount_due: float
    hold_expires_at: datetime | None
    rescheduled_from_id: int | None


class MpesaPayIn(BaseModel):
    appointment_id: int
    phone: str


class InsurancePayIn(BaseModel):
    appointment_id: int
    provider: str
    member_no: str


class CardPayIn(BaseModel):
    appointment_id: int
    card_number: str
    card_holder: str = "Valued Patient"
    expiry: str = ""
    cvv: str | None = None
    bank_name: str | None = None


class PaymentOut(ORM):
    id: int
    method: str
    amount: float
    status: str
    provider_ref: str | None


class DepartmentIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    icon: str | None = None


class DepartmentOut(ORM):
    id: int
    name: str
    icon: str | None
    active: bool
    doctor_count: int = 0


class HospitalSettingsIn(BaseModel):
    payment_methods: list[str]
    insurance_providers: list[str]


class HospitalSettingsOut(BaseModel):
    payment_methods: list[str]
    insurance_providers: list[str]


class UserPaymentPrefIn(BaseModel):
    preferred_method: str | None = None
    insurance_provider: str | None = None
    insurance_member_no: str | None = None
    mpesa_phone: str | None = None


class UserPaymentPrefOut(ORM):
    preferred_method: str | None
    insurance_provider: str | None
    insurance_member_no: str | None
    mpesa_phone: str | None


class NotificationOut(ORM):
    id: int
    kind: str
    channel: str
    subject: str
    body: str
    status: str
    appointment_id: int | None
    created_at: datetime
    read_at: datetime | None


class AdminPaymentOut(ORM):
    id: int
    appointment_id: int
    patient: str
    doctor: str
    method: str
    amount: float
    status: str
    provider_ref: str | None
    created_at: datetime


class AdminNotificationOut(NotificationOut):
    recipient: str
    user_name: str
    attempts: int
    error: str | None


class AuditRowOut(ORM):
    id: int
    action: str
    detail: str
    created_at: datetime
    admin: str = ""


class ReferralCodeIn(BaseModel):
    patient_id: int
    doctor_id: int


class ReferralCodeOut(ORM):
    id: int
    code: str
    patient_id: int
    doctor_id: int
    used: bool
    used_at: datetime | None
    created_at: datetime
    patient_name: str = ""
    doctor_name: str = ""


class ReferralVerifyIn(BaseModel):
    code: str = Field(min_length=1, max_length=8)
    doctor_id: int
