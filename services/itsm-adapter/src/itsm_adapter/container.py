"""Object graph for both processes (api and worker). The ServiceNow and Jira connections come from the database
(configured in the admin UI) through the `Registry`, which rebuilds the ports when a connection changes. ITSM_MODE
`mcp` (tests) reaches the same operations as MCP tool calls, configured from the settings."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from itsm_adapter.connections import HttpFactory, Registry
from itsm_adapter.poller import ItsmPoller
from itsm_adapter.ports import JiraPort, ServiceNowPort
from itsm_adapter.settings import Settings
from itsm_adapter.sync import Dispatcher, TicketSync
from mes_common.db import make_engine, make_session_factory


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    registry: Registry
    sync: TicketSync
    dispatcher: Dispatcher
    poller: ItsmPoller

    @property
    def servicenow(self) -> ServiceNowPort:
        return self.registry.sn

    @property
    def jira(self) -> JiraPort:
        return self.registry.jira

    async def aclose(self) -> None:
        await self.engine.dispose()


def build_container(settings: Settings, ports: tuple[ServiceNowPort, JiraPort] | None = None,
                    http: HttpFactory | None = None) -> Container:
    engine = make_engine(settings.database_url, pool_size=5)
    sf = make_session_factory(engine)
    reg = Registry(settings, sf, static=ports, http=http)
    sync = TicketSync(reg, settings)
    return Container(settings, engine, sf, reg, sync, Dispatcher(sync, sf, settings), ItsmPoller(reg, sync, sf, settings))
