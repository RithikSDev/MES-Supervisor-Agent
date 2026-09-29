from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # demo / test installations: admins may empty all data from the UI ("Reset demo data"); never on in production
    allow_data_reset: bool = False

    database_url: str = "postgresql+asyncpg://integ:integ@localhost:5433/mes_integration"
    kafka_bootstrap: str = "localhost:19092"

    # rest (default): ServiceNow Table API + Jira Cloud REST v3. mcp: the same operations as MCP tool calls.
    itsm_mode: str = "rest"
    http_timeout_s: float = 15.0

    # The ServiceNow and Jira connections are configured in the admin UI (Integrations -> ServiceNow / Jira) and
    # stored in the database, their secrets encrypted with this key (Fernet; `python -m itsm_adapter.keygen` writes one
    # into .env). The SERVICENOW_* / JIRA_* values below are only read once: imported when no connection is stored
    # yet (older setups), and as the configuration in MCP mode (tests).
    itsm_secret_key: str = ""
    itsm_allow_any_host: bool = False  # else only *.service-now.com / *.servicenowservices.com and *.atlassian.net
    config_refresh_s: float = 5.0  # how fast a saved connection reaches the worker

    # ServiceNow (REST): instance URL and an integration user (roles: itil; read sys_journal_field, sys_user_group)
    servicenow_instance_url: str = ""
    servicenow_user: str = ""
    servicenow_password: str = ""
    servicenow_close_code: str = "Solution provided"  # incident.close_code used when MES resolves a ticket
    servicenow_contact_type: str = ""  # incident.contact_type for incidents from MES (a choice value); empty = leave unset

    # Jira Cloud (REST): site URL, account e-mail + API token, project key and issue types
    jira_site_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project: str = "MES"
    jira_issue_type_bug: str = "Bug"
    jira_issue_type_task: str = "Task"

    # MCP mode: MCP servers exposing the ServiceNow / Jira tools (the headless mocks in tests)
    servicenow_mcp_url: str = ""
    servicenow_mcp_key: str = ""
    servicenow_mcp_user: str = "mes.integration"
    jira_mcp_url: str = ""
    jira_mcp_key: str = ""
    jira_mcp_user: str = "mes-integration"
    mcp_timeout_s: float = 10.0

    # tier -> ServiceNow assignment group
    group_l1: str = "MES L1 Operations"
    group_l2: str = "MES L2 App & DB Engineering"
    group_l3: str = "MES L3 Core Dev"
    mes_ui_url: str = "http://localhost:3080"

    poll_interval_s: float = 10.0
    poll_overlap_s: int = 120
    dispatch_interval_s: float = 1.0
    max_attempts: int = 8
    lease_s: int = 120
    # deliveries in parallel (a claimed batch holds at most one message per ticket, so order per ticket is kept)
    dispatch_concurrency: int = 4

    oidc_issuer: str = "http://localhost:8080/realms/mes"
    oidc_jwks_url: str = "http://localhost:8080/realms/mes/protocol/openid-connect/certs"
    oidc_audience: str = "mes-api"

    @property
    def groups(self) -> dict[str, str]:
        return {"L1": self.group_l1, "L2": self.group_l2, "L3": self.group_l3}


@lru_cache
def get_settings() -> Settings:
    return Settings()
