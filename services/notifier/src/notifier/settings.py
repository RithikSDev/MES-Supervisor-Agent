from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # demo / test installations: admins may empty all data from the UI ("Reset demo data"); never on in production
    allow_data_reset: bool = False
    mailpit_api_url: str = ""  # demo mailbox (Mailpit): emptied by the reset

    database_url: str = "postgresql+asyncpg://integ:integ@localhost:5433/mes_integration"
    kafka_bootstrap: str = "localhost:19092"
    mes_ui_url: str = "http://localhost:3080"

    # e-mail (SMTP). The demo stack uses Mailpit (a local mail catcher, web UI :8025); set a real server in .env.
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    smtp_from: str = "MES Support <mes-support@plant.local>"
    # Microsoft Teams: an incoming-webhook / Workflows URL of the support channel (optional)
    teams_webhook_url: str = ""

    dispatch_interval_s: float = 2.0
    escalation_interval_s: float = 30.0

    oidc_issuer: str = "http://localhost:8080/realms/mes"
    oidc_jwks_url: str = "http://localhost:8080/realms/mes/protocol/openid-connect/certs"
    oidc_audience: str = "mes-api"


@lru_cache
def get_settings() -> Settings:
    return Settings()
