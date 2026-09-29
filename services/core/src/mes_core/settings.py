from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Runtime role: DML only, cannot touch the audit log except through the trigger.
    database_url: str = "postgresql+asyncpg://mes_app:mes_app@localhost:5433/mes_core"
    # Owner role: DDL, used by migrations and the seed script.
    migration_database_url: str = "postgresql+asyncpg://mes_owner:mes_owner@localhost:5433/mes_core"
    kafka_bootstrap: str = "localhost:19092"

    oidc_issuer: str = "http://localhost:8080/realms/mes"
    oidc_jwks_url: str = "http://localhost:8080/realms/mes/protocol/openid-connect/certs"
    oidc_audience: str = "mes-api"

    redis_url: str = "redis://localhost:6379/0"
    maintenance_interval_s: float = 300
    sla_check_interval_s: float = 60

    # Operator terminals. Demo defaults; set real secrets in any shared environment.
    terminal_jwt_secret: str = "dev-only-terminal-jwt-secret-change-me-0123456789"
    terminal_pin_pepper: str = "dev-only-pin-pepper-change-me"
    terminal_session_hours: int = 8
    terminal_max_pin_failures: int = 5
    terminal_pin_lockout_s: int = 60

    audit_seal_interval_s: float = 10.0
    schedule_horizon_days: int = 14

    seed_plant_timezone: str = "Asia/Kolkata"
    # demo / test installations: admins may empty all data from the UI (never on in production)
    allow_data_reset: bool = False
    reset_database_url: str = ""  # owner role (truncate + seed); defaults to the migration URL


@lru_cache
def get_settings() -> Settings:
    return Settings()
