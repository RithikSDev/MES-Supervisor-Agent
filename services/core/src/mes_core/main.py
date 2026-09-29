"""API process: `uvicorn mes_core.main:create_app --factory`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from sqlalchemy import text

from mes_common.auth import JwksTokenVerifier, TokenVerifier
from mes_common.logs import install_request_id, setup_logging
from mes_core import models  # noqa: F401  (complete metadata)
from mes_core.modules.compliance.api import router as compliance_router
from mes_core.modules.execution.api import router as execution_router
from mes_core.modules.execution.live import LiveBus
from mes_core.modules.execution.run_api import router as run_router
from mes_core.modules.genealogy.api import router as genealogy_router
from mes_core.modules.identity.api import router as identity_router
from mes_core.modules.identity.terminal_api import router as terminal_router
from mes_core.modules.inventory.api import router as inventory_router
from mes_core.modules.maintenance.api import router as maintenance_router
from mes_core.modules.masterdata.api import router as masterdata_router
from mes_core.modules.org.api import router as org_router
from mes_core.modules.planning.api import router as planning_router
from mes_core.modules.quality.api import router as quality_router
from mes_core.modules.ticketing.api import router as ticketing_router
from mes_core.reset import router as reset_router
from mes_core.settings import Settings, get_settings
from mes_core.shared.db.session import Database
from mes_core.web.errors import install_error_handlers


def create_app(
    settings: Settings | None = None, *, db: Database | None = None, verifier: TokenVerifier | None = None
) -> FastAPI:
    setup_logging("mes-core-api")
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.db = db or Database(settings.database_url)
        app.state.verifier = verifier or JwksTokenVerifier(
            issuer=settings.oidc_issuer, jwks_url=settings.oidc_jwks_url, audience=settings.oidc_audience
        )
        app.state.user_cache = {}
        app.state.live = LiveBus(settings.redis_url or None)
        yield
        await app.state.live.aclose()
        if db is None:
            await app.state.db.dispose()

    app = FastAPI(
        title="MES Core API",
        version="0.1.0",
        lifespan=lifespan,
        description="Manufacturing Execution System: organisation, work orders, scheduling, confirmations, audit.",
    )
    install_error_handlers(app)

    install_request_id(app)

    @app.get("/health/live", tags=["health"])
    async def live() -> dict:
        return {"status": "UP"}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request) -> dict:
        async with request.app.state.db.read() as s:
            await s.execute(text("SELECT 1"))
        return {"status": "UP"}

    api = APIRouter(prefix="/api/v1")
    for r in (
        identity_router,
        terminal_router,
        org_router,
        masterdata_router,
        planning_router,
        execution_router,
        run_router,
        inventory_router,
        quality_router,
        genealogy_router,
        maintenance_router,
        ticketing_router,
        compliance_router,
        reset_router,
    ):
        api.include_router(r)
    app.include_router(api)
    return app
