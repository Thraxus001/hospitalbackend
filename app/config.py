from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5433/appointments"
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_minutes: int = 60 * 24
    timezone: str = "Africa/Nairobi"
    hold_minutes: int = 2  # spot held for exactly 2 minutes before being automatically dropped

    mpesa_env: str = "sandbox"
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_shortcode: str = "174379"
    mpesa_passkey: str = ""
    mpesa_callback_url: str = ""


settings = Settings()
