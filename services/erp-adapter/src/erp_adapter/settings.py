from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # demo / test installations: admins may empty all data from the UI ("Reset demo data"); never on in production
    allow_data_reset: bool = False

    database_url: str = "postgresql+asyncpg://integ:integ@localhost:5433/mes_integration"
    kafka_bootstrap: str = "localhost:19092"

    sap_base_url: str = "http://localhost:8030"
    sap_user: str = "MES_INTEG"
    sap_password: str = "mes-integ-secret"
    sap_plants: str = "1000"
    sap_timeout_s: float = 5.0
    # SAP message codes (class/number) that mean "try again later", e.g. order locked by another user.
    sap_retryable_codes: str = "CO/469"

    poll_interval_s: float = 10.0
    poll_page_size: int = 50
    poll_overlap_s: int = 10
    initial_lookback_days: int = 60

    dispatch_interval_s: float = 1.0
    max_attempts: int = 8
    lease_s: int = 120

    oidc_issuer: str = "http://localhost:8080/realms/mes"
    oidc_jwks_url: str = "http://localhost:8080/realms/mes/protocol/openid-connect/certs"
    oidc_audience: str = "mes-api"

    @property
    def plants(self) -> list[str]:
        return [p.strip() for p in self.sap_plants.split(",") if p.strip()]

    @property
    def retryable_codes(self) -> set[str]:
        return {c.strip() for c in self.sap_retryable_codes.split(",") if c.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
