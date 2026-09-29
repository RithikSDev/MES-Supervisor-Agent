"""The ServiceNow and Jira connections, configured in the admin UI and stored in the database.

  - `Vault`: secrets (ServiceNow password, Jira API token) are encrypted with ITSM_SECRET_KEY; the API never returns
    them, logs and the call log never contain them.
  - `Registry`: builds the ports from the stored connections and rebuilds them when a connection changes (the API
    process on save, the worker within `config_refresh_s`), so a new account is used without a restart.
  - `check_servicenow` / `check_jira`: the same checks as the setup CLI, used by "Test connection" in the UI.

In MCP mode (tests) and when ports are passed in, the registry is static and reads its options from the settings.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itsm_adapter.connectors.jira_rest import JiraRest
from itsm_adapter.connectors.mcp import JiraMcp, ServiceNowMcp
from itsm_adapter.connectors.servicenow_rest import ServiceNowRest
from itsm_adapter.db import connection, connection_change
from itsm_adapter.mcp_tools import McpTools
from itsm_adapter.ports import JiraPort, ServiceNowPort
from itsm_adapter.settings import Settings

log = logging.getLogger(__name__)

SYSTEMS = ("servicenow", "jira")
SN_OPTIONS = ("close_code", "contact_type", "group_l1", "group_l2", "group_l3")
JIRA_OPTIONS = ("project", "issue_type_bug", "issue_type_task")
ALLOWED_HOSTS = {"servicenow": (".service-now.com", ".servicenowservices.com"), "jira": (".atlassian.net",)}
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def validate_url(system: str, url: str, allow_any_host: bool) -> str | None:
    """None when fine, else why not. https only (plain http for local development), known cloud hosts only."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if not host or u.scheme not in ("https", "http"):
        return "enter the full address, e.g. https://dev12345.service-now.com" if system == "servicenow" \
            else "enter the full address, e.g. https://your-site.atlassian.net"
    local = host in LOCAL_HOSTS or host.endswith((".localhost", ".test"))
    if u.scheme != "https" and not local:
        return "the address must start with https://"
    if u.path not in ("", "/") or u.query:
        return "only the site address, without a path"
    if not local and not allow_any_host and not host.endswith(ALLOWED_HOSTS[system]):
        return f"not a {'ServiceNow' if system == 'servicenow' else 'Jira Cloud'} address (expected *{' or *'.join(ALLOWED_HOSTS[system])})"
    return None


class Vault:
    def __init__(self, key: str) -> None:
        self.error: str | None = None
        self._f: Fernet | None = None
        if key:
            try:
                self._f = Fernet(key.encode())
            except (ValueError, TypeError):
                self.error = "ITSM_SECRET_KEY is not a valid key (run python -m itsm_adapter.keygen)"
        else:
            self.error = "ITSM_SECRET_KEY is not set (run python -m itsm_adapter.keygen)"

    @property
    def ready(self) -> bool:
        return self._f is not None

    def seal(self, secret: str) -> str:
        if self._f is None:
            raise ValueError(self.error)
        return self._f.encrypt(secret.encode()).decode()

    def open(self, token: str | None) -> str:
        if not token:
            return ""
        if self._f is None:
            raise ValueError(self.error)
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError("the stored secret can't be decrypted with ITSM_SECRET_KEY (key changed?): enter it again") from e


@dataclass(frozen=True)
class ItsmConfig:
    """What sync and poller need besides the ports."""

    sn_instance: str | None = None  # host of the ServiceNow instance in use
    jira_site: str | None = None
    groups: dict[str, str] = field(default_factory=dict)  # tier -> ServiceNow assignment group
    jira_issue_type_bug: str = "Bug"
    jira_issue_type_task: str = "Task"

    @staticmethod
    def from_settings(s: Settings, sn_url: str = "", jira_url: str = "") -> ItsmConfig:
        return ItsmConfig(host_of(sn_url) or None, host_of(jira_url) or None, s.groups, s.jira_issue_type_bug,
                          s.jira_issue_type_task)


def defaults(system: str, s: Settings) -> dict[str, str]:
    if system == "servicenow":
        return {"close_code": s.servicenow_close_code, "contact_type": s.servicenow_contact_type,
                "group_l1": s.group_l1, "group_l2": s.group_l2, "group_l3": s.group_l3}
    return {"project": s.jira_project, "issue_type_bug": s.jira_issue_type_bug, "issue_type_task": s.jira_issue_type_task}


HttpFactory = Callable[[str], httpx.AsyncClient | None]  # system -> client (tests inject fakes)


def build_servicenow(url: str, user: str, password: str, o: dict[str, str], s: Settings,
                     http: httpx.AsyncClient | None = None) -> ServiceNowRest:
    return ServiceNowRest(url, user, password, close_code=o.get("close_code") or "Solution provided",
                          contact_type=o.get("contact_type") or "", timeout_s=s.http_timeout_s, http=http)


def build_jira(url: str, email: str, token: str, o: dict[str, str], s: Settings,
               http: httpx.AsyncClient | None = None) -> JiraRest:
    return JiraRest(url, email, token, o.get("project") or "MES", timeout_s=s.http_timeout_s, http=http)


class Registry:
    def __init__(self, settings: Settings, sf: async_sessionmaker[AsyncSession], *,
                 static: tuple[ServiceNowPort, JiraPort] | None = None, http: HttpFactory | None = None) -> None:
        self.s, self._sf, self._http = settings, sf, http or (lambda system: None)
        self._own_http = http is None
        self.vault = Vault(settings.itsm_secret_key)
        self.error: dict[str, str] = {}  # system -> why it isn't usable (e.g. secret can't be decrypted)
        self._version: tuple[Any, ...] | None = None
        self.dynamic = static is None and settings.itsm_mode == "rest"
        if static is not None:
            self.sn, self.jira = static
        elif settings.itsm_mode == "mcp":
            self.sn = ServiceNowMcp(McpTools("servicenow", settings.servicenow_mcp_url, settings.servicenow_mcp_key,
                                             settings.mcp_timeout_s), settings.servicenow_mcp_user)
            self.jira = JiraMcp(McpTools("jira", settings.jira_mcp_url, settings.jira_mcp_key, settings.mcp_timeout_s),
                                settings.jira_mcp_user, settings.jira_project)
        elif settings.itsm_mode == "rest":
            self.sn, self.jira = build_servicenow("", "", "", {}, settings), build_jira("", "", "", {}, settings)
        else:
            raise ValueError(f"ITSM_MODE must be 'rest' or 'mcp', not {settings.itsm_mode!r}")
        self.cfg = ItsmConfig.from_settings(settings, getattr(self.sn, "endpoint", ""), getattr(self.jira, "endpoint", ""))

    async def load(self) -> dict[str, Any]:
        async with self._sf() as s:
            return {r.system: r for r in (await s.execute(select(connection))).all()}

    async def refresh(self, force: bool = False) -> bool:
        """Rebuild the ports when a stored connection changed. Returns True when it did."""
        if not self.dynamic:
            return False
        rows = await self.load()
        version = tuple((k, rows[k].version) for k in sorted(rows))
        if version == self._version and not force:
            return False
        old = [self.sn, self.jira]
        errors: dict[str, str] = {}
        sn_row, jira_row = rows.get("servicenow"), rows.get("jira")
        sn_opts = {**defaults("servicenow", self.s), **(sn_row.options if sn_row else {})}
        jira_opts = {**defaults("jira", self.s), **(jira_row.options if jira_row else {})}

        def secret(row: Any) -> str:
            try:
                return self.vault.open(row.secret)
            except ValueError as e:
                errors[row.system] = str(e)
                return ""

        sn_on = sn_row is not None and sn_row.enabled
        jira_on = jira_row is not None and jira_row.enabled
        sn_secret = secret(sn_row) if sn_on else ""
        jira_secret = secret(jira_row) if jira_on else ""
        sn_ok = sn_on and "servicenow" not in errors
        jira_ok = jira_on and "jira" not in errors
        self.sn = build_servicenow(sn_row.url if sn_ok else "", sn_row.username if sn_ok else "", sn_secret, sn_opts,
                                   self.s, self._http("servicenow"))
        self.jira = build_jira(jira_row.url if jira_ok else "", jira_row.username if jira_ok else "", jira_secret,
                               jira_opts, self.s, self._http("jira"))
        self.cfg = ItsmConfig(
            sn_instance=host_of(sn_row.url) if sn_ok else None, jira_site=host_of(jira_row.url) if jira_ok else None,
            groups={"L1": sn_opts["group_l1"], "L2": sn_opts["group_l2"], "L3": sn_opts["group_l3"]},
            jira_issue_type_bug=jira_opts["issue_type_bug"], jira_issue_type_task=jira_opts["issue_type_task"])
        self.error = errors
        self._version = version
        for port in old if self._own_http else []:  # in-flight calls may still use the old client: close it later
            if hasattr(port, "aclose"):
                asyncio.get_running_loop().call_later(60, lambda p=port: asyncio.ensure_future(p.aclose()))
        log.info("connections loaded: ServiceNow %s, Jira %s", self.cfg.sn_instance or "not connected",
                 self.cfg.jira_site or "not connected")
        return True

    async def import_env_once(self) -> None:
        """Older setups kept the connections in .env: take them over once, when nothing is stored yet."""
        if not self.dynamic:
            return
        s = self.s
        candidates = [("servicenow", s.servicenow_instance_url, s.servicenow_user, s.servicenow_password),
                      ("jira", s.jira_site_url, s.jira_email, s.jira_api_token)]
        async with self._sf() as ses, ses.begin():
            existing = set((await ses.execute(select(connection.c.system))).scalars())
            for system, url, user, secret in candidates:
                if system in existing or not url or not user:
                    continue
                if secret and not self.vault.ready:
                    log.warning("%s settings in .env not imported: %s", system, self.vault.error)
                    continue
                await ses.execute(insert(connection).values(
                    system=system, url=url.rstrip("/"), username=user, secret=self.vault.seal(secret) if secret else None,
                    secret_set_at=datetime.now(UTC) if secret else None, secret_set_by="import from .env" if secret else None,
                    options=defaults(system, s), updated_by="import from .env").on_conflict_do_nothing())
                await ses.execute(connection_change.insert().values(
                    system=system, by="import from .env", changes=["url", "username", "secret", "options"],
                    note="taken over from .env once; from now on the connection is managed in the UI"))
                log.info("%s connection imported from .env (managed in the UI from now on)", system)


# ------------------------------------------------------------------------------------------------ checks

Check = dict[str, Any]  # {name, ok: True | False | None (not verifiable), detail}


async def check_servicenow(port: ServiceNowRest, groups: dict[str, str], *, create_groups: bool = False) -> list[Check]:
    out: list[Check] = []
    try:
        out.append({"name": "Sign in", "ok": True, "detail": (await port.health())["detail"][0]})
    except Exception as e:
        return [{"name": "Sign in", "ok": False, "detail": str(e)[:300]}]
    try:
        journal = await port.can_read_journal()
        out.append({"name": "Read work notes", "ok": journal,
                    "detail": "work notes (sys_journal_field) readable" if journal else
                    "no work notes yet to verify" if journal is None else
                    "the user can't read sys_journal_field: engineers' notes won't reach MES (add a read ACL)"})
    except Exception as e:
        out.append({"name": "Read work notes", "ok": False, "detail": str(e)[:300]})
    for tier, name in groups.items():
        try:
            state = await port.ensure_group(name, create=create_groups)
            out.append({"name": f"{tier} group", "ok": state != "missing",
                        "detail": f"'{name}': {state}" + (" (create it, or pick an existing group)" if state == "missing" else "")})
        except Exception as e:
            out.append({"name": f"{tier} group", "ok": False, "detail": f"'{name}': {str(e)[:250]}"})
    return out


async def check_jira(port: JiraRest, issue_types: list[str]) -> list[Check]:
    try:
        out = [{"name": "Sign in", "ok": True, "detail": (await port.health())["detail"][0]}]
    except Exception as e:
        return [{"name": "Sign in", "ok": False, "detail": str(e)[:300]}]
    try:
        types = await port.issue_types()
        out.append({"name": "Project", "ok": True, "detail": f"{port.project}: {', '.join(types)}"})
        for t in issue_types:
            out.append({"name": f"Issue type {t}", "ok": t in types,
                        "detail": "available" if t in types else f"not in project {port.project}"})
    except Exception as e:
        out.append({"name": "Project", "ok": False, "detail": f"{port.project}: {str(e)[:250]}"})
    return out
