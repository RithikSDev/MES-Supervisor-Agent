"""Object graph for both processes (api and worker)."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from erp_adapter.inbound import MaintOrderPoller, OrderPoller
from erp_adapter.outbound import OutboundDispatcher
from erp_adapter.ports import ErpPort
from erp_adapter.sap.client import SapODataClient
from erp_adapter.sap.s4 import S4ODataErpAdapter
from erp_adapter.settings import Settings
from mes_common.db import make_engine, make_session_factory


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    sap_client: SapODataClient
    erp: ErpPort
    poller: OrderPoller
    pm_poller: MaintOrderPoller
    dispatcher: OutboundDispatcher

    async def aclose(self) -> None:
        await self.sap_client.aclose()
        await self.engine.dispose()


def build_container(settings: Settings, **client_kwargs) -> Container:  # type: ignore[no-untyped-def]
    engine = make_engine(settings.database_url, pool_size=5)
    sf = make_session_factory(engine)
    client = SapODataClient(
        settings.sap_base_url,
        settings.sap_user,
        settings.sap_password,
        timeout_s=settings.sap_timeout_s,
        retryable_codes=settings.retryable_codes,
        **client_kwargs,
    )
    erp = S4ODataErpAdapter(client)
    return Container(
        settings=settings,
        engine=engine,
        session_factory=sf,
        sap_client=client,
        erp=erp,
        poller=OrderPoller(erp, sf, settings),
        pm_poller=MaintOrderPoller(erp, sf, settings),
        dispatcher=OutboundDispatcher(erp, sf, settings),
    )
