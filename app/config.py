from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_PATH, extra="ignore")

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5433/appointments"
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_minutes: int = 60 * 24
    timezone: str = "Africa/Nairobi"
    hold_minutes: int = 2  # spot held for exactly 2 minutes before being automatically dropped

    # Public base URL — used by PesaPal and other webhooks
    public_base_url: str = "https://YOUR-PUBLIC-URL"

    # ── M-Pesa (Safaricom Daraja) ────────────────────────────────────────────
    mpesa_env: str = "sandbox"
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_shortcode: str = "174379"
    mpesa_passkey: str = ""
    mpesa_callback_url: str = ""

    # ── PesaPal (card / hosted checkout) ────────────────────────────────────
    pesapal_env: str = "sandbox"
    pesapal_consumer_key: str = ""
    pesapal_consumer_secret: str = ""
    pesapal_ipn_id: str = ""

    # ── Email — Resend ───────────────────────────────────────────────────────
    resend_api_key: str = ""
    resend_from: str = "noreply@yourhospital.com"

    # ── SMS — Africa's Talking ───────────────────────────────────────────────
    at_env: str = "sandbox"
    at_api_key: str = ""
    at_username: str = "sandbox"
    at_sender_id: str = ""

    # ── Appointment reminders ────────────────────────────────────────────────
    reminder_offsets_minutes: str = "1440,120,30"       # patient reminders
    doctor_reminder_offsets_minutes: str = "120,30"     # doctor reminders
    min_reminder_lead_minutes: int = 15
    notify_doctors_external: bool = False               # also send email/SMS to doctor


settings = Settings()
