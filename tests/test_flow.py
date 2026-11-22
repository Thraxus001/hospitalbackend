"""End-to-end check against a real PostgreSQL (the booking index, SKIP LOCKED and time zones need it).

    pip install pgserver pytest && pytest -q tests          # spins up a throwaway embedded Postgres
or set TEST_DATABASE_URL to an empty database you own.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest


@pytest.fixture(scope="module")
def env():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        import pgserver
        srv = pgserver.get_server(tempfile.mkdtemp())
        url = srv.get_uri().replace("postgresql://", "postgresql+psycopg2://", 1)
    os.environ["DATABASE_URL"] = url
    os.environ["REMINDER_OFFSETS_MINUTES"] = "1440,120,30"
    from fastapi.testclient import TestClient
    from app.database import Base, SessionLocal, engine
    from app.main import app
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    import runpy
    runpy.run_module("app.seed", run_name="seed")
    return TestClient(app), SessionLocal


def login(c, phone, pw):
    r = c.post("/api/auth/login", json={"phone": phone, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def register(c, name, phone, email):
    r = c.post("/api/auth/register", json={"full_name": name, "phone": phone, "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(c, phone, "password123")


def free_slots(c, doctor_id, n=6):
    out = []
    for i in range(3, 17):  # 3+ days ahead: always outside the 24h refund window
        day = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=i)).date().isoformat()
        out += c.get(f"/api/doctors/{doctor_id}/slots", params={"day": day}).json()
        if len(out) >= n:
            break
    return out


def test_full_flow(env, monkeypatch):
    c, SessionLocal = env
    from app.models import Appointment, Notification, Payment
    from app.services import notifications as N, pesapal

    admin = login(c, "0700000000", "admin12345")
    doc_hdr = login(c, "0700000001", "doctor12345")
    ana = register(c, "Ana Wanjiru", "0711000001", "ana@example.com")
    ben = register(c, "Ben Otieno", "0711000002", "")  # no email: SMS only

    doctor = next(d for d in c.get("/api/doctors").json() if d["full_name"].startswith("Dr. Amina"))
    assert doctor["has_login"] and doctor["slot_minutes"] == 30
    slots = free_slots(c, doctor["id"])
    s0, s1, s2, s4 = slots[0], slots[1], slots[2], slots[4]
    # 30-minute slots never overlap
    assert datetime.fromisoformat(s1["start_at"]) - datetime.fromisoformat(s0["start_at"]) >= timedelta(minutes=30)

    # --- booking holds the slot; a second patient cannot take it
    r = c.post("/api/appointments", json={"doctor_id": doctor["id"], "start_at": s0["start_at"]}, headers=ana)
    assert r.status_code == 201 and r.json()["status"] == "pending_payment"
    appt_id = r.json()["id"]
    assert c.post("/api/appointments", json={"doctor_id": doctor["id"], "start_at": s0["start_at"]}, headers=ben).status_code == 409

    # --- card/bank checkout via (mocked) Pesapal -> IPN -> verified -> confirmed
    monkeypatch.setattr(pesapal, "submit_order", lambda *a, **k: ("TRK-1", "https://pay.example/checkout/TRK-1"))
    monkeypatch.setattr(pesapal, "get_status", lambda t: {"payment_status_description": "Completed", "payment_method": "Visa",
                                                           "amount": 3500, "currency": "KES", "confirmation_code": "CONF123"})
    co = c.post("/api/payments/checkout", json={"appointment_id": appt_id}, headers=ana)
    assert co.status_code == 201 and co.json()["checkout_url"].startswith("https://pay.example")
    ipn = c.get("/api/payments/pesapal/ipn", params={"OrderTrackingId": "TRK-1", "OrderMerchantReference": f"APPT{appt_id}-P1"})
    assert ipn.json()["status"] == 200
    assert c.get(f"/api/appointments/{appt_id}", headers=ana).json()["status"] == "confirmed"
    ipn2 = c.get("/api/payments/pesapal/ipn", params={"OrderTrackingId": "TRK-1"})  # replayed IPN is harmless
    with SessionLocal() as db:
        pays = db.query(Payment).filter_by(appointment_id=appt_id).all()
        assert [(p.status, p.method, p.provider_ref) for p in pays] == [("paid", "card", "CONF123")]

    # --- confirmation notifications: patient email+SMS, doctor dashboard
    with SessionLocal() as db:
        got = {(n.kind, n.channel) for n in db.query(Notification).filter_by(appointment_id=appt_id)}
        assert {("confirmed", "email"), ("confirmed", "sms"), ("new_booking", "in_app")} <= got

        # --- reminders: 24h/2h/30min logic and exactly-once delivery
        start = db.get(Appointment, appt_id).start_at

        def kinds():
            return sorted(n.kind for n in db.query(Notification).filter(Notification.appointment_id == appt_id, Notification.kind.like("%reminder_%")))

        N.schedule_reminders(db, start - timedelta(days=3)); db.commit()
        assert kinds() == []                                    # too early
        N.schedule_reminders(db, start - timedelta(minutes=100)); db.commit()
        assert "reminder_120m" in " ".join(kinds()) and "reminder_30m" not in " ".join(kinds())
        N.schedule_reminders(db, start - timedelta(minutes=25)); db.commit()
        before = kinds()
        N.schedule_reminders(db, start - timedelta(minutes=24)); db.commit()
        assert kinds() == before                                # never sent twice
        assert before.count("reminder_30m") == 2                # patient email + SMS; doctor 30m is "doctor_reminder_30m"
        assert before.count("doctor_reminder_30m") == 3         # doctor dashboard + email + SMS
        n_before = db.query(Notification).count()
        N.schedule_reminders(db, start - timedelta(minutes=3)); db.commit()   # too close to be useful
        assert db.query(Notification).count() == n_before

        assert N.deliver_pending(db) > 0
        assert db.query(Notification).filter_by(status="pending").count() == 0
        assert {n.status for n in db.query(Notification).filter(Notification.channel != "in_app")} == {"dry_run"}

    # --- doctor dashboard sees the alerts; marks them read; cannot complete a future visit
    feed = c.get("/api/doctor/notifications", headers=doc_hdr).json()
    assert any(f["kind"] == "doctor_reminder_30m" for f in feed)
    assert c.post("/api/doctor/notifications/read-all", headers=doc_hdr).status_code == 200
    assert c.get("/api/doctor/notifications", params={"unread_only": True}, headers=doc_hdr).json() == []
    assert c.post(f"/api/doctor/appointments/{appt_id}/complete", headers=doc_hdr).status_code == 409
    assert c.get("/api/doctor/appointments", headers=ana).status_code == 403   # patients can't use doctor API

    # --- no-show -> free reschedule -> emergency bumps it -> displaced patient is notified by email+SMS
    assert c.post(f"/api/admin/appointments/{appt_id}/no-show", headers=admin).status_code == 200
    rs = c.post(f"/api/appointments/{appt_id}/reschedule", json={"start_at": s1["start_at"]}, headers=ana)
    assert rs.status_code == 200 and rs.json()["rescheduled_from_id"] == appt_id and rs.json()["status"] == "confirmed"
    moved_id = rs.json()["id"]
    ben_id = c.get("/api/auth/me", headers=ben).json()["id"]
    pr = c.post("/api/admin/appointments/priority", json={"patient_id": ben_id, "doctor_id": doctor["id"], "start_at": s1["start_at"], "priority": 0}, headers=admin)
    assert pr.status_code == 201 and pr.json()["displaced"]["id"] == moved_id
    assert datetime.fromisoformat(pr.json()["displaced"]["start_at"]) == datetime.fromisoformat(s2["start_at"])  # pushed to next free slot
    with SessionLocal() as db:
        assert {(n.kind, n.channel) for n in db.query(Notification).filter_by(appointment_id=moved_id, kind="moved")} >= {("moved", "email"), ("moved", "sms")}
        # Ben has no email: priority confirmation goes by SMS only
        ben_n = [n.channel for n in db.query(Notification).filter(Notification.user_id == ben_id, Notification.kind == "confirmed")]
        assert ben_n == ["sms"]

    # --- late cancellation keeps fee; early cancellation -> refund queue -> refunded notice
    third = c.post("/api/appointments", json={"doctor_id": doctor["id"], "start_at": s4["start_at"]}, headers=ben).json()["id"]
    monkeypatch.setattr(pesapal, "submit_order", lambda *a, **k: ("TRK-2", "https://pay.example/2"))
    c.post("/api/payments/checkout", json={"appointment_id": third}, headers=ben)
    c.get("/api/payments/pesapal/ipn", params={"OrderTrackingId": "TRK-2"})
    assert c.post(f"/api/appointments/{third}/cancel", headers=ben).json()["status"] == "cancelled"
    rows = c.get("/api/admin/payments", params={"status": "refund_due"}, headers=admin).json()
    assert len(rows) == 1
    assert c.post(f"/api/admin/payments/{rows[0]['id']}/refunded", headers=admin).status_code == 200
    with SessionLocal() as db:
        kinds_ben = {n.kind for n in db.query(Notification).filter_by(user_id=ben_id)}
        assert {"refund_due", "refunded"} <= kinds_ben

    # --- admin notification log + summary
    log = c.get("/api/admin/notifications", headers=admin).json()
    assert log and all(x["channel"] in ("email", "sms") for x in log)
    summ = c.get("/api/admin/reports/summary", headers=admin).json()
    assert summ["collected_by_method"]["card"] >= 3500
