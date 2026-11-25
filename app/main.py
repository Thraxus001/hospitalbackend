import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import Base, engine
from .routers import admin, appointments, auth, doctor, doctors, patients, payments
from .routers.referrals import admin_router as referral_admin_router, patient_router as referral_patient_router
from .services.expiry import expiry_loop
from .services.notifications import notify_loop

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)  # use Alembic migrations once the schema stabilises
    tasks = [
        asyncio.create_task(expiry_loop()),
        asyncio.create_task(notify_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Outsourced Doctor Appointments API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled exception on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal Server Error: {str(exc)}"},
    )


for r in (auth.router, doctors.router, appointments.router, payments.router, admin.router, patients.router, doctor.router, referral_admin_router, referral_patient_router):
    app.include_router(r, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
