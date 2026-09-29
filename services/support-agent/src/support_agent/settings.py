from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# repository root: /app in the container, the checkout locally (knowledge/runbooks lives there)
REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # demo / test installations: admins may empty all data from the UI ("Reset demo data"); never on in production
    allow_data_reset: bool = False

    database_url: str = "postgresql+asyncpg://integ:integ@localhost:5433/mes_integration"
    kafka_bootstrap: str = "localhost:19092"

    # MES APIs, called as the agent's Keycloak service account (or with a user's own token, see actions.py)
    core_api_url: str = "http://localhost:8000/api/v1"
    erp_api_url: str = "http://localhost:8020/api/v1"
    itsm_api_url: str = "http://localhost:8070/api/v1"
    token_url: str = "http://localhost:8080/realms/mes/protocol/openid-connect/token"
    client_id: str = "mes-support-agent"
    client_secret: str = "mes-support-agent-secret"
    plant_code: str = "P1000"
    mes_ui_url: str = "http://localhost:3080"
    loki_url: str = "http://localhost:3100"  # service logs (L2 diagnosis)

    # Claude
    anthropic_api_key: str = ""
    agent_model: str = "claude-opus-5"
    effort_ticket: str = "high"  # diagnosing a ticket: thoroughness matters more than latency
    effort_chat: str = "medium"  # talking to an operator at the line: keep turns quick
    effort_l2: str = "high"  # root-cause analysis at L2
    max_tokens: int = 32000
    refusal_fallback: bool = True  # server-side fallback to another model if a request is declined
    anthropic_timeout_s: float = 300

    runbooks_dir: Path = REPO_ROOT / "knowledge" / "runbooks"
    max_steps: int = 24  # model turns per run
    worker_interval_s: float = 2.0
    run_lease_s: int = 900
    # only tickets raised recently are picked up from the event stream (a first start or a reset consumer group
    # replays the whole topic: old tickets must not be worked, or handed over, again)
    max_event_age_min: int = 30
    retry_after_s: int = 60  # transient Claude errors (rate limit, overload, network): the run is retried later

    # MCP servers the agent may use (registered in the UI, tokens encrypted with this key: the ITSM_SECRET_KEY)
    agent_secret_key: str = ""
    mcp_timeout_s: float = 20.0
    # defaults registered on first start (the read-only data MCP servers of the stack)
    default_mes_db_mcp_url: str = ""
    default_mes_db_mcp_key: str = ""
    default_sap_mcp_url: str = ""
    default_sap_mcp_key: str = ""

    oidc_issuer: str = "http://localhost:8080/realms/mes"
    oidc_jwks_url: str = "http://localhost:8080/realms/mes/protocol/openid-connect/certs"
    oidc_audience: str = "mes-api"


@lru_cache
def get_settings() -> Settings:
    return Settings()
