from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # MES database, as the read-only role (infra/postgres/readonly.sql)
    mes_db_url: str = "postgresql://mes_readonly:mes_readonly@localhost:5433/mes_core"
    statement_timeout_ms: int = 5000
    max_rows: int = 200
    max_cell_chars: int = 500

    # SAP OData APIs (read-only calls with a technical user; the mock SAP in the demo)
    sap_base_url: str = "http://localhost:8030"
    sap_user: str = "MES_INTEG"
    sap_password: str = "mes-integ-secret"
    sap_timeout_s: float = 10.0

    # keys MCP clients present as `Authorization: Bearer <key>` (one per server)
    mes_db_mcp_key: str = "mes-db-mcp-demo-key"
    sap_mcp_key: str = "sap-mcp-demo-key"


@lru_cache
def get_settings() -> Settings:
    return Settings()
