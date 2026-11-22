"""python -m app.seed  -> creates an admin, two sample doctors, departments, and hospital settings."""
from datetime import time
from decimal import Decimal

from .database import Base, SessionLocal, engine
from .models import Department, Doctor, DoctorSchedule, HospitalSettings, User
from .security import hash_password

Base.metadata.create_all(engine)
with SessionLocal() as db:
    if not db.query(User).filter_by(phone="254700000000").first():
        db.add(User(full_name="Hospital Admin", phone="254700000000", role="admin",
                    hashed_password=hash_password("admin12345")))

    # Seed doctors (with doctor login users)
    if not db.query(Doctor).count():
        for name, spec, fee, mins, days, phone in [
            ("Dr. Amina Odhiambo", "Cardiology", "3500", 30, (1, 3), "254700000001"),
            ("Dr. Peter Kamau", "Dermatology", "2500", 20, (0, 2, 4), "254700000002"),
            ("Dr. Grace Mwangi", "Neurology", "4000", 30, (0, 1, 2), "254700000003"),
            ("Dr. James Odhiambo", "Orthopedics", "3000", 20, (3, 4), "254700000004"),
            ("Dr. Sarah Njoroge", "Pediatrics", "2000", 20, (0, 2, 4), "254700000005"),
            ("Dr. Ahmed Hassan", "General Medicine", "1500", 20, (0, 1, 2, 3, 4), "254700000006"),
        ]:
            # Create doctor user account
            if not db.query(User).filter_by(phone=phone).first():
                u = User(full_name=name, phone=phone, role="doctor",
                         hashed_password=hash_password("doctor12345"))
                db.add(u)
                db.flush()

            d = Doctor(full_name=name, specialty=spec, hospital_location="Main Campus",
                       consultation_fee=Decimal(fee), slot_minutes=mins, rating=4.6)
            d.schedules = [DoctorSchedule(weekday=w, start_time=time(9), end_time=time(13)) for w in days]
            db.add(d)

    # Seed departments
    DEPARTMENTS = [
        "Cardiology",
        "Dermatology",
        "Neurology",
        "Orthopedics",
        "Pediatrics",
        "General Medicine",
        "Oncology",
        "Ophthalmology",
        "ENT",
        "Gynecology",
    ]
    for name in DEPARTMENTS:
        d = db.query(Department).filter_by(name=name).first()
        if not d:
            db.add(Department(name=name, icon=None))
        elif d.icon is not None:
            d.icon = None

    # Seed sample patients if none exist
    if not db.query(User).filter_by(role="patient").count():
        for pname, pphone, pemail in [
            ("John Kamau", "254711000001", "john.kamau@example.com"),
            ("Mary Wambui", "254711000002", "mary.wambui@example.com"),
            ("David Ochieng", "254711000003", "david.ochieng@example.com"),
        ]:
            db.add(User(
                full_name=pname,
                phone=pphone,
                email=pemail,
                role="patient",
                hashed_password=hash_password("patient12345"),
            ))

    # Seed hospital settings
    if not db.get(HospitalSettings, 1):
        db.add(HospitalSettings(
            id=1,
            payment_methods="mpesa,card,insurance",
            insurance_providers="AAR Insurance,Jubilee Health,CIC Insurance,Madison Health,APA Insurance,Britam Health,Sanlam Health,Liberty Health",
        ))

    db.commit()
print("seeded")
